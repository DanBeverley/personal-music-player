import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import '../logic/track_metadata.dart';
import 'music_api.dart';
import 'playback_port.dart';
import 'catalog_api.dart';

class WebPlayer extends ChangeNotifier {
  final progress = ValueNotifier<int>(0);
  String _checkpointStamp = '';
  int _checkpointRevision = 0;
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
  Map<String, dynamic>? _pendingSelection;
  Map<String, dynamic>? get pendingSelection => _pendingSelection;
  String? selectionError;
  bool get preparingSelection => _pendingSelection != null;
  String? _pendingIncumbentEvent;
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
  Uri? _loadedCachedUri;
  bool _failedResource = false;
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
  String _primedHorizon = '';
  String? _nextBufferedId;
  Uri? _nextBufferedUri;
  Future<void>? _nextBuffer;
  DateTime? _nextBufferRetryAt;
  int _nextBufferRevision = 0;
  DateTime? _queueCacheRetryAfter;
  DateTime? _queueCacheCheckedAt;
  Timer? _queueCachePoll;
  bool _queueCacheInFlight = false;
  final Map<String, DateTime> _prepareRetryAfter = {};
  Timer? _prepareRetryTimer;
  Stopwatch? _startupWatch, _transitionWatch;
  int? _startupPriority;
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
    api.preloadCachedMedia(tracks);
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
    api.preloadCachedMedia(queue.skip(index < 0 ? 0 : index));
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
            _checkpointStamp = '${saved['checkpointStamp'] ?? ''}';
            try {
              final rawCheckpoint = store is CheckpointSessionStore
                  ? (store as CheckpointSessionStore).readCheckpoint()
                  : null;
              final checkpoint =
                  rawCheckpoint == null ? null : jsonDecode(rawCheckpoint);
              if (_checkpointStamp.isNotEmpty &&
                  checkpoint is Map &&
                  checkpoint['stamp'] == _checkpointStamp &&
                  checkpoint['current'] == extractTrackId(current) &&
                  checkpoint['index'] == index) {
                final value = checkpoint['position'];
                final length = checkpoint['duration'];
                if (value is num && value.isFinite && value >= 0)
                  _restoredPosition = value.toDouble();
                if (length is num && length.isFinite && length > 0)
                  _knownDuration = length.toDouble();
              }
            } catch (_) {
              /* A bad checkpoint cannot invalidate a usable queue. */
            }
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
    _clearNextBuffer();
    _cancelSelection();
    selectionError = null;
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
    _loadedCachedUri = null;
    _failedResource = false;
    _lastStartupMs = _lastNextGapMs = null;
    _lastResumeMs = null;
    _resumeWatch = null;
    _discoverySession = '';
    _recentPlayed.clear();
    hasMore = true;
    store = nextStore;
    _restore();
    api.preloadCachedMedia(queue.skip(index < 0 ? 0 : index));
    _notify();
  }

  Future<void> startQueue(
          List<Map<String, dynamic>> tracks, int selected, String label) =>
      _startQueue(tracks, selected, label);

  Future<void> startArtistQueue(
    Map<String, dynamic> track,
    List<Map<String, dynamic>> preview,
    String artist, {
    Future<List<Map<String, dynamic>>> Function()? loadSongs,
    bool wrap = true,
  }) {
    String? source(Map<String, dynamic> item) =>
        extractPlaybackSourceId(item) ?? extractTrackId(item);
    final selected = source(track);
    if (selected == null || track['playable'] == false) return Future.value();
    List<Map<String, dynamic>> ordered(List<Map<String, dynamic>> songs) {
      final at = songs.indexWhere((item) => source(item) == selected);
      final seen = {selected};
      final candidates = wrap && at >= 0
          ? [...songs.skip(at + 1), ...songs.take(at)]
          : songs;
      return candidates.where((item) {
        final id = source(item);
        return id != null && item['playable'] != false && seen.add(id);
      }).toList();
    }

    Future<void> expand() async {
      final session = _session, revision = _queueRevision;
      try {
        final songs = await loadSongs!();
        if (_disposed || session != _session || revision != _queueRevision ||
            songs.isEmpty) return;
        final retained = queue.take(index + 1).toList();
        final existing = {for (final item in queue) source(item): item};
        final seen = retained.map(source).toSet();
        queue = [
          ...retained,
          for (final item in ordered(songs))
            if (seen.add(source(item))) existing[source(item)] ?? item,
        ];
        _queueEdited();
        if (audio.phase == PlaybackPhase.ended) unawaited(_advanceAfterEnd());
      } catch (_) {
        // The selected song and available artist tracks remain playable.
      }
    }

    return _startQueue([track, ...ordered(preview)], 0, artist,
        allowRelated: false,
        onCommitted: loadSongs == null ? null : () => unawaited(expand()));
  }

  Future<void> _startQueue(
      List<Map<String, dynamic>> tracks, int selected, String label,
      {bool relatedAfterPlay = false,
      bool allowRelated = true,
      VoidCallback? onCommitted}) async {
    if (selected < 0 || selected >= tracks.length) return;
    final nextQueue = tracks.map((t) => Map<String, dynamic>.from(t)).toList();
    final retainPending = preparingSelection &&
        _loadedId == extractTrackId(nextQueue[selected]) &&
        audio.loadedTrackId == _loadedId &&
        audio.sourceMatches;
    if (retainPending) _cancelSelection(restoreIncumbent: true);
    if (_shouldStage(nextQueue[selected])) {
      final committed = await _stageSelection(nextQueue[selected],
          select: () => _replaceQueue(nextQueue, selected, label, allowRelated));
      if (committed) {
        onCommitted?.call();
        if (hasMore && queue.length - index <= 5) unawaited(replenish());
        unawaited(api
            .recordExplicitSearchSelection(nextQueue[selected])
            .catchError((_) {}));
      }
      return;
    }
    _cancelSelection();
    selectionError = null;
    final retain = retainPending || _canRetain(tracks[selected]);
    _replaceQueue(nextQueue, selected, label, allowRelated);
    if (retain) {
      audio.record('queue-context-replaced', {'sourceRetained': true});
      _queueEdited();
      onCommitted?.call();
      if (hasMore && queue.length - index <= 5) unawaited(replenish());
      return;
    }
    final playback = playIndex(selected);
    final session = _session;
    onCommitted?.call();
    // Start native playback in the gesture before requesting recommendations.
    // Keep album/playlist order; related songs only extend the tail.
    if (hasMore && !relatedAfterPlay && queue.length - index <= 5)
      unawaited(replenish());
    unawaited(
        api.recordExplicitSearchSelection(tracks[selected]).catchError((_) {}));
    await playback;
    if (!_disposed && session == _session && relatedAfterPlay && hasMore) {
      unawaited(replenish());
    }
  }

  void _replaceQueue(
      List<Map<String, dynamic>> tracks, int selected, String label,
      bool allowRelated) {
    _clearNextBuffer();
    _cancelQueueCache();
    _session++;
    _queueRevision++;
    _queueRemovedIds.clear();
    _transitionWatch = null;
    queue = tracks;
    index = selected;
    title = label;
    _discoverySession =
        '${DateTime.now().microsecondsSinceEpoch}-${_session.toRadixString(36)}';
    loadingRelated = false;
    relatedError = null;
    hasMore = allowRelated;
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
    if (identical(queue[at], _pendingSelection) ||
        preparingSelection &&
            extractTrackId(queue[at]) == extractTrackId(_pendingSelection)) {
      _cancelSelection(restoreIncumbent: true);
    }
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

  /// Apply a chat placement together so the existing queue warms only once.
  bool placeUpcoming(List<Map<String, dynamic>> tracks, {
    required int revision,
    String? after,
    bool atEnd = false,
    bool anchorNext = false,
  }) {
    if (!_canEditQueue(index, revision) || tracks.isEmpty) return false;
    final currentId = extractTrackId(current);
    final moving = <String, Map<String, dynamic>>{};
    for (final track in tracks) {
      final id = extractTrackId(track);
      if (id == null || id == currentId || id == after || track['playable'] == false) {
        return false;
      }
      moving[id] = track;
    }
    final upcoming = queue.skip(index + 1).toList();
    final anchor = upcoming.where((track) => extractTrackId(track) == after).toList();
    if (after != null && anchor.length != 1) return false;
    for (final track in upcoming) {
      final id = extractTrackId(track);
      if (moving.containsKey(id)) moving[id!] = track;
    }
    upcoming.removeWhere((track) => moving.containsKey(extractTrackId(track)));
    if (anchorNext) {
      if (anchor.isEmpty) return false;
      upcoming.remove(anchor.single);
      upcoming.insert(0, anchor.single);
    }
    final offset = after != null
        ? upcoming.indexOf(anchor.single) + 1
        : atEnd ? upcoming.length : 0;
    upcoming.insertAll(offset, moving.values.map(Map<String, dynamic>.of));
    queue = [...queue.take(index + 1), ...upcoming];
    _queueRemovedIds.removeAll(moving.keys);
    _queueEdited();
    return true;
  }

  double get position => !_metadataReady ? _restoredPosition : audio.position;
  void _rememberPosition() {
    if (_loadedId != extractTrackId(current) ||
        audio.loadedTrackId != _loadedId ||
        !audio.sourceMatches) return;
    if (audio.position.isFinite && audio.position > 0)
      _restoredPosition = audio.position;
    if (audio.duration > 0) _knownDuration = audio.duration;
  }

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
      loading &&
          (!preparingSelection || audio.phase != PlaybackPhase.playing) ||
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
        'preparingSelection': preparingSelection,
        'mediaRoute': api.mediaBase.origin == api.base.origin
            ? 'shared_api'
            : 'separate_origin',
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
            !audio.sourceAssigned)) {
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
        final until = api.preparedUntil(id) ?? api.preparedMedia(id)?.deliveryUntil ??
            (api.preparedMedia(id)?.uri.queryParameters.containsKey('cache_version') == true
                ? api.clock()
                : api.clock().add(const Duration(minutes: 5)));
        if (until.isAfter(api.clock().add(const Duration(seconds: 60)))) {
          _ready[id] = uri;
          _readyUntil[id] = until;
          if (api.preparedMedia(id) != null) save();
        } else {
          _retryPreparation(id, const Duration(seconds: 30));
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
    if (_disposed || preparingSelection || busy) return;
    final upcoming = queue.skip(index + 1).take(5).toList();
    final horizon = '$_preparationScope:${extractTrackId(current)}:'
        '${upcoming.map(extractTrackId).join('|')}';
    final changed = horizon != _primedHorizon;
    if (changed) {
      _primedHorizon = horizon;
      api.preloadCachedMedia(upcoming, priority: true);
    }
    // Native playback events can refresh this window while hidden. No hidden
    // timer is required or relied on; the backend owns full-cache continuation.
    final wanted = [...upcoming,
      if (_visible) ...queue.take(index).toList().reversed.take(2).where(
          (track) => api.preparedMedia(extractTrackId(track)!) != null)];
    for (final track in wanted) {
      final id = extractTrackId(track)!;
      if (api.preparedMedia(id) != null && !api.startupPrepared(id)) {
        _ready.remove(id);
        _readyUntil.remove(id);
      }
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
    _syncQueueCache(nativeEvent: !changed && _queueCacheSignature.isNotEmpty);
    _primeNextBuffer();
    if (_queuePreparations == 0 && _searchPreparation.isNotEmpty) {
      unawaited(_drainSearchPreparation());
    }
  }

  final Map<String, int> _queuePreparingIds = {};

  void _retryPreparation(String id, Duration delay) {
    final now = api.clock();
    _prepareRetryAfter[id] = now.add(delay);
    final next = _prepareRetryAfter.values
        .where((until) => until.isAfter(now))
        .reduce((a, b) => a.isBefore(b) ? a : b);
    _prepareRetryTimer?.cancel();
    _prepareRetryTimer = Timer(next.difference(now), _primeNext);
  }

  Future<void> _prepareQueueTrack(Map<String, dynamic> track) async {
    final id = extractTrackId(track)!;
    final scope = _preparationScope;
    try {
      await _prepare(track, activeQueue: true);
    } catch (error) {
      if (scope == _preparationScope && !_disposed) {
        _retryPreparation(
            id,
            error is PlaybackPreparationException &&
                    error.code == 'preparation_busy'
                ? const Duration(seconds: 2)
                : const Duration(seconds: 30));
      }
    } finally {
      if (_queuePreparingIds[id] == scope) _queuePreparingIds.remove(id);
      _queuePreparations--;
      _primeNext();
    }
  }

  void _syncQueueCache({bool nativeEvent = false}) {
    if (nativeEvent && (_queueCacheInFlight ||
        (_queueCacheCheckedAt?.add(const Duration(seconds: 15))
            .isAfter(api.clock()) ?? false))) return;
    if (!nativeEvent) _queueCachePoll?.cancel();
    if (_disposed || current == null || !_wantsPlayback) return;
    // This is a foreground recovery/status check, not a background keepalive.
    if (_visible && !nativeEvent)
      _queueCachePoll = Timer(const Duration(seconds: 15), _syncQueueCache);
    if (busy || _queueCacheInFlight) return;
    if (_queueCacheRetryAfter?.isAfter(api.clock()) ?? false) return;
    final upcoming = queue.skip(index + 1).take(10).toList();
    if (upcoming.isEmpty) {
      unawaited(_cancelQueueCache());
      return;
    }
    final signature = upcoming.map(extractTrackId).join('|');
    if (signature == _queueCacheSignature &&
        (_queueCacheCheckedAt
                ?.add(const Duration(seconds: 15))
                .isAfter(api.clock()) ??
            false)) return;
    final unchanged = signature == _queueCacheSignature;
    if (unchanged && !_visible && !nativeEvent) return;
    if (!unchanged) {
      _queueCacheSignature = signature;
      _queueCacheRevision++;
    }
    _queueCacheCheckedAt = api.clock();
    _queueCacheInFlight = true;
    final revision = _queueCacheRevision;
    final scope = _preparationScope;
    unawaited((unchanged
            ? api.queueCacheStatus(_queueCacheToken, revision)
            : api.cacheQueue(_queueCacheToken, revision, upcoming))
        .then((result) {
      if (_disposed ||
          scope != _preparationScope ||
          revision != _queueCacheRevision) return;
      final status = (result['queue_cache'] as Map?)?['status'];
      _primeNextBuffer();
      if (status == 'missing') {
        _queueCacheSignature = '';
      } else if (status == 'capacity' ||
          status == 'failed' ||
          status == 'provider_backoff') {
        _queueCacheSignature = '';
        _queueCacheRetryAfter = api.clock().add(const Duration(seconds: 30));
      }
    }).catchError((Object _) {
      if (!_disposed &&
          scope == _preparationScope &&
          revision == _queueCacheRevision) {
        _queueCacheSignature = '';
        _queueCacheRetryAfter = api.clock().add(const Duration(seconds: 30));
      }
    }).whenComplete(() {
      _queueCacheInFlight = false;
      if (!_disposed &&
          queue.skip(index + 1).take(10).map(extractTrackId).join('|') !=
              _queueCacheSignature) _syncQueueCache();
    }));
  }

  Future<void> _cancelQueueCache() async {
    _queueCachePoll?.cancel();
    _queueCacheCheckedAt = null;
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

  Future<void> start(Map<String, dynamic> track) =>
      _startQueue([track], 0, 'Inspired by ${track['title']}',
          relatedAfterPlay: true);

  bool _shouldStage(Map<String, dynamic> track) =>
      audio.supportsStagedHandoff &&
      current != null &&
      _canRetain(current!) &&
      extractTrackId(track) != extractTrackId(current);

  void _clearNextBuffer({bool cancel = true}) {
    final owned = _nextBufferedId != null;
    _nextBufferRevision++;
    _nextBufferedId = null;
    _nextBufferedUri = null;
    _nextBuffer = null;
    _nextBufferRetryAt = null;
    if (owned && cancel) audio.cancelStaged();
  }

  void _primeNextBuffer() {
    if (_disposed || preparingSelection || busy || !audio.supportsStagedHandoff)
      return;
    if (index + 1 >= queue.length || !_wantsPlayback) {
      _clearNextBuffer();
      return;
    }
    final track = queue[index + 1], id = extractTrackId(queue[index + 1])!;
    final media = api.preparedMedia(id);
    // One real next-song element, in addition to backend next-five supply.
    // Do not compete with A by opening another cold provider stream.
    if (media == null || !media.uri.queryParameters.containsKey('cache_version')) {
      if (_nextBufferedId != id) _clearNextBuffer();
      return;
    }
    final uri = api.playbackUri(media.uri);
    if (_nextBufferedId == id && _nextBufferedUri == uri &&
        (_nextBuffer != null ||
          (_nextBufferRetryAt?.isAfter(api.clock()) ?? false))) return;
    if (!playing || audio.paused || !audio.sourceAssigned) return;
    _clearNextBuffer();
    final revision = _nextBufferRevision;
    _nextBufferedId = id;
    _nextBufferedUri = uri;
    final pending = audio.stage(uri, track, mimeType: media.mimeType);
    _nextBuffer = pending;
    unawaited(pending.then((_) {
      if (!_disposed && revision == _nextBufferRevision)
        audio.record('next-buffer-ready', {'requestedTrack': id});
    }, onError: (Object _, StackTrace __) {
      if (!_disposed && revision == _nextBufferRevision) {
        _nextBuffer = null;
        _nextBufferRetryAt = api.clock().add(const Duration(seconds: 30));
        audio.record('next-buffer-failed', {'requestedTrack': id});
      }
    }));
  }

  void _cancelSelection({bool restoreIncumbent = false}) {
    if (!preparingSelection) return;
    _operation++;
    _pendingSelection = null;
    loading = false;
    audio.cancelStaged();
    api.endPlaybackStartup(_startupPriority);
    _startupPriority = null;
    if (restoreIncumbent)
      _restoreIncumbent();
    else
      _pendingIncumbentEvent = null;
  }

  void _restoreIncumbent() {
    final event = _pendingIncumbentEvent;
    _pendingIncumbentEvent = null;
    if (_disposed ||
        current == null ||
        audio.phase == PlaybackPhase.playing && !audio.paused) return;
    if (event == 'ended' || audio.phase == PlaybackPhase.ended) {
      _wantsPlayback = false;
      selectionError =
          'The selected song could not load, and the previous song has ended.';
    } else if (event != null && event != 'interrupted') {
      _onEvent(event);
    } else if (_wantsPlayback && audio.paused) {
      unawaited(play(automatic: true));
    }
  }

  ({
    Uri uri,
    String? mimeType,
    DateTime? until,
    Uri? cachedUri,
    bool prepared,
    double? duration
  }) _selectSource(Map<String, dynamic> track, int operation,
      {bool explicitRetry = false, bool renewSource = false}) {
    final id = extractTrackId(track)!;
    final previous = api.preparedMedia(id);
    final renewing = explicitRetry || renewSource;
    if (renewing) {
      _ready.remove(id);
      _readyUntil.remove(id);
      api.forgetPreparedMedia(id);
    }
    final media = renewing
        ? (previous?.uri.queryParameters.containsKey('cache_version') == true
            ? previous
            : null)
        : previous;
    final prepared = !renewing &&
        (_readyUntil[id]
                ?.isAfter(api.clock().add(const Duration(seconds: 60))) ??
            false);
    final source = media?.uri ??
        (prepared ? _ready[id] ?? api.streamUri(track) : api.streamUri(track));
    final sessionUntil = api.clock().add(const Duration(minutes: 20));
    final directUntil = media?.deliveryUri != null &&
            media!.deliveryUntil!.isAfter(api.clock().add(const Duration(seconds: 60)))
        ? media.deliveryUntil
        : null;
    final until = directUntil ?? (source.queryParameters.containsKey('cache_variant')
        ? null
        : prepared && _readyUntil[id]!.isBefore(sessionUntil)
            ? _readyUntil[id]
            : sessionUntil);
    final uri = media != null && !renewing
        ? source
        : source.replace(queryParameters: {
            ...source.queryParameters,
            if (explicitRetry && media == null) 'retry_source': '1',
            'playback_session':
                '${DateTime.now().microsecondsSinceEpoch}-$operation',
          });
    return (
      uri: api.playbackUri(uri),
      mimeType: media?.mimeType,
      until: until,
      cachedUri: media?.uri,
      prepared: media != null || prepared && _ready.containsKey(id),
      duration: media?.duration
    );
  }

  Future<bool> _stageSelection(Map<String, dynamic> track,
      {required void Function() select, bool Function()? valid,
      bool automaticAdvance = false}) async {
    _cancelSelection();
    final operation = ++_operation;
    final watch = Stopwatch()..start();
    final source = _selectSource(track, operation);
    final buffered = _nextBufferedId == extractTrackId(track) &&
            _nextBufferedUri == source.uri ? _nextBuffer : null;
    _clearNextBuffer(cancel: buffered == null);
    final priority = _startupPriority = api.beginPlaybackStartup();
    _pendingSelection = Map.of(track);
    selectionError = null;
    _searchPreparation = [];
    unawaited(_cancelQueueCache());
    loading = true;
    try {
      // Only this selected replacement buffers; the active song/session remains
      // authoritative until native playback of the candidate can start.
      final pending = buffered ??
          audio.stage(source.uri, track, mimeType: source.mimeType);
      _notify();
      await pending;
      if (_disposed || operation != _operation) return false;
      if (valid?.call() == false) {
        _cancelSelection();
        return false;
      }
      await audio.commitStaged();
      if (_disposed || operation != _operation) return false;
      select();
      _pendingSelection = null;
      _pendingIncumbentEvent = null;
      _listeningEpoch++;
      _restoredPosition = 0;
      _knownDuration =
          source.duration ?? trackDurationSeconds(track).toDouble();
      _loadedId = extractTrackId(track);
      _loadedCachedUri = source.cachedUri;
      _loadedUntil = source.until;
      _metadataReady = true;
      _failedResource = false;
      _wantsPlayback = true;
      _resuming = startupInterrupted = _hiddenDuringStartup = false;
      _resumeRecoveries = 0;
      _resumeWatch = null;
      _lastResumeMs = null;
      _usedPreparation = source.prepared;
      _awaitingFirstPlayback = true;
      _startupWatch = watch;
      _lastStartupMs = _lastNextGapMs = null;
      error = null;
      audio
          .record('handoff-complete', {'elapsedMs': watch.elapsedMilliseconds});
      _markPlayed(_loadedId);
      return true;
    } catch (failure) {
      if (_disposed || operation != _operation) return false;
      audio.cancelStaged();
      _pendingSelection = null;
      selectionError = failure is PlaybackStartException &&
              failure.cause == 'permission'
          ? 'The browser could not switch songs. Tap the song again.'
          : 'The selected song could not load. Your current song is retained.';
      audio.record('handoff-failed', {
        'elapsedMs': watch.elapsedMilliseconds,
        'cause': failure is PlaybackStartException ? failure.cause : 'unknown'
      });
      if (automaticAdvance && _wantsPlayback &&
          failure is PlaybackStartException && failure.cause == 'stage-thin-buffer' &&
          audio.phase == PlaybackPhase.ended && queue.contains(track)) {
        _pendingIncumbentEvent = null;
        audio.record('hidden-handoff-direct-fallback');
        await playIndex(queue.indexOf(track), allowStaging: false);
        return false;
      }
      _restoreIncumbent();
      return false;
    } finally {
      api.endPlaybackStartup(priority);
      if (_startupPriority == priority) _startupPriority = null;
      if (!_disposed && operation == _operation) {
        loading = false;
        save();
        _notify();
      }
    }
  }

  Future<void> playIndex(int target,
      {bool resume = false,
      bool recovery = false,
      bool explicitRetry = false,
      bool renewSource = false,
      bool allowStaging = true,
      bool automaticAdvance = false}) async {
    if (target < 0 || target >= queue.length) return;
    if (!resume && target == index && preparingSelection) {
      _cancelSelection(restoreIncumbent: true);
      selectionError = null;
      _notify();
      return;
    }
    if (!resume && target == index && _canRetain(queue[target])) {
      _cancelSelection();
      selectionError = null;
      _notify();
      return;
    }
    if (!resume && allowStaging && (_shouldStage(queue[target]) ||
        _nextBufferedId == extractTrackId(queue[target]) && _nextBuffer != null)) {
      final selected = queue[target];
      await _stageSelection(selected,
          automaticAdvance: automaticAdvance,
          valid: () => queue.contains(selected),
          select: () => index = queue.indexOf(selected));
      return;
    }
    _cancelSelection();
    _clearNextBuffer();
    selectionError = null;
    if (!resume) {
      _resumeWatch = null;
      _lastResumeMs = null;
    }
    if (resume && _resumeWatch == null) _beginResume(automatic: recovery);
    // Stop speculative requests; the selected song and queue now take priority.
    _searchPreparation = [];
    if (!recovery) _resumeRecoveries = 0;
    _wantsPlayback = true;
    _resuming = false;
    final operation = ++_operation;
    if (resume && index == target && _metadataReady) {
      _rememberPosition();
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
    final priority = _startupPriority = api.beginPlaybackStartup();
    _notify();
    try {
      // Call play within the tap. The stream endpoint resolves/prepares audio;
      // awaiting an HTTP request here loses iPhone's first-play permission.
      final source = _selectSource(track, operation,
          explicitRetry: explicitRetry, renewSource: renewSource);
      if (source.duration != null) _knownDuration = source.duration!;
      _usedPreparation = source.prepared;
      _loadedUntil = source.until;
      if (_disposed || operation != _operation) return;
      _loadedId = id;
      _loadedCachedUri = source.cachedUri;
      _failedResource = false;
      audio.load(source.uri, track, mimeType: source.mimeType);
      if (resume && _restoredPosition > 0) audio.seek(_restoredPosition);
      final pending = audio.play();
      audio.record('playback-selection', {
        'preparedAtStart': _usedPreparation,
        'startupBytesReady': api.startupPrepared(id),
        'mediaRoute': api.mediaBase.origin == api.base.origin
            ? 'shared_api'
            : 'separate_origin',
      });
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
      api.endPlaybackStartup(priority);
      if (_startupPriority == priority) _startupPriority = null;
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
    if (preparingSelection) return;
    if (!automatic) selectionError = null;
    final retrySource = !automatic && phase == PlaybackPhase.failed;
    // Only identity validation belongs before native play. Reconciliation
    // also writes storage and metadata, which delays remote iOS commands.
    if (_loadedId != null &&
        (_loadedId != audio.loadedTrackId || !audio.sourceAssigned)) {
      _operation++;
      _loadedId = null;
      _metadataReady = false;
      audio.pause();
    }
    if (!automatic) _resumeRecoveries = 0;
    _wantsPlayback = true;
    if (playing && !audio.paused) return;
    if (_resumeWatch == null) _beginResume(automatic: automatic);
    if (automatic && _failedResource) {
      if (_resumeRecoveries >= 2) {
        _wantsPlayback = false;
        _resumeFailed('recovery-budget-exhausted');
        audio.record('resume-recovery-failed', {'reason': 'budget-exhausted'});
        save();
        _notify();
        return;
      }
      _resumeRecoveries++;
    }
    final id = extractTrackId(current);
    final media = id == null ? null : api.preparedMedia(id);
    if (_failedResource &&
        media != null &&
        media.uri.queryParameters.containsKey('cache_version') &&
        media.uri != _loadedCachedUri) {
      audio.record('resume-recovery-selection', {
        'reason': 'failed-resource',
        'choice': 'cached-replacement',
        'sourceVerified': true,
      });
      await playIndex(index, resume: true, recovery: automatic);
      return;
    }
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
      _rememberPosition();
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
    _clearNextBuffer();
    _cancelSelection();
    api.endPlaybackStartup(_startupPriority);
    _startupPriority = null;
    _operation++;
    _startupWatch = _transitionWatch = null;
    if (_resumeWatch != null) {
      audio.record(
          'resume-cancelled', {'resumeMs': _resumeWatch!.elapsedMilliseconds});
      _resumeWatch = null;
    }
    _wantsPlayback = _resuming = false;
    _queueCachePoll?.cancel();
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
    _queueCachePoll?.cancel();
    if (visible) {
      _queueCacheCheckedAt = null;
      _primedHorizon = '';
    }
    if (visible) {
      _rememberPosition();
      progress.value++;
      _primeNext();
    }
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
    if (!_wantsPlayback) return;
    final id = extractTrackId(current);
    final media = id == null ? null : api.preparedMedia(id);
    final verified =
        media?.uri.queryParameters.containsKey('cache_version') == true;
    final failedResource = const {
      'media-network-error',
      'media-source-error',
      'resume-stalled',
      'playback-stalled-timeout',
    }.contains(reason);
    final cachedReplacement =
        verified && media!.uri != _loadedCachedUri && failedResource;
    final frozenResume = reason == 'resume-stalled' && audio.fullyBuffered;
    final maxRecoveries = frozenResume ? 1 : 2;
    final attemptReplacement = cachedReplacement &&
        _wantsPlayback &&
        _resumeRecoveries < maxRecoveries &&
        (_visible || _resumeRecoveries == 0);
    _failedResource = failedResource;
    audio.record('resume-recovery', {
      'reason': reason,
      'resumeMs': _resumeWatch?.elapsedMilliseconds,
      'fullyBuffered': audio.fullyBuffered,
      'sourceVerified': verified,
      'choice': attemptReplacement ? 'cached-replacement' : 'existing-source',
    });
    _rememberPosition();
    _metadataReady = false;
    _operation++; // Fence any older Promise before renewing the media resource.
    loading = false;
    startupInterrupted = true;
    audio.pause();
    error = 'Audio did not resume. Tap Play to retry from this position.';
    if (_wantsPlayback &&
        _resumeRecoveries < maxRecoveries &&
        (_visible || _resumeRecoveries == 0 &&
            (frozenResume || cachedReplacement || reason == 'media-network-error' ||
                reason == 'media-source-error'))) {
      _resumeRecoveries++;
      final validLease = _loadedUntil == null ||
          _loadedUntil!.isAfter(api.clock().add(const Duration(seconds: 30)));
      if (cachedReplacement) {
        unawaited(playIndex(index, resume: true, recovery: true));
      } else if (_resumeRecoveries == 1 &&
          !audio.needsNewSource &&
          validLease) {
        _metadataReady = false;
        audio.record('resume-same-session', {'reason': reason});
        audio.reloadSource();
        _failedResource = false; // This retry was counted before reloading.
        unawaited(play(automatic: true));
      } else {
        _metadataReady = false;
        audio.record('resume-source-renewal',
            {'reason': reason, 'recoveryAttempts': _resumeRecoveries});
        unawaited(
            playIndex(index, resume: true, recovery: true, renewSource: true));
      }
    } else if (!_visible &&
        _wantsPlayback &&
        (reason == 'media-network-error' || reason == 'media-source-error') &&
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
    if (!seconds.isFinite || seconds < 0) return;
    _restoredPosition = duration > 0 ? seconds.clamp(0, duration) : seconds;
    if (_loadedId != null &&
        _loadedId == extractTrackId(current) &&
        audio.loadedTrackId == _loadedId &&
        audio.sourceMatches) audio.seek(_restoredPosition);
    // One explicit seek establishes the authoritative saved position. Normal
    // playback ticks retain the lightweight checkpoint path.
    save();
    _notify();
  }

  int get _selectionIndex => _pendingSelection == null
      ? index
      : queue.indexWhere(
          (t) => extractTrackId(t) == extractTrackId(_pendingSelection));
  Future<void> next() =>
      playIndex((_selectionIndex < 0 ? index : _selectionIndex) + 1);
  Future<void> previous() =>
      playIndex((_selectionIndex < 0 ? index : _selectionIndex) - 1);

  Future<void> _advanceAfterEnd() async {
    if (!_wantsPlayback) return;
    final session = _session, operation = _operation, endedIndex = index;
    if (index + 1 >= queue.length && hasMore) await replenish();
    if (_disposed || !_wantsPlayback || session != _session ||
        operation != _operation || index != endedIndex || index + 1 >= queue.length) return;
    _transitionWatch = Stopwatch()..start();
    await playIndex(index + 1, automaticAdvance: true);
  }

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
    if (preparingSelection) {
      // Incumbent progress still belongs to A. It must not complete B's startup,
      // publish B's history, or auto-next over the user's pending selection.
      if (_metadataReady) _rememberPosition();
      if (const {
        'ended',
        'error',
        'media-network-error',
        'media-source-error',
        'media-aborted',
        'interrupted',
        'source-failure',
        'resume-stalled',
        'playback-stalled-timeout'
      }.contains(event)) _pendingIncumbentEvent = event;
      if (event == 'timeupdate' || event == 'progress') {
        if (event == 'timeupdate' && position.toInt() != _lastSavedSecond)
          save(positionOnly: true);
        progress.value++;
      } else {
        _notify();
      }
      return;
    }
    if (event == 'source-reloaded') {
      audio.seek(_restoredPosition);
      return;
    }
    if (event == 'source-reloading') {
      _rememberPosition();
      _metadataReady = false;
      return;
    }
    if (_metadataReady) _rememberPosition();
    final providerDenied = const {
      'source_blocked',
      'video_unavailable',
      'format_unavailable'
    }.contains(audio.sourceFailureCode);
    if (const {
          'source-failure',
          'media-source-error',
          'media-network-error',
          'error',
          'media-aborted'
        }.contains(event) &&
        audio.sourceFailureCode != null &&
        (error != null || providerDenied && (loading || _wantsPlayback)) &&
        audio.loadedTrackId == extractTrackId(current) &&
        audio.sourceMatches) {
      if (audio.sourceFailureCode == 'playback_source_expired') {
        final id = extractTrackId(current);
        if (id != null) {
          api.forgetPreparedMedia(id);
          _ready.remove(id);
          _readyUntil.remove(id);
        }
      }
      startupInterrupted = false;
      loading = false;
      _wantsPlayback = false;
      _awaitingFirstPlayback = false;
      // A correlated server failure may precede the native Play rejection.
      // Invalidate that pending operation/recovery before it can overwrite the
      // reason or retry a recording the provider already rejected.
      _operation++;
      api.endPlaybackStartup(_startupPriority);
      _startupPriority = null;
      error = switch (audio.sourceFailureCode) {
        'source_blocked' =>
          'The music provider is blocking this track. Try again later.',
        'video_unavailable' => 'This recording is unavailable.',
        'format_unavailable' =>
          'No supported audio is available for this recording.',
        'preparation_busy' => 'Audio preparation is busy. Tap Play to retry.',
        'preparation_timeout' =>
          'Audio preparation timed out. Tap Play to retry.',
        _ => 'This audio source could not be loaded. Tap Play to retry.',
      };
      audio.pause();
      save();
      _notify();
      return;
    }
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
        event == 'media-source-error' && _wantsPlayback ||
        event == 'error' &&
            _resuming &&
            _wantsPlayback &&
            !audio.needsNewSource) {
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
      _failedResource = false;
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
        if (audio.position > .02 &&
            current != null &&
            (!_usedPreparation ||
                api.needsRepresentationRefresh(extractTrackId(current)!))) {
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
      unawaited(_advanceAfterEnd());
    }
    if ((event == 'loadedmetadata' || event == 'durationchange') &&
        _loadedId != null) {
      if (event == 'loadedmetadata') _metadataReady = true;
      if (audio.duration > 0) {
        _knownDuration = audio.duration;
        if (event == 'loadedmetadata')
          audio.record('source-duration', {
            'catalogueDuration': trackDurationSeconds(current!).toDouble(),
            'sourceDuration': audio.duration,
            'durationDeltaSeconds':
                audio.duration - trackDurationSeconds(current!),
          });
      }
      if (event == 'loadedmetadata' &&
          _restoredPosition > 0 &&
          (audio.position - _restoredPosition).abs() > .05)
        audio.seek(_restoredPosition);
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
        (event == 'timeupdate' && position.toInt() != _lastSavedSecond))
      save(positionOnly: event == 'timeupdate');
    if (event == 'timeupdate' || event == 'progress') {
      progress.value++;
    } else {
      _notify();
    }
  }

  bool _nativeResumeAttempted = false;

  void save({bool positionOnly = false}) {
    if (queue.isEmpty) return;
    try {
      if (positionOnly &&
          _checkpointStamp.isNotEmpty &&
          store is CheckpointSessionStore &&
          (store as CheckpointSessionStore).writeCheckpoint(jsonEncode({
            'stamp': _checkpointStamp,
            'current': extractTrackId(current),
            'index': index,
            'position': position,
            'duration': duration,
          }))) {
        _restoredPosition = position;
        _knownDuration = duration;
        _lastSavedSecond = position.toInt();
        return;
      }
      final stamp =
          '${DateTime.now().microsecondsSinceEpoch}:${++_checkpointRevision}';
      store.write(jsonEncode({
        'checkpointStamp': stamp,
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
      _checkpointStamp = stamp;
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
    _clearNextBuffer();
    _cancelSelection();
    api.endPlaybackStartup(_startupPriority, resume: false);
    _startupPriority = null;
    final cancelled = _cancelQueueCache();
    save();
    _disposed = true;
    _prepareRetryTimer?.cancel();
    _queueCachePoll?.cancel();
    _operation++;
    _session++;
    _subscription.cancel();
    progress.dispose();
    audio.dispose();
    unawaited(cancelled.whenComplete(api.client.close));
    super.dispose();
  }
}
