import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import '../logic/track_metadata.dart';
import 'music_api.dart';
import 'playback_port.dart';
import 'catalog_api.dart';

class WebPlayer extends ChangeNotifier {
  final MusicApi api;
  final PlaybackPort audio;
  SessionStore store;
  late final StreamSubscription<String> _subscription;
  List<Map<String, dynamic>> queue = [];
  int index = 0;
  String title = 'Your queue';
  String? error;
  String? relatedError;
  String? storageError;
  bool loading = false;
  bool loadingRelated = false;
  Future<void>? _relatedRequest;
  bool startupInterrupted = false;
  bool _hiddenDuringStartup = false;
  bool _awaitingFirstPlayback = false;
  bool _wantsPlayback = false, _visible = true, _resuming = false;
  int _resumeRecoveries = 0;
  bool hasMore = true;
  int _session = 0;
  int _operation = 0;
  int _listeningEpoch = 0;
  int get listeningEpoch => _listeningEpoch;
  bool _disposed = false;
  final Map<String, Uri> _ready = {};
  final Map<String, DateTime> _readyUntil = {};
  DateTime? _loadedUntil;
  int? _lastStartupMs, _lastNextGapMs;
  int? _lastResumeMs;
  Stopwatch? _resumeWatch;
  bool _resumeAutomatic = false;
  bool _resumeSourceRetained = false;
  bool _usedPreparation = false;
  final Map<String, Future<Uri>> _pending = {};
  List<Map<String, dynamic>> _searchPreparation = [];
  bool _preparingSearch = false;
  int _queuePreparations = 0;
  int _preparationScope = 0;
  final String _queueCacheToken =
      'queue-${DateTime.now().microsecondsSinceEpoch}';
  int _queueCacheRevision = 0;
  String _queueCacheSignature = '';
  DateTime? _queueCacheRetryAfter;
  final Map<String, DateTime> _prepareRetryAfter = {};
  Timer? _prepareRetryTimer;
  Stopwatch? _startupWatch, _transitionWatch;
  String _discoverySession = '';
  final List<String> _recentPlayed = [];
  int _queueRevision = 0;
  int get queueRevision => _queueRevision;
  final Set<String> _queueRemovedIds = {};
  final Map<String, ({DateTime until, Map<String, dynamic> data})> _lyrics = {};
  final Map<String, Future<Map<String, dynamic>>> _lyricsPending = {};
  String _lyricsWarmKey = '';
  int _lyricsEpoch = 0;

  String _lyricsKey(Map<String, dynamic> track) => jsonEncode([
        api.userScopeId,
        extractTrackId(track),
        track['title'],
        track['artist'] ?? track['channel'],
        track['album'],
        trackDurationSeconds(track).round(),
        track['playback'] is Map
            ? track['playback']['source_id']
            : track['videoId'] ?? track['source_id'],
      ]);

  Map<String, dynamic>? cachedLyrics(Map<String, dynamic> track) {
    final key = _lyricsKey(track), cached = _lyrics[key];
    if (cached == null) return null;
    if (cached.until.isBefore(DateTime.now())) {
      _lyrics.remove(key);
      return null;
    }
    return cached.data;
  }

  Future<Map<String, dynamic>> lyricsFor(Map<String, dynamic> track,
      {bool refresh = false}) {
    final key = _lyricsKey(track);
    final cached = refresh ? null : cachedLyrics(track);
    if (cached != null) return Future.value(cached);
    if (_lyricsPending[key] case final pending?) return pending;
    final epoch = _lyricsEpoch;
    late final Future<Map<String, dynamic>> work;
    work = CatalogApi(api)
        .lyrics(Map<String, dynamic>.from(track), refresh: refresh)
        .timeout(const Duration(seconds: 12))
        .then((data) {
      if (!_disposed && epoch == _lyricsEpoch) {
        final status = data['lyrics_status'];
        final ttl = status == 'provider_error'
            ? const Duration(seconds: 30)
            : status == 'not_found'
                ? const Duration(minutes: 15)
                : const Duration(hours: 6);
        _lyrics.remove(key);
        _lyrics[key] = (until: DateTime.now().add(ttl), data: data);
        while (_lyrics.length > 24) {
          _lyrics.remove(_lyrics.keys.first);
        }
      }
      return data;
    }).whenComplete(() {
      if (identical(_lyricsPending[key], work)) _lyricsPending.remove(key);
    });
    _lyricsPending[key] = work;
    return work;
  }

  void _warmLyrics() {
    final track = current;
    if (track == null || _lyricsWarmKey == _lyricsKey(track)) return;
    _lyricsWarmKey = _lyricsKey(track);
    // Audio has started; lyric providers must never gate the play gesture.
    if (_lyricsPending.length >= 2) return;
    final next = index + 1 < queue.length ? queue[index + 1] : null;
    final epoch = _lyricsEpoch;
    unawaited(() async {
      try {
        await lyricsFor(track);
      } catch (_) {/* Manual Retry remains available. */}
      if (!_disposed &&
          epoch == _lyricsEpoch &&
          next != null &&
          _lyricsPending.length < 2 &&
          identical(current, track)) {
        try {
          await lyricsFor(next);
        } catch (_) {/* Speculative lookup only. */}
      }
    }());
  }

