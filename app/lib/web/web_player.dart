import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import '../logic/track_metadata.dart';
import 'music_api.dart';
import 'playback_port.dart';

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
  final Map<String, Future<Uri>> _pending = {};
  List<Map<String, dynamic>> _searchPreparation = [];
  bool _preparingSearch = false;
  int _queuePreparations = 0;
  int _preparationScope = 0;
  bool _cachingFullTrack = false;
  final Map<String, DateTime> _prepareRetryAfter = {};
  Timer? _prepareRetryTimer;
  Stopwatch? _startupWatch, _transitionWatch;
  final Set<String> _queueCacheAttempts = {};
  String _discoverySession = '';
  final List<String> _recentPlayed = [];

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
          queue = MusicApi.tracks(saved['queue']);
          final savedIndex =
              queue.indexWhere((t) => extractTrackId(t) == saved['current']);
          if (savedIndex >= 0) {
            index = savedIndex;
            title = saved['title']?.toString() ?? title;
            _restoredPosition = (saved['position'] as num? ?? 0).toDouble();
            _knownDuration =
                (saved['duration'] as num? ?? trackDurationSeconds(current!))
                    .toDouble();
            hasMore = saved['hasMore'] != false;
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
    save();
    audio.pause();
    _operation++;
    _session++;
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
    _pending.clear();
    _prepareRetryAfter.clear();
    _prepareRetryTimer?.cancel();
    _startupWatch = _transitionWatch = null;
    _ready.clear();
    _queueCacheAttempts.clear();
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
    _session++;
    _queueCacheAttempts.clear();
    _transitionWatch = null;
    queue = List.of(tracks);
    index = selected;
    title = label;
    _discoverySession = '';
    loadingRelated = false;
    relatedError = null;
    hasMore = false;
    final playback = playIndex(selected);
    unawaited(
        api.recordExplicitSearchSelection(tracks[selected]).catchError((_) {}));
    await playback;
  }

  Map<String, dynamic>? get current => queue.isEmpty ? null : queue[index];
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
      if (!_disposed && scope == _preparationScope) _ready[id] = uri;
      return uri;
    }).whenComplete(() {
      if (identical(_pending[id], pending)) _pending.remove(id);
    });
    _pending[id] = pending;
    return pending;
  }

  void _primeNext() {
    if (_disposed || busy) return;
    final upcoming = queue.skip(index + 1).take(2).toList();
    for (final track in upcoming) {
      final id = extractTrackId(track)!;
      if (_ready.containsKey(id) ||
          _queuePreparingIds[id] == _preparationScope ||
          (_prepareRetryAfter[id]?.isAfter(DateTime.now()) ?? false)) continue;
      if (_queuePreparations >= 2) break;
      _queuePreparingIds[id] = _preparationScope;
      _queuePreparations++;
      unawaited(_prepareQueueTrack(track));
    }
    // Startup buffers have priority. Full downloads use a separate, single
    // background request, so they cannot hold the queue preparation pump.
    if (_queuePreparations == 0 &&
        upcoming.every((t) => _ready.containsKey(extractTrackId(t)))) {
      unawaited(_cacheQueue(upcoming));
    }
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
        _prepareRetryAfter[id] =
            DateTime.now().add(const Duration(seconds: 30));
        _prepareRetryTimer?.cancel();
        _prepareRetryTimer = Timer(const Duration(seconds: 30), _primeNext);
      }
    } finally {
      if (_queuePreparingIds[id] == scope) _queuePreparingIds.remove(id);
      _queuePreparations--;
      _primeNext();
    }
  }

  Future<void> _cacheQueue(List<Map<String, dynamic>> upcoming) async {
    if (_cachingFullTrack || _disposed || busy) return;
    final candidates = upcoming
        .where((track) => !_queueCacheAttempts.contains(extractTrackId(track)));
    if (candidates.isEmpty) return;
    final track = candidates.first;
    final id = extractTrackId(track)!;
    _queueCacheAttempts.add(id);
    _cachingFullTrack = true;
    try {
      await api.prepare(track, cacheFull: true);
    } catch (_) {
      /* A background cache failure must not invalidate the prefix. */
    } finally {
      _cachingFullTrack = false;
      _primeNext();
    }
  }

  Future<void> start(Map<String, dynamic> track) async {
    _session++;
    _transitionWatch = null;
    _discoverySession =
        '${DateTime.now().microsecondsSinceEpoch}-${_session.toRadixString(36)}';
    _queueCacheAttempts.clear();
    loadingRelated = false;
    relatedError = null;
    hasMore = true;
    _loadedId = null;
    _metadataReady = false;
    _knownDuration = trackDurationSeconds(track).toDouble();
    _restoredPosition = 0;
    queue = [track];
    index = 0;
    title = 'Inspired by ${track['title']}';
    _restoredPosition = 0;
    final playback = playIndex(0);
    unawaited(api.recordExplicitSearchSelection(track).catchError((_) {}));
    await playback;
    if (hasMore) unawaited(replenish());
  }

  Future<void> playIndex(int target,
      {bool resume = false, bool recovery = false}) async {
    if (target < 0 || target >= queue.length) return;
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
      final source = _ready[id] ?? api.streamUri(track);
      final uri = source.replace(queryParameters: {
        ...source.queryParameters,
        'playback_session':
            '${DateTime.now().microsecondsSinceEpoch}-$operation',
      });
      if (_disposed || operation != _operation) return;
      _loadedId = id;
      audio.load(uri, track);
      await audio.play();
      if (_disposed || operation != _operation) return;
      if (_loadedId != id || startupInterrupted) return;
      startupInterrupted = false;
      _markPlayed(id);
      if (playing) _primeNext();
      if (queue.length - index <= 3 && hasMore) unawaited(replenish());
    } catch (e) {
      if (operation != _operation || _disposed) return;
      // A media error event may have already invalidated this loaded source.
      if (_loadedId != id) return;
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

  Future<void> play({bool automatic = false}) async {
    if (current == null) return;
    reconcile();
    if (!automatic) _resumeRecoveries = 0;
    _wantsPlayback = true;
    if (playing && !audio.paused) return;
    if (audio.needsNewSource) {
      audio.record('resume-new-session', {'reason': 'source-invalidated'});
      await playIndex(index, resume: true, recovery: automatic);
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
      _notify();
      try {
        await audio.play();
      } catch (e) {
        if (_disposed ||
            operation != _operation ||
            session != _session ||
            id != extractTrackId(current) ||
            _loadedId != id) return;
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
      await playIndex(index, resume: true, recovery: automatic);
    }
  }

  void pause() {
    _operation++;
    _startupWatch = _transitionWatch = null;
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
    if (_metadataReady) {
      _restoredPosition = audio.position;
      if (audio.duration > 0) _knownDuration = audio.duration;
    }
    _operation++; // Fence any older Promise before renewing the media resource.
    _ready.remove(_loadedId);
    _metadataReady = false;
    loading = false;
    startupInterrupted = true;
    audio.pause();
    error = 'Audio did not resume. Tap Play to retry from this position.';
    if (_visible && _wantsPlayback && _resumeRecoveries == 0) {
      _resumeRecoveries++;
      audio.record('resume-source-renewal',
          {'reason': reason, 'recoveryAttempts': _resumeRecoveries});
      unawaited(playIndex(index, resume: true, recovery: true));
    } else {
      _loadedId = null;
      _wantsPlayback = false;
      audio.record('resume-recovery-failed', {'reason': reason});
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

  Future<void> replenish() async {
    if (loadingRelated || current == null) return;
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
        ...page.tracks.where((t) => seen.add(extractTrackId(t)))
      ];
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
    if (event == 'resume-stalled' ||
        event == 'playback-stalled-timeout' ||
        event == 'error' && _resuming && _wantsPlayback) {
      _recoverResume(event);
      return;
    }
    if (event == 'startup-stalled') {
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
      if (_startupWatch != null) {
        audio.record('playback-startup', {
          'startupMs': _startupWatch!.elapsedMilliseconds,
          if (_transitionWatch != null)
            'nextGapMs': _transitionWatch!.elapsedMilliseconds,
          'prepared': _ready.containsKey(_loadedId),
        });
        _startupWatch = _transitionWatch = null;
      }
      _awaitingFirstPlayback = false;
      _hiddenDuringStartup = false;
      _resuming = false;
      startupInterrupted = false;
      error = null;
      loading = false;
      _primeNext();
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
    if (event == 'pause' ||
        event == 'ended' ||
        (event == 'timeupdate' && position.toInt() != _lastSavedSecond)) save();
    _notify();
  }

  void save() {
    if (queue.isEmpty) return;
    try {
      store.write(jsonEncode({
        'version': 1,
        'queue': queue,
        'current': extractTrackId(current),
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
    save();
    _disposed = true;
    _prepareRetryTimer?.cancel();
    _operation++;
    _session++;
    _subscription.cancel();
    audio.dispose();
    api.client.close();
    super.dispose();
  }
}