  void preloadSearch(List<Map<String, dynamic>> tracks) {
    final seen = <String>{};
    _searchPreparation = tracks
        .where((track) {
          final id = extractTrackId(track);
          return id != null && seen.add(id);
        })
        .take(5)
        .toList();
    unawaited(_drainSearchPreparation());
  }

  Future<void> _drainSearchPreparation() async {
    if (_preparingSearch || _disposed) return;
    _preparingSearch = true;
    try {
      while (!_disposed &&
          !busy &&
          _queuePreparations == 0 &&
          _searchPreparation.isNotEmpty) {
        final track = _searchPreparation.removeAt(0);
        try {
          await _prepare(track);
        } catch (_) {
          // A speculative failure must not prevent explicit playback.
        }
      }
    } finally {
      _preparingSearch = false;
      _primeNext();
    }
  }

  double _restoredPosition = 0;
  double _knownDuration = 0;
  bool _metadataReady = false;
  int _lastSavedSecond = -1;
  String? _loadedId;

  WebPlayer(this.api, this.audio, this.store) {
    _restore();
    _subscription = audio.events.listen(_onEvent);
  }

  void _restore() {
    try {
      final raw = store.read();
      if (raw != null) {
        final saved = jsonDecode(raw) as Map;
        if (saved['version'] == 1) {
          if (saved['preparedMedia'] is List)
            api.restorePreparedMedia(saved['preparedMedia']);
          queue = MusicApi.tracks(saved['queue']);
          final storedIndex = saved['currentIndex'];
          final savedIndex = storedIndex is int &&
                  storedIndex >= 0 &&
                  storedIndex < queue.length &&
                  extractTrackId(queue[storedIndex]) == saved['current']
              ? storedIndex
              : queue.indexWhere((t) => extractTrackId(t) == saved['current']);
          if (savedIndex >= 0) {
            index = savedIndex;
            title = saved['title']?.toString() ?? title;
            _restoredPosition = (saved['position'] as num? ?? 0).toDouble();
            _knownDuration =
                (saved['duration'] as num? ?? trackDurationSeconds(current!))
                    .toDouble();
            hasMore = saved['hasMore'] != false;
            _queueRemovedIds.addAll((saved['removedIds'] as List? ?? const [])
                .whereType<String>()
                .take(256));
            _discoverySession = saved['discoverySession']?.toString() ?? '';
            final savedRecent = saved['recentPlayed'] is List
                ? (saved['recentPlayed'] as List).whereType<String>()
                : const <String>[];
            _recentPlayed
              ..clear()
              ..addAll(
                  savedRecent.toList().reversed.take(40).toList().reversed);
          } else {
            queue = [];
          }
        }
      }
    } catch (_) {
      storageError = 'The saved queue could not be restored.';
    }
  }

  void changeAccount(SessionStore nextStore) {
    _cancelQueueCache();
    save();
    audio.pause();
    _operation++;
    _session++;
    _queueRevision++;
    _queueRemovedIds.clear();
    queue = [];
    index = 0;
    title = 'Your queue';
    _loadedId = null;
    _metadataReady = false;
    _restoredPosition = 0;
    _knownDuration = 0;
    _lastSavedSecond = -1;
    loading = loadingRelated = false;
    error = relatedError = storageError = null;
    startupInterrupted = false;
    _hiddenDuringStartup = false;
    _awaitingFirstPlayback = false;
    _wantsPlayback = _resuming = false;
    _resumeRecoveries = 0;
    _searchPreparation = [];
    _preparationScope++;
    _lyricsEpoch++;
    _lyrics.clear();
    _lyricsPending.clear();
    _lyricsWarmKey = '';
    _pending.clear();
    _prepareRetryAfter.clear();
    _prepareRetryTimer?.cancel();
    _startupWatch = _transitionWatch = null;
    _ready.clear();
    _readyUntil.clear();
    _loadedUntil = null;
    _lastStartupMs = _lastNextGapMs = null;
    _lastResumeMs = null;
    _resumeWatch = null;
    _discoverySession = '';
    _recentPlayed.clear();
    hasMore = true;
    store = nextStore;
    _restore();
    _notify();
  }

  Future<void> startQueue(
      List<Map<String, dynamic>> tracks, int selected, String label) async {
    if (selected < 0 || selected >= tracks.length) return;
    final retain = _canRetain(tracks[selected]);
    _cancelQueueCache();
    _session++;
    _queueRevision++;
    _queueRemovedIds.clear();
    _transitionWatch = null;
    queue = tracks.map((track) => Map<String, dynamic>.from(track)).toList();
    index = selected;
    title = label;
    _discoverySession =
        '${DateTime.now().microsecondsSinceEpoch}-${_session.toRadixString(36)}';
    loadingRelated = false;
    relatedError = null;
    hasMore = true;
    if (retain) {
      audio.record('queue-context-replaced', {'sourceRetained': true});
      _queueEdited();
      if (queue.length - index <= 5) unawaited(replenish());
      return;
    }
    final playback = playIndex(selected);
    // Start native playback in the gesture before requesting recommendations.
    // Keep album/playlist order; related songs only extend the tail.
    if (queue.length - index <= 5) unawaited(replenish());
    unawaited(
        api.recordExplicitSearchSelection(tracks[selected]).catchError((_) {}));
    await playback;
  }

  Map<String, dynamic>? get current => queue.isEmpty ? null : queue[index];

  bool _canRetain(Map<String, dynamic> track) =>
      playing &&
      !audio.paused &&
      !audio.needsNewSource &&
      audio.sourceMatches &&
      _loadedId != null &&
      _loadedId == audio.loadedTrackId &&
      _loadedId == extractTrackId(track);

  bool _canEditQueue(int at, int? revision) =>
      !_disposed &&
      at >= 0 &&
      at < queue.length &&
      (revision == null || revision == _queueRevision);

  void _queueEdited() {
    _queueRevision++;
    // Keep the current source, listening epoch and pending Play promise intact.
    // The existing cache-plan revision supersedes speculative work in old order.
    _primeNext();
    save();
    _notify();
  }

  bool removeQueueItem(int at, {int? revision}) {
    if (!_canEditQueue(at, revision) || at == index) return false;
    final id = extractTrackId(queue[at]);
    if (id != null) {
      _queueRemovedIds.add(id);
      while (_queueRemovedIds.length > 256) {
        _queueRemovedIds.remove(_queueRemovedIds.first);
      }
    }
    queue = List.of(queue)..removeAt(at);
    if (at < index) index--;
    _queueEdited();
    return true;
  }

  /// Destination is the final queue index, not ReorderableList's drop offset.
  bool moveQueueItem(int from, int to, {int? revision}) {
    if (!_canEditQueue(from, revision) ||
        from == index ||
        to < 0 ||
        to >= queue.length ||
        from == to) return false;
    final moved = queue[from];
    queue = List.of(queue)
      ..removeAt(from)
      ..insert(to, moved);
    if (from < index && to >= index) {
      index--;
    } else if (from > index && to <= index) {
      index++;
    }
    _queueEdited();
    return true;
  }

  bool playNext(Map<String, dynamic> track, {int? fromIndex, int? revision}) {
    if (_disposed ||
        (revision != null && revision != _queueRevision) ||
        extractTrackId(track) == null ||
        track['playable'] == false) return false;
    if (fromIndex != null) {
      if (!_canEditQueue(fromIndex, revision) || fromIndex == index)
        return false;
      final destination = fromIndex < index ? index : index + 1;
      if (fromIndex == destination) return true;
      return moveQueueItem(fromIndex, destination, revision: revision);
    }
    final copy = Map<String, dynamic>.from(track);
    _queueRemovedIds.remove(extractTrackId(copy));
    if (queue.isEmpty) {
      queue = [copy];
      index = 0;
      title = 'Your queue';
      hasMore = true;
    } else {
      queue = List.of(queue)..insert(index + 1, copy);
    }
    _queueEdited();
    return true;
  }

  bool addToQueue(Map<String, dynamic> track, {int? revision}) {
    final id = extractTrackId(track);
    if (_disposed ||
        id == null ||
        track['playable'] == false ||
        (revision != null && revision != _queueRevision) ||
        queue.any((item) => extractTrackId(item) == id)) return false;
    _queueRemovedIds.remove(id);
    queue = [...queue, Map<String, dynamic>.from(track)];
    if (queue.length == 1) {
      index = 0;
      title = 'Your queue';
      hasMore = true;
    }
    _queueEdited();
    return true;
  }

  double get position => !_metadataReady ? _restoredPosition : audio.position;
  double get duration =>
      _metadataReady && audio.duration > 0 ? audio.duration : _knownDuration;
  PlaybackPhase get phase => current == null
      ? PlaybackPhase.idle
      : startupInterrupted
          ? PlaybackPhase.interrupted
          : error != null
              ? PlaybackPhase.failed
              : audio.phase;
  bool get playing => phase == PlaybackPhase.playing;
  bool get busy =>
      loading ||
      phase == PlaybackPhase.starting ||
      phase == PlaybackPhase.buffering;
  bool get canPause => playing || busy;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Map<String, dynamic> get diagnostics => {
        'startupMs': _lastStartupMs,
        'nextGapMs': _lastNextGapMs,
        'resumeMs': _lastResumeMs,
        'selectedTrack': extractTrackId(current),
        'loadedTrack': _loadedId,
        'queueIndex': index,
        'operation': _operation,
        'loading': loading,
        'error': error,
        'startupInterrupted': startupInterrupted,
        'awaitingFirstPlayback': _awaitingFirstPlayback,
        'hiddenDuringStartup': _hiddenDuringStartup,
        'playRequested': _wantsPlayback,
        'phase': phase.name,
        'recoveryAttempts': _resumeRecoveries,
      };

  void reconcile() {
    if (_disposed || current == null) return;
    if (_loadedId != null &&
        (_loadedId != audio.loadedTrackId ||
            _loadedId != extractTrackId(current) ||
            !audio.sourceMatches)) {
      _operation++;
      _loadedId = null;
      _metadataReady = false;
      loading = false;
      audio.pause();
      error = 'Playback state changed. Tap Play to resume this track.';
    }
    audio.syncMetadata(current!);
    save();
    _notify();
  }

  Future<Uri> _prepare(Map<String, dynamic> track, {bool activeQueue = false}) {
    final id = extractTrackId(track)!;
    final existing = _pending[id];
    if (existing != null) return existing;
    final scope = _preparationScope;
    late final Future<Uri> pending;
    pending = api.prepare(track, activeQueue: activeQueue).then((uri) {
      if (!_disposed && scope == _preparationScope) {
        final until = api.preparedUntil(id) ??
            api.clock().add(const Duration(minutes: 5));
        if (until.isAfter(api.clock().add(const Duration(seconds: 60)))) {
          _ready[id] = uri;
          _readyUntil[id] = until;
          if (api.preparedMedia(id) != null) save();
        } else {
          _prepareRetryAfter[id] = api.clock().add(const Duration(seconds: 30));
          _prepareRetryTimer?.cancel();
          _prepareRetryTimer = Timer(const Duration(seconds: 30), _primeNext);
        }
      }
      return uri;
    }).whenComplete(() {
      if (identical(_pending[id], pending)) _pending.remove(id);
    });
    _pending[id] = pending;
    return pending;
  }

  void _primeNext() {
    if (_disposed || busy || !_visible) return;
    final upcoming = queue.skip(index + 1).take(2).toList();
    for (final track in upcoming) {
      final id = extractTrackId(track)!;
      if (!(_readyUntil[id]
              ?.isAfter(api.clock().add(const Duration(seconds: 60))) ??
          false)) {
        _ready.remove(id);
        _readyUntil.remove(id);
      }
      if (_ready.containsKey(id) ||
          _queuePreparingIds[id] == _preparationScope ||
          (_prepareRetryAfter[id]?.isAfter(api.clock()) ?? false)) continue;
      if (_queuePreparations >= 2) break;
      _queuePreparingIds[id] = _preparationScope;
      _queuePreparations++;
      unawaited(_prepareQueueTrack(track));
    }
    // The backend continues full batches while this browser is suspended.
    // Each track is independent; a failed prefix cannot gate healthy neighbours.
    _syncQueueCache();
    if (_queuePreparations == 0 && _searchPreparation.isNotEmpty) {
      unawaited(_drainSearchPreparation());
    }
  }

  final Map<String, int> _queuePreparingIds = {};

  Future<void> _prepareQueueTrack(Map<String, dynamic> track) async {
    final id = extractTrackId(track)!;
    final scope = _preparationScope;
    try {
      await _prepare(track, activeQueue: true);
    } catch (_) {
      if (scope == _preparationScope && !_disposed) {
        _prepareRetryAfter[id] = api.clock().add(const Duration(seconds: 30));
        _prepareRetryTimer?.cancel();
        _prepareRetryTimer = Timer(const Duration(seconds: 30), _primeNext);
      }
    } finally {
      if (_queuePreparingIds[id] == scope) _queuePreparingIds.remove(id);
      _queuePreparations--;
      _primeNext();
    }
  }

  void _syncQueueCache() {
    if (_disposed || busy || current == null || !_wantsPlayback) return;
    if (_queueCacheRetryAfter?.isAfter(api.clock()) ?? false) return;
    final upcoming = queue.skip(index + 1).take(100).toList();
    final signature = upcoming.map(extractTrackId).join('|');
    if (signature == _queueCacheSignature) return;
    _queueCacheSignature = signature;
    final revision = ++_queueCacheRevision;
    unawaited(
        api.cacheQueue(_queueCacheToken, revision, upcoming).then((result) {
      if (_disposed || revision != _queueCacheRevision) return;
      final status = (result['queue_cache'] as Map?)?['status'];
      if (status == 'capacity' ||
          status == 'failed' ||
          status == 'provider_backoff') {
        _queueCacheSignature = '';
        _queueCacheRetryAfter = api.clock().add(const Duration(seconds: 30));
      }
    }).catchError((Object _) {
      if (!_disposed && revision == _queueCacheRevision) {
        _queueCacheSignature = '';
        _queueCacheRetryAfter = api.clock().add(const Duration(seconds: 30));
      }
    }));
  }

  Future<void> _cancelQueueCache() async {
    if (_queueCacheSignature.isEmpty) return;
    _queueCacheSignature = '';
    _queueCacheRetryAfter = null;
    try {
      await api.cacheQueue(_queueCacheToken, ++_queueCacheRevision, const [],
          cancel: true);
    } catch (_) {
      /* The bounded backend lease also expires abandoned work. */
    }
  }

  Future<void> start(Map<String, dynamic> track) async {
    final retain = _canRetain(track);
    _cancelQueueCache();
    _session++;
    _queueRevision++;
    _queueRemovedIds.clear();
    _transitionWatch = null;
    _discoverySession =
        '${DateTime.now().microsecondsSinceEpoch}-${_session.toRadixString(36)}';
    loadingRelated = false;
    relatedError = null;
    hasMore = true;
    if (!retain) {
      _loadedId = null;
      _metadataReady = false;
      _knownDuration = trackDurationSeconds(track).toDouble();
      _restoredPosition = 0;
    }
    queue = [Map<String, dynamic>.from(track)];
    index = 0;
    title = 'Inspired by ${track['title']}';
    if (retain) {
      audio.record('queue-context-replaced', {'sourceRetained': true});
      _queueEdited();
      unawaited(replenish());
      return;
    }
    _restoredPosition = 0;
    final playback = playIndex(0);
    unawaited(api.recordExplicitSearchSelection(track).catchError((_) {}));
    await playback;
    if (hasMore) unawaited(replenish());
  }

  Future<void> playIndex(int target,
      {bool resume = false,
      bool recovery = false,
      bool explicitRetry = false}) async {
    if (target < 0 || target >= queue.length) return;
    if (!resume && target == index && _canRetain(queue[target])) return;
    if (!resume) _resumeWatch = null;
    if (resume && _resumeWatch == null) _beginResume(automatic: recovery);
    // Stop speculative requests; the selected song and queue now take priority.
    _searchPreparation = [];
    if (!recovery) _resumeRecoveries = 0;
    _wantsPlayback = true;
    _resuming = false;
    final operation = ++_operation;
    if (resume && index == target && _metadataReady) {
      _restoredPosition = audio.position;
      if (audio.duration > 0) _knownDuration = audio.duration;
    }
    audio.pause();
    _startupWatch = Stopwatch()..start();
    _lastStartupMs = _lastNextGapMs = null;
    final track = queue[target];
    final id = extractTrackId(track)!;
    if (!resume || index != target) {
      _listeningEpoch++;
      _restoredPosition = 0;
      _knownDuration = trackDurationSeconds(track).toDouble();
    }
    _metadataReady = false;
    index = target;
    loading = true;
    error = null;
    startupInterrupted = false;
    _hiddenDuringStartup = false;
    _awaitingFirstPlayback = true;
    _loadedId = null;
    _notify();
    try {
      // Call play within the tap. The stream endpoint resolves/prepares audio;
      // awaiting an HTTP request here loses iPhone's first-play permission.
      if (explicitRetry) api.forgetPreparedMedia(id);
      final media = explicitRetry ? null : api.preparedMedia(id);
      final prepared = !explicitRetry &&
          (_readyUntil[id]
                  ?.isAfter(api.clock().add(const Duration(seconds: 60))) ??
              false);
      _usedPreparation = media != null || prepared && _ready.containsKey(id);
      final source = media?.uri ??
          (prepared
              ? _ready[id] ?? api.streamUri(track)
              : api.streamUri(track));
      final sessionUntil = api.clock().add(const Duration(minutes: 20));
      _loadedUntil = source.queryParameters.containsKey('cache_variant')
          ? null
          : prepared && _readyUntil[id]!.isBefore(sessionUntil)
              ? _readyUntil[id]
              : sessionUntil;
      final uri = media != null
          ? source
          : source.replace(queryParameters: {
              ...source.queryParameters,
              if (explicitRetry) 'retry_source': '1',
              'playback_session':
                  '${DateTime.now().microsecondsSinceEpoch}-$operation',
            });
      if (_disposed || operation != _operation) return;
      _loadedId = id;
      audio.load(uri, track, mimeType: media?.mimeType);
      final pending = audio.play();
      if (resume) _recordResumeRequest();
      await pending;
      if (_disposed || operation != _operation) return;
      if (_loadedId != id || startupInterrupted) return;
      startupInterrupted = false;
      _markPlayed(id);
      if (playing) _primeNext();
      if (queue.length - index <= 5 && hasMore) unawaited(replenish());
    } catch (e) {
      if (operation != _operation || _disposed) return;
      // A media error event may have already invalidated this loaded source.
      if (_loadedId != id) return;
      _resumeFailed(e is PlaybackStartException ? e.cause : 'start-failed');
      if (e is PlaybackStartException &&
          e.kind == PlaybackStartFailure.interrupted &&
          (e.cause == 'permission' || e.cause == 'abort')) {
        startupInterrupted = true;
        if (e.cause == 'permission') _wantsPlayback = false;
        error = e.cause == 'permission'
            ? 'Browser playback permission was interrupted. Tap Play to resume this track.'
            : 'Playback was interrupted while starting. Tap Play to resume this track.';
      } else {
        _ready.remove(id);
        api.forgetPreparedMedia(id);
        _wantsPlayback = false;
        _loadedId = null;
        _metadataReady = false;
        audio.pause();
        startupInterrupted = false;
        debugPrint('Browser playback start failed: ${e.runtimeType}');
        error = 'Playback could not start. Tap Play to retry.';
      }
    } finally {
      if (!_disposed && operation == _operation) {
        loading = false;
        if (playing) _primeNext();
        save();
        _notify();
      }
    }
  }

  void _beginResume({required bool automatic}) {
    _resumeWatch = Stopwatch()..start();
    _lastResumeMs = null;
    _resumeAutomatic = automatic;
    _resumeSourceRetained = _loadedId != null &&
        _loadedId == audio.loadedTrackId &&
        audio.sourceMatches &&
        !audio.needsNewSource &&
        (audio.fullyBuffered ||
            _loadedUntil == null ||
            _loadedUntil!
                .isAfter(api.clock().add(const Duration(seconds: 30))));
  }

  void _recordResumeRequest() {
    audio.record('resume-request', {
      'kind': _resumeAutomatic ? 'automatic' : 'explicit',
      'fullyBuffered': audio.fullyBuffered,
      'sourceRetained': _resumeSourceRetained,
    });
  }

  void _resumeFailed(String reason) {
    if (_resumeWatch == null) return;
    audio.record('resume-failed', {
      'reason': reason,
      'resumeMs': _resumeWatch!.elapsedMilliseconds,
      'failed': true,
    });
    _resumeWatch = null;
  }

  Future<void> play({bool automatic = false}) async {
    if (current == null) return;
    final retrySource = !automatic && phase == PlaybackPhase.failed;
    // Only identity validation belongs before native play. Reconciliation
    // also writes storage and metadata, which delays remote iOS commands.
    if (_loadedId != null &&
        (_loadedId != audio.loadedTrackId || !audio.sourceMatches)) {
      _operation++;
      _loadedId = null;
      _metadataReady = false;
      audio.pause();
    }
    if (!automatic) _resumeRecoveries = 0;
    _wantsPlayback = true;
    if (playing && !audio.paused) return;
    if (_resumeWatch == null) _beginResume(automatic: automatic);
    if (audio.needsNewSource ||
        (!audio.fullyBuffered &&
            _loadedUntil != null &&
            !_loadedUntil!
                .isAfter(api.clock().add(const Duration(seconds: 30))))) {
      audio.record('resume-new-session', {
        'reason': audio.needsNewSource ? 'source-invalidated' : 'lease-expired'
      });
      await playIndex(index,
          resume: true, recovery: automatic, explicitRetry: retrySource);
      return;
    }
    if (_loadedId == extractTrackId(current)) {
      final operation = ++_operation;
      final session = _session;
      final id = extractTrackId(current);
      error = null;
      startupInterrupted = false;
      _hiddenDuringStartup = false;
      _awaitingFirstPlayback = true;
      loading = true;
      _resuming = true;
      try {
        final pending = audio.play();
        _recordResumeRequest();
        _notify();
        await pending;
      } catch (e) {
        if (_disposed ||
            operation != _operation ||
            session != _session ||
            id != extractTrackId(current) ||
            _loadedId != id) return;
        _resumeFailed(e is PlaybackStartException ? e.cause : 'start-failed');
        if (e is PlaybackStartException &&
            e.kind == PlaybackStartFailure.interrupted &&
            (e.cause == 'permission' || e.cause == 'abort')) {
          startupInterrupted = true;
          if (e.cause == 'permission') _wantsPlayback = false;
          error = e.cause == 'permission'
              ? 'Browser playback permission was interrupted. Tap Play to resume this track.'
              : 'Playback was interrupted while starting. Tap Play to resume this track.';
        } else {
          startupInterrupted = false;
          _wantsPlayback = false;
          _loadedId = null;
          _metadataReady = false;
          audio.pause();
          error = 'Playback could not start. Tap Play to retry.';
        }
      }
      if (!_disposed && operation == _operation) {
        loading = audio.phase == PlaybackPhase.starting ||
            audio.phase == PlaybackPhase.buffering;
        save();
        _notify();
      }
    } else {
      await playIndex(index,
          resume: true, recovery: automatic, explicitRetry: retrySource);
    }
  }

  void pause() {
    _operation++;
    _startupWatch = _transitionWatch = null;
    if (_resumeWatch != null) {
      audio.record(
          'resume-cancelled', {'resumeMs': _resumeWatch!.elapsedMilliseconds});
      _resumeWatch = null;
    }
    _wantsPlayback = _resuming = false;
    _awaitingFirstPlayback = false;
    loading = false;
    audio.record('pause-request');
    audio.pause();
    save();
    _notify();
  }

  /// Records browser lifecycle evidence without changing the selected track.
  void lifecycle(String kind, {required bool visible}) {
    _visible = visible;
    if (visible) _primeNext();
    if (!visible && (loading || _awaitingFirstPlayback))
      _hiddenDuringStartup = true;
    if (visible && startupInterrupted && !loading) {
      error =
          'Playback was interrupted while starting. Tap Play to resume this track.';
      _notify();
    }
    if (visible &&
        _wantsPlayback &&
        !loading &&
        _loadedId != null &&
        (audio.phase == PlaybackPhase.interrupted || audio.paused)) {
      audio.record('foreground-resume', {'kind': kind});
      unawaited(play(automatic: true));
    }
  }

  void _recoverResume(String reason) {
    audio.record('resume-recovery', {
      'reason': reason,
      'resumeMs': _resumeWatch?.elapsedMilliseconds,
      'fullyBuffered': audio.fullyBuffered,
    });
    if (_metadataReady) {
      _restoredPosition = audio.position;
      if (audio.duration > 0) _knownDuration = audio.duration;
    }
    _operation++; // Fence any older Promise before renewing the media resource.
    loading = false;
    startupInterrupted = true;
    audio.pause();
    error = 'Audio did not resume. Tap Play to retry from this position.';
    if (_visible && _wantsPlayback && _resumeRecoveries < 2) {
      _resumeRecoveries++;
      final validLease = _loadedUntil == null ||
          _loadedUntil!.isAfter(api.clock().add(const Duration(seconds: 30)));
      if (_resumeRecoveries == 1 && !audio.needsNewSource && validLease) {
        _metadataReady = false;
        audio.record('resume-same-session', {'reason': reason});
        audio.reloadSource();
        unawaited(play(automatic: true));
      } else {
        _ready.remove(_loadedId);
        _metadataReady = false;
        audio.record('resume-source-renewal',
            {'reason': reason, 'recoveryAttempts': _resumeRecoveries});
        unawaited(playIndex(index, resume: true, recovery: true));
      }
    } else if (!_visible &&
        _wantsPlayback &&
        reason == 'media-network-error' &&
        !audio.needsNewSource) {
      // Preserve intent and the selected resource while the browser is hidden.
      // Foreground/native activation can retry; background timers cannot.
      audio.record('resume-recovery-deferred', {'reason': reason});
      save();
      _notify();
    } else {
      _loadedId = null;
      _wantsPlayback = false;
      audio.record('resume-recovery-failed', {'reason': reason});
      _resumeWatch = null;
      save();
      _notify();
    }
  }

  void seek(double seconds) {
    if (_metadataReady) {
      audio.seek(seconds);
    } else if (duration > 0) {
      _restoredPosition = seconds.clamp(0, duration);
    }
    save();
    _notify();
  }

  Future<void> next() => playIndex(index + 1);
  Future<void> previous() => playIndex(index - 1);

  Future<void> replenish() {
    if (loadingRelated && _relatedRequest != null) return _relatedRequest!;
    return _relatedRequest = _replenish();
  }

  Future<void> _replenish() async {
    if (loadingRelated || current == null) return;
    if (_discoverySession.isEmpty) {
      _discoverySession =
          '${DateTime.now().microsecondsSinceEpoch}-${_session.toRadixString(36)}';
    }
    final session = _session;
    loadingRelated = true;
    relatedError = null;
    _notify();
    try {
      final page = await api.related(current!, queue,
          discoverySession: _discoverySession,
          recentIds: List<String>.unmodifiable(_recentPlayed));
      if (_disposed || session != _session) return;
      final seen = queue.map(extractTrackId).toSet();
      queue = [
        ...queue,
        ...page.tracks
            .where((t) =>
                !_queueRemovedIds.contains(extractTrackId(t)) &&
                seen.add(extractTrackId(t)))
            .map((t) => Map<String, dynamic>.from(t))
      ];
      _queueRevision++;
      hasMore = page.hasMore;
      _primeNext();
      save();
    } catch (_) {
      if (session == _session)
        relatedError = 'Related songs could not load. Retry below.';
    } finally {
      if (!_disposed && session == _session) {
        loadingRelated = false;
        _notify();
      }
    }
  }

  void _markPlayed(String? id) {
    if (id == null || id.isEmpty) return;
    _recentPlayed.remove(id);
    _recentPlayed.add(id);
    if (_recentPlayed.length > 40) {
      _recentPlayed.removeRange(0, _recentPlayed.length - 40);
    }
  }

  void _onEvent(String event) {
    if (_loadedId == null && event != 'pause') return;
    if (event != 'pause' &&
        (_loadedId != audio.loadedTrackId ||
            _loadedId != extractTrackId(current) ||
            !audio.sourceMatches)) return;
    if ((event == 'session-active' || event == 'canplay') &&
        _wantsPlayback &&
        startupInterrupted &&
        !loading &&
        !_nativeResumeAttempted &&
        !audio.needsNewSource) {
      _nativeResumeAttempted = true;
      audio.record('native-interruption-resume', {'kind': event});
      unawaited(play(automatic: true));
      return;
    }
    if (event == 'resume-stalled' ||
        event == 'playback-stalled-timeout' ||
        event == 'media-network-error' && _wantsPlayback ||
        event == 'error' && _resuming && _wantsPlayback) {
      _recoverResume(event);
      return;
    }
    if (event == 'startup-stalled') {
      _resumeFailed('startup-stalled');
      _operation++;
      _ready.remove(_loadedId);
      _loadedId = null;
      _wantsPlayback = false;
      loading = false;
      startupInterrupted = true;
      audio.pause();
      error = 'This track has not started. Tap Play to retry.';
      save();
      _notify();
      return;
    }
    if (event == 'pause' &&
        audio.paused &&
        audio.phase == PlaybackPhase.paused &&
        !startupInterrupted) {
      _wantsPlayback = false;
    }
    if (audio.phase == PlaybackPhase.playing) {
      if (_resumeWatch != null) {
        _lastResumeMs = _resumeWatch!.elapsedMilliseconds;
        audio.record('resume-progress', {
          'resumeMs': _lastResumeMs,
          'fullyBuffered': audio.fullyBuffered,
        });
        _resumeWatch = null;
      }
      if (_startupWatch != null) {
        _lastStartupMs = _startupWatch!.elapsedMilliseconds;
        _lastNextGapMs = _transitionWatch?.elapsedMilliseconds;
        audio.record('playback-startup', {
          'startupMs': _lastStartupMs,
          if (_transitionWatch != null) 'nextGapMs': _lastNextGapMs,
          'prepared': _usedPreparation,
        });
        _startupWatch = _transitionWatch = null;
        // Learn a completed cached resource without putting HTTP before Play.
        // The existing preparation owner coalesces and scopes this lookup.
        if (!_usedPreparation && audio.position > .02 && current != null) {
          unawaited(_prepare(current!)
              .then<void>((_) {}, onError: (Object _, StackTrace __) {}));
        }
      }
      _awaitingFirstPlayback = false;
      _hiddenDuringStartup = false;
      _resuming = false;
      _nativeResumeAttempted = false;
      startupInterrupted = false;
      error = null;
      loading = false;
      _primeNext();
      _warmLyrics();
    }
    if (event == 'interrupted') {
      startupInterrupted = true;
      loading = false;
      error = 'Audio was interrupted. Tap Play to resume.';
      audio.record('playback-interrupted');
    }
    if (event == 'ended') {
      if (index + 1 < queue.length) _transitionWatch = Stopwatch()..start();
      unawaited(next());
    }
    if (event == 'loadedmetadata' && _loadedId != null) {
      _metadataReady = true;
      if (audio.duration > 0) _knownDuration = audio.duration;
      if (_restoredPosition > 0) audio.seek(_restoredPosition);
    }
    if (event == 'media-aborted') {
      if (_metadataReady && audio.position > 0)
        _restoredPosition = audio.position;
      _metadataReady = false;
      startupInterrupted = true;
      error =
          'Playback was interrupted while starting. Tap Play to resume this track.';
      loading = false;
      audio.pause();
      if (current != null) audio.syncMetadata(current!);
      save();
    } else if (event == 'error') {
      _resumeFailed('media-error');
      if (_metadataReady && audio.position > 0)
        _restoredPosition = audio.position;
      _metadataReady = false;
      _ready.remove(_loadedId);
      _loadedId = null;
      startupInterrupted = false;
      _wantsPlayback = false;
      error = 'The audio stream failed. Tap Play to retry.';
      loading = false;
      audio.pause();
      if (current != null) audio.syncMetadata(current!);
      save();
    }
    if (event == 'timeupdate' && position.toInt() != _lastSavedSecond)
      _primeNext();
    if (event == 'pause' ||
        event == 'ended' ||
        (event == 'timeupdate' && position.toInt() != _lastSavedSecond)) save();
    _notify();
  }

  bool _nativeResumeAttempted = false;

  void save() {
    if (queue.isEmpty) return;
    try {
      store.write(jsonEncode({
        'preparedMedia': api.savedPreparedMedia(),
        'version': 1,
        'queue': queue,
        'current': extractTrackId(current),
        'currentIndex': index,
        'removedIds': _queueRemovedIds.toList(),
        'title': title,
        'position': position,
        'duration': duration,
        'hasMore': hasMore,
        'discoverySession': _discoverySession,
        'recentPlayed': _recentPlayed,
      }));
      _restoredPosition = position;
      _knownDuration = duration;
      _lastSavedSecond = position.toInt();
    } catch (_) {
      storageError =
          'Browser storage is unavailable; this queue cannot survive a reload.';
    }
  }

  @override
  void dispose() {
    final cancelled = _cancelQueueCache();
    save();
    _disposed = true;
    _prepareRetryTimer?.cancel();
    _operation++;
    _session++;
    _subscription.cancel();
    audio.dispose();
    unawaited(cancelled.whenComplete(api.client.close));
    super.dispose();
  }
}
