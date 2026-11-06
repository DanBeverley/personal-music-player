import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'package:web/web.dart' as web;
import 'playback_port.dart';
import 'playback_diagnostics.dart';
import 'diagnostic_archive.dart';
import '../logic/track_metadata.dart';

extension type _MediaAction(JSObject _) implements JSObject {
  external double? get seekTime;
  external double? get seekOffset;
}

extension type _AudioNavigator(JSObject _) implements JSObject {
  external _AudioSession? get audioSession;
}

extension type _AudioSession(JSObject _) implements JSObject {
  external set type(String value);
  external String get type;
  external String get state;
  external set onstatechange(JSFunction? value);
}

extension type _DiagnosticDocument(JSObject _) implements JSObject {
  external bool? get wasDiscarded;
}

class BrowserSessionStore implements CheckpointSessionStore {
  final String key;
  BrowserSessionStore(String backend, {String scope = 'guest'})
      : key = 'neatie:$scope:session:$backend';
  @override
  String? read() => web.window.localStorage.getItem(key);
  @override
  void write(String value) => web.window.localStorage.setItem(key, value);
  @override
  String? readCheckpoint() => web.window.localStorage.getItem('$key:position');
  @override
  bool writeCheckpoint(String value) {
    web.window.localStorage.setItem('$key:position', value);
    return true;
  }
}

class _StagedAudio {
  _StagedAudio(this.element, this.uri, this.track, this.traceId);
  final web.HTMLAudioElement element;
  final Uri uri;
  final Map<String, dynamic> track;
  final String traceId;
  final watch = Stopwatch()..start();
  final start = web.window.performance.now();
  int? metadataMs, readyMs, playCallMs, playAcceptedMs;
  web.HTMLSourceElement? source;
  String? declaredMediaType;
  final sourceFailed = Completer<void>();
  final cancelled = Completer<void>();
  Completer<void>? ready;
  Timer? timer;
  JSFunction? metadata, canPlay, error;
}

/// One active media session, with at most one silent preloaded replacement.
class BrowserAudio extends PlaybackPort {
  final List<String> Function(Map<String, dynamic>)? artworkUrls;
  Map<String, dynamic> _metadataTrack = const {};
  String _artworkKey = '', _artworkUrl = '';
  int _artworkEpoch = 0;
  bool _artworkPending = false;
  DateTime? _artworkAttemptedAt;
  void Function()? _cancelArtwork;
  web.HTMLAudioElement element = web.HTMLAudioElement();
  _StagedAudio? _staged;
  _StagedAudio? _adopting;
  bool _handoffEventsSuppressed = false;
  bool _disposed = false;
  final _events = StreamController<String>.broadcast(sync: true);
  final Map<String, JSFunction> _listeners = {};
  final List<String> _actions = [];
  final _progress = PlaybackProgress();
  DiagnosticArchive? _archive;
  late final _reports = PlaybackDiagnostics({
    'userAgent': web.window.navigator.userAgent,
    'platform': web.window.navigator.platform,
    'language': web.window.navigator.language,
    'navigationType': _navigationType(),
    'pageWasDiscarded': _DiagnosticDocument(web.document).wasDiscarded,
  }, onArchive: (row) => _archive?.add(row));
  Timer? _watchdog;
  DateTime? _loadedAt, _pausedAt, _lastProgressAt, _deadline;
  bool _everAdvanced = false, _resume = false, _renewSource = false;
  bool _foreground = !web.document.hidden;
  int _attempt = 0, _pauseDurationMs = 0;
  String _diagnosticKey = '';
  _AudioSession? _audioSession;
  String _track = '';
  String _session = '';
  String _traceId = '', _title = '', _artist = '';
  int _diagnosticEpoch = 0;
  final Set<String> _tracePending = {};
  final Map<String, int> _traceReads = {};
  final _pageInstance = web.window.crypto.randomUUID().replaceAll('-', '');
  final _pageWatch = Stopwatch()..start();
  final Map<String, JSFunction> _pageListeners = {};
  final _observationClock = PlaybackObservationClock();
  DateTime? _hiddenAt;
  String _hiddenTrace = '', _deliveryTraceStatus = '';
  double _hiddenPosition = 0;
  Timer? _diagnosticSave;
  String _expectedSource = '';
  String? _sourceFailureCode;
  web.HTMLSourceElement? _typedSource;
  bool _sourceElementFailed = false;
  bool _abortedBeforeMetadata = false;
  web.PerformanceObserver? _resourceObserver;
  final Map<String, List<web.PerformanceResourceTiming>> _observedResources =
      {};
  final Map<String, ({String source, double start, double? end})>
      _resourceAttempts = {};
  final Map<String, Set<double>> _recordedResourceStarts = {};
  Stopwatch? _recoveryWatch;
  String? _declaredMediaType;
  Stopwatch? _loadWatch;
  int? _loadStartMs, _metadataMs, _canPlayMs, _playingEventMs;
  int? _nativePlayCallMs, _playAcceptedMs, _firstProgressMs;
  int _startupWaitCount = 0;
  int _sourceAssignments = 0, _sourceReloads = 0;
  double _performanceStart = 0;
  double _expectedDuration = 0;
  Map<String, dynamic> Function()? diagnosticContext;
  void Function(String)? onReconcile;
  @override
  String? get loadedTrackId => _track.isEmpty ? null : _track;
  @override
  bool get sourceAssigned =>
      _expectedSource.isNotEmpty &&
      (_typedSource?.src ?? element.src) == _expectedSource;
  @override
  bool get sourceMatches => sourceAssigned &&
      (element.currentSrc.isEmpty || element.currentSrc == _expectedSource);
  @override
  PlaybackPhase get phase => _progress.phase;
  @override
  bool get needsNewSource => _renewSource;
  @override
  String? get sourceFailureCode => _sourceFailureCode;
  @override
  bool get fullyBuffered {
    if (!sourceMatches || element.error != null || duration <= 0) return false;
    for (var i = 0; i < element.buffered.length; i++) {
      if (element.buffered.start(i) <= position &&
          element.buffered.end(i) >= duration - .1) return true;
    }
    return false;
  }

  void Function()? beforeDiagnosticCapture;

  // The signed disposition carries safe representation identity. Never copy
  // the capability URL, credentials or signature into a report.
  Map<String, String> get _directIdentity {
    final uri = Uri.tryParse(_expectedSource);
    final version = RegExp(r'^inline; filename="(v1-[a-f0-9]{32})\.(aac-v1|opus-v1)"$')
        .firstMatch(uri?.queryParameters['response-content-disposition'] ?? '');
    final source = RegExp(r'/([A-Za-z0-9_-]+)\.bin\.(aac-v1|opus-v1)$')
        .firstMatch(uri?.path ?? '');
    return version == null || source == null ? const {} :
        {'sourceId': source[1]!, 'cacheVersion': version[1]!, 'cacheVariant': version[2]!};
  }

  Map<String, dynamic> diagnosticReport(
      {bool current = false, bool full = true}) {
    beforeDiagnosticCapture?.call();
    _readDelivery(_traceId, _expectedSource, _diagnosticEpoch);
    final report = _reports.report(current: current, full: full);
    _persistFailures(immediate: true);
    return report;
  }

  Future<Map<String, dynamic>> refreshDiagnosticReport(
      {bool current = true, bool fullHistory = false}) async {
    beforeDiagnosticCapture?.call();
    final epoch = _diagnosticEpoch;
    final archive = _archive;
    await _readDelivery(_traceId, _expectedSource, _diagnosticEpoch);
    if (epoch != _diagnosticEpoch) return {'reportId': 'account-changed'};
    var report = _reports.report(current: current, full: false);
    _persistFailures(immediate: true);
    if (archive != null) {
      final exported = await archive.export(fullHistory: fullHistory);
      // Rebuild the bounded summary from the selected run. Historical saved
      // incidents must not leak into a current-run report after page reload.
      final selected =
          PlaybackDiagnostics(report['device'] as Map<String, dynamic>);
      var replayed = 0;
      for (final row in '${exported['data'] ?? ''}'.split('\n')) {
        // Replaying a long archive is user-initiated work, not one blocking
        // media/UI task. Preserve every row while yielding between batches.
        if (++replayed % 64 == 0) {
          await Future<void>.delayed(Duration.zero);
          if (epoch != _diagnosticEpoch) return {'reportId': 'account-changed'};
        }
        if (row.isNotEmpty) {
          final event = Map<String, dynamic>.from(jsonDecode(row) as Map);
          if (event['event'] == 'delivery-snapshot' &&
              event['traceId'] is String) {
            selected.attachDelivery(event['traceId'] as String, event);
          } else {
            selected.record(event);
          }
        }
      }
      report = selected.report(current: current, full: false);
      report['reportType'] = fullHistory ? 'full-history' : 'session';
      report['archive'] = exported;
    }
    if (epoch != _diagnosticEpoch) return {'reportId': 'account-changed'};
    return report;
  }

  void markUnexpectedSilence() {
    record('silence-reported', {'reportedSilence': true});
    _persistFailures(immediate: true);
  }

  String? _navigationType() {
    try {
      final entries =
          web.window.performance.getEntriesByType('navigation').toDart;
      return entries.isEmpty
          ? null
          : (entries.first as web.PerformanceNavigationTiming).type;
    } catch (_) {
      return null;
    }
  }

  Map<String, dynamic> _mediaSessionEvidence() {
    try {
      final session = web.window.navigator.mediaSession;
      return {
        'mediaSessionState': session.playbackState,
        'mediaMetadataPresent': session.metadata != null,
        'mediaHandlers': _actions.join(','),
        'mediaPositionAvailable': duration > 0,
        'playRequested': _progress.requested,
      };
    } catch (_) {
      return {};
    }
  }

  void _observeExecution(String trigger) {
    final gap = _observationClock.observe(
        wallMs: DateTime.now().millisecondsSinceEpoch,
        monotonicMs: web.window.performance.now(),
        position: position,
        trace: _traceId,
        visibility: web.document.visibilityState,
        expected: _progress.requested && (_watchdog?.isActive ?? false),
        paused: paused,
        trigger: trigger);
    if (gap != null) record('observation-gap', gap);
  }

  double? _nativeEventDelay(web.Event event) {
    final now = web.window.performance.now(), stamp = event.timeStamp;
    return stamp > 0 && stamp <= now && now - stamp < 86400000
        ? now - stamp
        : null;
  }

  void setDiagnosticScope(String backend, String scope) {
    final next = 'neatie:$scope:playback-failures:$backend';
    if (_diagnosticKey == next) return;
    final previousArchive = _archive;
    if (_diagnosticKey.isNotEmpty && _diagnosticKey != next) {
      cancelStaged();
      record('source-clear', {'sourceMutationReason': 'scope-change'});
      _persistFailures(immediate: true);
      _diagnosticEpoch++;
      _traceReads.clear();
      _traceId = _title = _artist = '';
      _sourceFailureCode = null;
      _hiddenAt = null;
      _hiddenTrace = _deliveryTraceStatus = '';
      pause();
      _track = _session = _expectedSource = '';
      _loadedAt = _pausedAt = _lastProgressAt = _deadline = null;
      _everAdvanced = _resume = _renewSource = false;
      _pauseDurationMs = 0;
      _declaredMediaType = null;
      _loadWatch = null;
      _loadStartMs = _metadataMs = _canPlayMs = _playingEventMs = null;
      _nativePlayCallMs = _playAcceptedMs = _firstProgressMs = null;
      _startupWaitCount = 0;
      _sourceAssignments = _sourceReloads = 0;
      _expectedDuration = 0;
      _progress.stop(PlaybackPhase.idle);
      element.removeAttribute('src');
      element.textContent = '';
      _typedSource = null;
      element.load();
      try {
        web.window.navigator.mediaSession.metadata = null;
        web.window.navigator.mediaSession.setPositionState();
      } catch (_) {}
      _updateSession();
    }
    _diagnosticKey = next;
    _archive = DiagnosticArchive(next);
    _observedResources.clear();
    _resourceAttempts.clear();
    _recordedResourceStarts.clear();
    unawaited(previousArchive?.close() ?? Future.value());
    try {
      _reports.restore(web.window.localStorage.getItem(_diagnosticKey));
    } catch (_) {
      _reports.restore(null);
    }
    record('device');
    _archive?.add({
      'event': 'page-context',
      'pageInstance': _pageInstance,
      'at': DateTime.now().toUtc().toIso8601String(),
      'device': _reports.report(full: false)['device']
    });
  }

  void record(String event, [Map<String, dynamic> extra = const {}]) {
    if (const {'ui-frame-summary', 'image-load-summary', 'image-warmup'}
        .contains(event)) {
      // Aggregate UI observations need no audio snapshot or synchronous
      // localStorage rewrite. The existing batched archive retains them.
      _reports.record({
        'at': DateTime.now().toUtc().toIso8601String(),
        'event': event,
        'pageInstance': _pageInstance,
        if (_traceId.isNotEmpty) ...{
          'traceId': _traceId,
          'track': _track,
          'session': _session,
          'time': position,
          'phase': phase.name,
          'paused': paused,
        },
        'visibility':
            _foreground && !web.document.hidden ? 'visible' : 'hidden',
        ...extra
      });
      return;
    }
    final direct = _directIdentity;
    _reports.record({
      'at': DateTime.now().toUtc().toIso8601String(),
      'event': event,
      'pageInstance': _pageInstance,
      'pageAgeMs': _pageWatch.elapsedMilliseconds,
      'track': _track,
      'session': _session,
      'traceId': _traceId,
      'title': _title,
      'artist': _artist,
      'sourceId': Uri.tryParse(_expectedSource)?.queryParameters['source_id'],
      'cacheVariant':
          Uri.tryParse(_expectedSource)?.queryParameters['cache_variant'],
      'cacheVersion':
          Uri.tryParse(_expectedSource)?.queryParameters['cache_version'],
      ...direct,
      if (direct.isNotEmpty) 'mediaRoute': 'r2_direct',
      'time': position,
      'networkOnline': web.window.navigator.onLine,
      'documentFocused': web.document.hasFocus(),
      'expectedDuration': _expectedDuration > 0 ? _expectedDuration : null,
      'nativeDuration': element.duration.isFinite ? element.duration : null,
      'playbackRate': element.playbackRate,
      'bufferedRanges': _rangeSummary(element.buffered),
      if (const {
        'waiting',
        'stalled',
        'error',
        'position-restored',
        'position-restore-pending',
        'recovery-progress',
        'playback-startup'
      }.contains(event)) ...{
        'bufferedAheadSeconds': _bufferedAhead,
        'seekableRanges': _rangeSummary(element.seekable),
        'pendingSeekSeconds': _pendingSeek,
      },
      'paused': paused,
      'muted': element.muted,
      'volume': element.volume,
      'seeking': element.seeking,
      'ended': element.ended,
      ..._mediaSessionEvidence(),
      'ready': element.readyState,
      'network': element.networkState,
      'mediaCode': element.error?.code,
      'mediaMessage': _safeErrorText(element.error?.message ?? ''),
      'bufferedEnd': element.buffered.length > 0
          ? element.buffered.end(element.buffered.length - 1)
          : 0,
      'visibility': _foreground && !web.document.hidden ? 'visible' : 'hidden',
      'audioSession': _audioSession?.state,
      'declaredMediaType': _declaredMediaType,
      'stableResource': direct.isNotEmpty || (Uri.tryParse(_expectedSource)
              ?.queryParameters
              .containsKey('cache_version') ??
          false),
      'pinnedSource': Uri.tryParse(_expectedSource)
              ?.queryParameters
              .containsKey('stream_version') ??
          false,
      'sourceAssignments': _sourceAssignments,
      'sourceReloads': _sourceReloads,
      'loadStartMs': _loadStartMs,
      'metadataMs': _metadataMs,
      'canPlayMs': _canPlayMs,
      'playingEventMs': _playingEventMs,
      'nativePlayCallMs': _nativePlayCallMs,
      'playAcceptedMs': _playAcceptedMs,
      'firstProgressMs': _firstProgressMs,
      'startupWaitCount': _startupWaitCount,
      if (const {
        'playback-startup',
        'error',
        'ended',
        'playback-replaced',
        'user-report-open',
        'delivery-observed'
      }.contains(event))
        ..._resourceTimings(),
      ...?diagnosticContext?.call(),
      ...extra,
      'phase': phase.name,
      'playRequested': _progress.requested,
      'resume': _resume,
      'pauseMs': phase == PlaybackPhase.paused && _pausedAt != null
          ? DateTime.now().difference(_pausedAt!).inMilliseconds
          : _resume ? _pauseDurationMs : null,
      'sourceAgeSeconds': _loadedAt == null
          ? null
          : DateTime.now().difference(_loadedAt!).inSeconds,
      'secondsSinceProgress': _lastProgressAt == null
          ? null
          : DateTime.now().difference(_lastProgressAt!).inSeconds,
      'repeatOne': element.loop,
      'sourceMatches': sourceMatches,
      'elementSession': sourceMatches ? _session : null,
    });
    _persistFailures(
        immediate: event == 'error' ||
            event.contains('interrupted') ||
            event.contains('failed') ||
            event.contains('rejected') ||
            event == 'source-failure' ||
            event == 'pause' &&
                (phase == PlaybackPhase.interrupted ||
                    phase == PlaybackPhase.failed) ||
            event == 'media-action' ||
            event == 'lifecycle' && (!_foreground || web.document.hidden));
    if (const {
      'playback-startup',
      'error',
      'source-error',
      'ended',
      'playback-replaced'
    }.contains(event)) {
      final trace = _traceId,
          source = _expectedSource,
          epoch = _diagnosticEpoch;
      // No HTTP work precedes native Play or runs during the startup decision.
      Timer(const Duration(seconds: 2),
          () => _readDelivery(trace, source, epoch));
      if (event == 'playback-startup')
        Timer(const Duration(seconds: 10),
            () => _readDelivery(trace, source, epoch));
    }
  }

  Future<void> _readDelivery(String trace, String source, int epoch) async {
    final uri = Uri.tryParse(source);
    if (epoch != _diagnosticEpoch ||
        trace.isEmpty ||
        uri == null ||
        !uri.path.startsWith('/playback/stream/') ||
        _tracePending.contains(trace) ||
        (_traceReads[trace] ?? 0) >= 12) return;
    // Stable cache URLs keep their existing HTTP-cache identity. Correlate the
    // completed Resource Timing entry instead of adding a cache-busting query.
    final resource = _resourceTimings(trace: trace);
    final lookup =
        uri.queryParameters['diag'] ?? resource['serverPlaybackRequest'];
    if (lookup is! String || !RegExp(r'^[a-f0-9]{32}$').hasMatch(lookup)) {
      _deliveryStatus(trace, source, epoch, 'no_request_id');
      return;
    }
    _traceReads[trace] = (_traceReads[trace] ?? 0) + 1;
    while (_traceReads.length > 16) _traceReads.remove(_traceReads.keys.first);
    _tracePending.add(trace);
    final abort = web.AbortController();
    final timeout = Timer(const Duration(seconds: 5), () => abort.abort());
    try {
      final endpoint = uri.resolve('/playback/diagnostics/$lookup');
      final response = await web.window
          .fetch(
              endpoint.toString().toJS,
              web.RequestInit(
                  cache: 'no-store', credentials: 'omit', signal: abort.signal))
          .toDart;
      if (!response.ok) {
        _deliveryStatus(trace, source, epoch, 'trace_http_${response.status}');
        return;
      }
      final text = (await response.text().toDart).toDart;
      if (epoch != _diagnosticEpoch) return;
      if (text.length > 1048576) {
        _deliveryStatus(trace, source, epoch, 'response_too_large');
        return;
      }
      final decoded = jsonDecode(text);
      if (decoded is Map && decoded['traceId'] == lookup) {
        _reports.attachDelivery(trace, {...decoded, 'traceId': trace});
        _deliveryStatus(trace, source, epoch, 'attached');
        final requests = decoded['requests'];
        if (trace == _traceId &&
            source == _expectedSource &&
            phase != PlaybackPhase.playing &&
            (phase != PlaybackPhase.paused || element.error != null) &&
            requests is List &&
            requests.isNotEmpty &&
            requests.last is Map) {
          final last = requests.last as Map;
          final code = last['failureCode'];
          if (last['status'] is num &&
              last['status'] >= 400 &&
              const {
                'source_blocked',
                'video_unavailable',
                'format_unavailable',
                'preparation_busy',
                'preparation_timeout',
                'stream_failed',
                'playback_source_expired',
                'playback_source_missing',
                'playback_sources_failed'
              }.contains(code) &&
              _sourceFailureCode != code) {
            _sourceFailureCode = code as String;
            _progress.stop(PlaybackPhase.failed);
            _watchdog?.cancel();
            _renewSource = true;
            _updateSession();
            record('source-failure', {'failureCode': code, 'failed': true});
            _events.add('source-failure');
          }
        }
      }
      if (source == _expectedSource) record('delivery-observed');
      _persistFailures();
    } catch (_) {
      _deliveryStatus(trace, source, epoch, 'trace_request_failed');
      /* Optional diagnostics never change playback or retry it. */
    } finally {
      timeout.cancel();
      _tracePending.remove(trace);
    }
  }

  void _deliveryStatus(String trace, String source, int epoch, String status) {
    if (epoch != _diagnosticEpoch ||
        trace != _traceId ||
        source != _expectedSource ||
        _deliveryTraceStatus == status) return;
    _deliveryTraceStatus = status;
    record('delivery-trace', {'deliveryTraceStatus': status});
  }

  Map<String, dynamic> _resourceTimings({String? trace}) {
    try {
      final owner = _resourceAttempts[trace ?? _traceId];
      if (owner == null)
        return {'resourceTimingCount': 0, 'resourceEntriesCompletedOnly': true};
      final observed = _observedResources[owner.source] ??
          const <web.PerformanceResourceTiming>[];
      final entriesByStart = <double, web.PerformanceResourceTiming>{};
      for (final entry in [
        ...observed,
        ...web.window.performance.getEntriesByName(owner.source).toDart
      ]) {
        if (entry.entryType == 'resource' &&
            entry.startTime >= owner.start - 1 &&
            (owner.end == null || entry.startTime < owner.end!))
          entriesByStart[entry.startTime] =
              entry as web.PerformanceResourceTiming;
      }
      final entries = entriesByStart.values.toList()
        ..sort((a, b) => a.startTime.compareTo(b.startTime));
      if (entries.isEmpty)
        return {'resourceTimingCount': 0, 'resourceEntriesCompletedOnly': true};
      final latest = entries.last as web.PerformanceResourceTiming;
      final server = <String, web.PerformanceServerTiming>{};
      var timingSupported = false;
      try {
        for (final item in latest.serverTiming.toDart) server[item.name] = item;
        timingSupported = true;
      } catch (_) {
        // Optional server timings must not hide ordinary network measurements.
      }
      return {
        'resourceTimingCount': entries.length,
        'resourceEntriesCompletedOnly': true,
        'resourceServerTimingSupported': timingSupported,
        'resourceServerTimingCount': server.length,
        'resourceInitiatorType': latest.initiatorType,
        'resourceFirstAttemptMs': entries.first.duration,
        'resourceFirstAttemptBytes': entries.first.encodedBodySize,
        'resourceFirstByteMs': latest.responseStart > 0
            ? latest.responseStart - latest.startTime
            : null,
        'resourceTotalMs': latest.duration,
        'resourceTransferBytes': latest.transferSize,
        'resourceBodyBytes': latest.encodedBodySize,
        'resourceStartMs': latest.startTime - owner.start,
        'resourceRequestMs': latest.requestStart - owner.start,
        'resourceResponseMs': latest.responseStart > 0
            ? latest.responseStart - owner.start
            : null,
        'resourceConnectMs': latest.connectEnd - latest.connectStart,
        'resourceDnsMs': latest.domainLookupEnd - latest.domainLookupStart,
        'resourceProtocol': latest.nextHopProtocol,
        'resourceExpectedBodyBytes':
            int.tryParse(server['body']?.description ?? ''),
        'resourceContentRange': server['range']?.description,
        'resourceProbeOnly': server['range'] != null &&
            (int.tryParse(server['body']?.description ?? '') ?? 1 << 30) <=
                2048,
        'serverPlaybackRequest': server['request']?.description,
        'resourceHandlerMs': server['handler']?.duration,
        'resourceRouteWaitMs': server['route']?.duration,
      };
    } catch (_) {
      return {'resourceTimingCount': 0, 'resourceEntriesCompletedOnly': true};
    }
  }

  Map<String, dynamic> _resourceAttemptEvidence(
      web.PerformanceResourceTiming entry, double start) {
    int? status;
    var statusSupported = false;
    try {
      final value =
          (entry as JSObject).getProperty<JSAny?>('responseStatus'.toJS);
      if (value.typeofEquals('number')) {
        statusSupported = true;
        status = (value as JSNumber).toDartInt;
      }
    } catch (_) {}
    var serverTimingSupported = false;
    var serverTimingCount = 0;
    final server = <String, web.PerformanceServerTiming>{};
    try {
      for (final item in entry.serverTiming.toDart) server[item.name] = item;
      serverTimingCount = server.length;
      serverTimingSupported = true;
    } catch (_) {}
    final request = server['request']?.description ?? '';
    final edge = server['edge']?.description ?? '';
    final body = server['body']?.description ?? '';
    final range = server['range']?.description ?? '';
    return {
      'resourceAttemptStartMs': entry.startTime - start,
      'resourceAttemptDurationMs': entry.duration,
      'resourceAttemptFirstByteMs': entry.responseStart > 0
          ? entry.responseStart - entry.startTime
          : null,
      'resourceAttemptConnectMs': entry.connectEnd - entry.connectStart,
      'resourceAttemptTransferBytes': entry.transferSize,
      'resourceAttemptBodyBytes': entry.encodedBodySize,
      'resourceAttemptStatusSupported': statusSupported,
      'resourceAttemptStatus': status,
      'resourceAttemptServerTimingSupported': serverTimingSupported,
      'resourceAttemptServerTimingCount': serverTimingCount,
      'resourceAttemptInitiator': entry.initiatorType,
      if (RegExp(r'^[a-f0-9]{32}$').hasMatch(request))
        'resourceAttemptRequestId': request,
      if (RegExp(
              r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
          .hasMatch(edge))
        'resourceAttemptEdgeId': edge,
      if (RegExp(r'^[0-9]{1,15}$').hasMatch(body))
        'resourceAttemptExpectedBytes': int.tryParse(body),
      if (RegExp(r'^bytes [0-9]{1,15}-[0-9]{1,15}/[0-9]{1,15}$')
          .hasMatch(range))
        'resourceAttemptContentRange': range,
    };
  }

  void _persistFailures({bool immediate = false}) {
    if (_diagnosticKey.isEmpty) return;
    if (!immediate) {
      _diagnosticSave ??= Timer(const Duration(seconds: 3), () {
        _diagnosticSave = null;
        _persistFailures(immediate: true);
      });
      return;
    }
    _diagnosticSave?.cancel();
    _diagnosticSave = null;
    try {
      web.window.localStorage.setItem(_diagnosticKey, _reports.encodeSaved());
    } catch (_) {
      _reports.storageWriteFailed();
    }
  }

  void Function()? onPlay;
  void Function()? onPause;
  void Function()? onNext;
  void Function()? onPrevious;
  void Function(double)? onSeek;

  BrowserAudio({this.artworkUrls}) {
    try {
      web.window.performance.setResourceTimingBufferSize(1024);
      _resourceObserver = web.PerformanceObserver(
          ((web.PerformanceObserverEntryList list,
              web.PerformanceObserver observer) {
        for (final entry in list.getEntries().toDart) {
          if (entry.entryType != 'resource' ||
              !(Uri.tryParse(entry.name)
                      ?.path
                      .startsWith('/playback/stream/') ??
                  false) && !_resourceAttempts.values.any((v) => v.source == entry.name)) continue;
          final timing = entry as web.PerformanceResourceTiming;
          final entries = _observedResources.putIfAbsent(entry.name, () => []);
          entries.add(timing);
          if (entries.length > 12) entries.removeAt(0);
          while (_observedResources.length > 16)
            _observedResources.remove(_observedResources.keys.first);
          for (final attempt in _resourceAttempts.entries.toList().reversed) {
            final owner = attempt.value;
            if (owner.source != entry.name ||
                entry.startTime < owner.start - 1 ||
                (owner.end != null && entry.startTime >= owner.end!)) continue;
            final starts =
                _recordedResourceStarts.putIfAbsent(attempt.key, () => {});
            if (starts.contains(entry.startTime)) break;
            if (starts.length >= 12) {
              if (starts.length == 12) {
                starts.add(entry.startTime);
                _reports.record({
                  'event': 'resource-samples-capped',
                  'at': DateTime.now().toUtc().toIso8601String(),
                  'traceId': attempt.key,
                  'resourceEntriesCompletedOnly': true
                });
              }
              break;
            }
            starts.add(entry.startTime);
            _reports.record({
              'event': 'resource-delivery',
              'at': DateTime.now().toUtc().toIso8601String(),
              'traceId': attempt.key,
              ..._resourceAttemptEvidence(timing, owner.start),
              'resourceTimingCount': starts.length,
              'resourceEntriesCompletedOnly': true,
            });
            unawaited(
                _readDelivery(attempt.key, owner.source, _diagnosticEpoch));
            break;
          }
        }
      }).toJS)
        ..observe(
            web.PerformanceObserverInit(type: 'resource', buffered: true));
    } catch (_) {/* Optional observation must never gate native playback. */}
    element.preload = 'auto';
    element.setAttribute('playsinline', '');
    element.style.display = 'none';
    web.document.body?.append(element);
    for (final kind in ['pagehide', 'pageshow', 'freeze', 'resume']) {
      final callback = ((web.Event event) {
        _observeExecution(kind);
        record('lifecycle', {
          'kind': kind,
          if (kind == 'pagehide' || kind == 'pageshow')
            'pagePersisted': (event as web.PageTransitionEvent).persisted
        });
        _persistFailures(immediate: true);
        if (kind == 'pagehide' || kind == 'freeze') {
          unawaited(_archive?.flush() ?? Future.value());
        }
      }).toJS;
      _pageListeners[kind] = callback;
      if (kind == 'freeze' || kind == 'resume') {
        web.document.addEventListener(kind, callback);
      } else {
        web.window.addEventListener(kind, callback);
      }
    }
    try {
      _audioSession = _AudioNavigator(web.window.navigator).audioSession;
      _audioSession?.type = 'playback';
      _audioSession?.onstatechange = ((web.Event _) {
        _observeExecution('audio-session');
        if (_audioSession?.state == 'interrupted' && _progress.requested) {
          _progress.interrupt();
          record('audio-session-interrupted');
          _reports.capture('audio-session-interrupted');
          _persistFailures(immediate: true);
          _events.add('interrupted');
        } else {
          record('audio-session');
          _sampleProgress();
          if (_audioSession?.state == 'active') _events.add('session-active');
        }
        _updateSession();
      }).toJS;
    } catch (_) {/* Audio Session is optional. */}
    for (final event in [
      'playing',
      'pause',
      'ended',
      'timeupdate',
      'loadedmetadata',
      'durationchange',
      'waiting',
      'stalled',
      'seeking',
      'seeked',
      'volumechange',
      'error',
      'abort',
      'emptied',
      'loadstart',
      'suspend',
      'canplay',
      'progress'
    ]) {
      final callback = ((web.Event nativeEvent) {
        if (_handoffEventsSuppressed) return;
        _observeExecution(event);
        final observed = _mediaSessionEvidence();
        if (!sourceMatches) {
          if (event != 'timeupdate' && event != 'progress') {
            record('ignored-event', {
              'kind': event,
              'nativeEventTrusted': nativeEvent.isTrusted,
              'nativeEventDelayMs': _nativeEventDelay(nativeEvent),
            });
          }
          return;
        }
        if (event == 'error' && element.error == null && _sourceElementFailed)
          return;
        final elapsed = _loadWatch?.elapsedMilliseconds;
        if (event == 'loadedmetadata' ||
            event == 'durationchange' ||
            event == 'canplay' ||
            event == 'seeked') _restoreSeek(completed: event == 'seeked');
        if (event == 'loadstart') _loadStartMs ??= elapsed;
        if (event == 'loadedmetadata') _metadataMs ??= elapsed;
        if (event == 'canplay') _canPlayMs ??= elapsed;
        if (event == 'playing') _playingEventMs ??= elapsed;
        var forwarded = event;
        if (event == 'seeking' ||
            event == 'seeked' ||
            event == 'loadedmetadata') {
          _progress.rebase(position);
          if (event == 'seeking') _progress.buffer();
        } else if (event == 'waiting' || event == 'stalled') {
          if (!_everAdvanced) _startupWaitCount++;
          _progress.buffer();
        } else if (event == 'pause' && paused) {
          if (element.error != null || phase == PlaybackPhase.failed) {
            _progress.stop(PlaybackPhase.failed);
          } else if (element.ended) {
            _progress.stop(PlaybackPhase.ended);
          } else if (_progress.requested) {
            // App and remote Pause clear intent before calling the element.
            // An unsolicited native pause may precede visibility/session events.
            _progress.interrupt();
            forwarded = 'interrupted';
          } else {
            _progress.stop(PlaybackPhase.paused);
          }
          _pausedAt ??= DateTime.now();
        } else if (event == 'ended' && element.ended) {
          _progress.stop(PlaybackPhase.ended);
        } else if (event == 'playing' && !paused &&
            phase == PlaybackPhase.interrupted &&
            _audioSession?.state != 'interrupted') {
          // Native playback has resumed. Wait for advancing time before
          // treating it as audible playback in the app.
          _progress.buffer();
        } else if (event == 'error') {
          if (element.error?.code == 1) {
            _progress.interrupt();
          } else {
            _progress.stop(PlaybackPhase.failed);
            // A transfer failure does not establish source expiry. Retry the
            // pinned resource first; decoding/unsupported media needs renewal.
            _renewSource = element.error?.code != 2;
          }
        }
        _sampleProgress();
        if (event == 'playing' && !paused) {
          // A native resume can reacquire platform controls even if the
          // JavaScript MediaSession still holds its previous metadata.
          _restoreSessionIfNeeded(force: _pausedAt != null);
          _pausedAt = null;
        } else {
          _updateSession();
        }
        if (event != 'timeupdate' && event != 'progress')
          record(event, {
            'nativeEventTrusted': nativeEvent.isTrusted,
            if (_recoveryWatch != null)
              'recoveryStageMs': _recoveryWatch!.elapsedMilliseconds,
            'nativeEventDelayMs': _nativeEventDelay(nativeEvent),
            'observedMediaSessionState': observed['mediaSessionState'],
            'observedMediaMetadataPresent': observed['mediaMetadataPresent'],
          });
        // An explicit media abort is different from a network/decode failure.
        _events.add(event == 'error' && element.error?.code == 1
            ? 'media-aborted'
            : event == 'error' && element.error?.code == 2
                ? 'media-network-error'
                : forwarded);
      }).toJS;
      _listeners[event] = callback;
      element.addEventListener(event, callback);
    }
    _action('play', (_) => onPlay?.call());
    _action('pause', (_) => onPause?.call());
    _action('nexttrack', (_) => onNext?.call());
    _action('previoustrack', (_) => onPrevious?.call());
    _action(
        'seekto', (details) => (onSeek ?? seek)(details.seekTime ?? position));
    _action('seekbackward',
        (details) => (onSeek ?? seek)(position - (details.seekOffset ?? 10)));
    _action('seekforward',
        (details) => (onSeek ?? seek)(position + (details.seekOffset ?? 10)));
  }

  void _action(String action, void Function(_MediaAction) callback) {
    try {
      web.window.navigator.mediaSession.setActionHandler(
          action,
          ((_MediaAction details) {
            final stateBefore = _mediaSessionEvidence();
            // Reach the native element within the remote command callback,
            // before synchronous reporting, storage or Flutter work.
            callback(details);
            _observeExecution('media-action:$action');
            record('media-action', {
              'action': action,
              'observedMediaSessionState': stateBefore['mediaSessionState'],
              'observedMediaMetadataPresent': stateBefore['mediaMetadataPresent'],
            });
            if (action == 'play') _restoreSessionIfNeeded();
            if (action == 'play' && (!_foreground || web.document.hidden)) {
              // Keep the remote command even if the browser suspends timers
              // and never resolves play(). It is a request, not proof of audio.
              _reports.capture('background-resume-request');
              _persistFailures(immediate: true);
            }
            onReconcile?.call('media-action:$action');
          }).toJS);
      _actions.add(action);
    } catch (_) {
      record('media-action-unavailable', {'action': action});
    }
  }

  void _updateSession() {
    try {
      final session = web.window.navigator.mediaSession;
      final state = _progress.mediaSessionState(nativePaused: paused);
      if (session.playbackState != state) session.playbackState = state;
      if (duration > 0)
        session.setPositionState(web.MediaPositionState(
            duration: duration,
            position: position.clamp(0, duration),
            playbackRate:
                element.playbackRate == 0 ? 1 : element.playbackRate));
    } catch (_) {/* Some browsers omit position reporting. */}
  }

  void _restoreSessionIfNeeded({bool force = false}) {
    if (_track.isEmpty || !sourceMatches ||
        extractTrackId(_metadataTrack) != _track) return;
    try {
      if (force || web.window.navigator.mediaSession.metadata == null) {
        _publishMetadata(_artworkUrl);
      } else {
        _updateSession();
      }
    } catch (_) {}
  }

  @override
  Stream<String> get events => _events.stream;
  @override
  double get position => element.currentTime.isFinite ? element.currentTime : 0;
  @override
  double get duration => element.duration.isFinite ? element.duration : 0;
  double? _pendingSeek;
  bool _seekApplied = false;

  String _rangeSummary(web.TimeRanges ranges) => [
        for (var i = 0; i < ranges.length && i < 4; i++)
          '${ranges.start(i).toStringAsFixed(3)}-${ranges.end(i).toStringAsFixed(3)}'
      ].join(',');

  double get _bufferedAhead {
    for (var i = 0; i < element.buffered.length; i++) {
      if (element.buffered.start(i) <= position &&
          element.buffered.end(i) >= position) {
        return element.buffered.end(i) - position;
      }
    }
    return 0;
  }

  void _restoreSeek({bool completed = false}) {
    final target = _pendingSeek;
    if (target == null || element.readyState < 1) return;
    try {
      if (!_seekApplied) {
        element.currentTime = duration > 0 ? target.clamp(0, duration) : target;
        _seekApplied = true;
      }
      if ((position - target).abs() > .25 &&
          (duration <= 0 || target < duration)) return;
      // Assigning currentTime can report the target before its bytes arrive.
      // Do not repeatedly restart that seek or declare restoration complete.
      if (element.seeking || (!completed && element.readyState < 3)) return;
      _pendingSeek = null;
      _seekApplied = false;
      _progress.rebase(position);
      record('position-restored',
          {'requestedPosition': target, 'restoredPosition': position});
    } catch (_) {
      // Metadata may precede a seekable range. Retry on native readiness events.
    }
  }

  void _reloadRetainingPosition() {
    _sourceElementFailed = false;
    _abortedBeforeMetadata = false;
    _recoveryWatch = Stopwatch()..start();
    _pendingSeek ??= position;
    _seekApplied = false;
    final attempt = _attempt, trace = _traceId;
    // Recovery may run inside a synchronous canplay/session event subscriber.
    // Notify after that dispatch unwinds, before native metadata tasks run.
    scheduleMicrotask(() {
      if (attempt != _attempt || trace != _traceId || !sourceMatches) return;
      _events.add('source-reloading');
      _events.add('source-reloaded');
    });
    element.load();
    record('position-restore-pending', {'requestedPosition': _pendingSeek});
  }

  @override
  bool get paused => element.paused;

  @override
  bool get supportsStagedHandoff => true;

  void _clearStageListeners(_StagedAudio candidate) {
    candidate.timer?.cancel();
    if (candidate.metadata != null)
      candidate.element.removeEventListener('loadedmetadata', candidate.metadata!);
    if (candidate.canPlay != null)
      candidate.element.removeEventListener('canplay', candidate.canPlay!);
    if (candidate.error != null)
      candidate.element.removeEventListener('error', candidate.error!);
    candidate.metadata = candidate.canPlay = candidate.error = null;
  }

  @override
  void cancelStaged() {
    final candidate = _staged;
    if (candidate == null) return;
    _staged = null;
    _clearStageListeners(candidate);
    if (!candidate.cancelled.isCompleted) candidate.cancelled.complete();
    if (candidate.ready != null && !candidate.ready!.isCompleted) {
      candidate.ready!.completeError(const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'stage-cancelled'));
    }
    candidate.element.pause();
    candidate.element.removeAttribute('src');
    candidate.element.textContent = '';
    candidate.element.load();
    candidate.element.remove();
    final attempt = _resourceAttempts[candidate.traceId];
    if (attempt != null)
      _resourceAttempts[candidate.traceId] = (
        source: attempt.source,
        start: attempt.start,
        end: web.window.performance.now()
      );
    record('stage-cancelled');
  }

  @override
  Future<void> stage(Uri uri, Map<String, dynamic> track, {String? mimeType}) {
    cancelStaged();
    final trace = web.window.crypto.randomUUID().replaceAll('-', '');
    if (uri.path.startsWith('/playback/stream/') &&
        (!uri.queryParameters.containsKey('cache_version') ||
            uri.queryParameters.containsKey('playback_session'))) {
      uri =
          uri.replace(queryParameters: {...uri.queryParameters, 'diag': trace});
    }
    final audio = web.HTMLAudioElement()
      ..preload = 'auto'
      ..muted = true;
    audio.setAttribute('playsinline', '');
    audio.style.display = 'none';
    web.document.body?.append(audio);
    final candidate = _StagedAudio(audio, uri, Map.of(track), trace);
    _staged = candidate;
    final ready = candidate.ready = Completer<void>();
    void finish([PlaybackStartException? failure]) {
      if (ready.isCompleted) return;
      _clearStageListeners(candidate);
      if (failure == null) {
        candidate.readyMs = candidate.watch.elapsedMilliseconds;
        record('stage-ready', {'candidateTraceId': trace});
        ready.complete();
      } else {
        record('stage-failed',
            {'candidateTraceId': trace, 'cause': failure.cause});
        ready.completeError(failure);
        if (identical(_staged, candidate)) cancelStaged();
      }
    }

    candidate.metadata = ((web.Event _) {
      candidate.metadataMs ??= candidate.watch.elapsedMilliseconds;
    }).toJS;
    candidate.canPlay = ((web.Event _) => finish()).toJS;
    candidate.error = ((web.Event _) => finish(const PlaybackStartException(
        PlaybackStartFailure.failed, 'stage-media-error'))).toJS;
    audio.addEventListener('loadedmetadata', candidate.metadata!);
    audio.addEventListener('canplay', candidate.canPlay!);
    audio.addEventListener('error', candidate.error!);
    candidate.timer = Timer(
        const Duration(seconds: 90),
        () => finish(const PlaybackStartException(
            PlaybackStartFailure.interrupted, 'stage-timeout')));
    candidate.source = _assignSource(audio, uri, mimeType, (source) {
      if (_typedSource == source) {
        _activeSourceError(source);
      } else if (identical(_staged, candidate)) {
        if (!candidate.sourceFailed.isCompleted) candidate.sourceFailed.complete();
        finish(const PlaybackStartException(
            PlaybackStartFailure.failed, 'stage-media-error'));
      }
    });
    candidate.declaredMediaType = candidate.source == null ? null : mimeType;
    _resourceAttempts[trace] =
        (source: candidate.source?.src ?? audio.src, start: candidate.start, end: null);
    _trimResourceAttempts();
    audio.load();
    record('stage-start', {'candidateTraceId': trace,
      'candidateMediaType': candidate.declaredMediaType});
    return ready.future;
  }

  @override
  Future<void> commitStaged() async {
    final candidate = _staged;
    if (candidate == null || !(candidate.ready?.isCompleted ?? false)) {
      throw const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'stage-unavailable');
    }
    if (candidate.sourceFailed.isCompleted) {
      throw const PlaybackStartException(
          PlaybackStartFailure.failed, 'stage-media-error');
    }
    if (web.document.hidden &&
        web.window.navigator.userAgent.toLowerCase().contains('android')) {
      final media = candidate.element;
      bool hasRunway() {
        final position = media.currentTime;
        final remaining = media.duration - position;
        final minimum = remaining.isFinite && remaining < 12 ? remaining : 12;
        for (var i = 0; i < media.buffered.length; i++) {
          if (media.buffered.start(i) <= position + .05 &&
              media.buffered.end(i) - position >= minimum - .05) return true;
        }
        return false;
      }
      if (!hasRunway()) {
        final ready = Completer<void>();
        final check = ((web.Event _) {
          if (!ready.isCompleted && hasRunway()) ready.complete();
        }).toJS;
        media.addEventListener('progress', check);
        media.addEventListener('canplaythrough', check);
        try {
          await Future.any<void>([ready.future, candidate.cancelled.future,
            candidate.sourceFailed.future]).timeout(
                const Duration(seconds: 3), onTimeout: () {});
        } finally {
          media.removeEventListener('progress', check);
          media.removeEventListener('canplaythrough', check);
        }
      }
      if (!identical(_staged, candidate)) {
        throw const PlaybackStartException(
            PlaybackStartFailure.interrupted, 'stage-cancelled');
      }
      if (candidate.sourceFailed.isCompleted) {
        throw const PlaybackStartException(
            PlaybackStartFailure.failed, 'stage-media-error');
      }
      if (web.document.hidden && !hasRunway()) {
        throw const PlaybackStartException(
            PlaybackStartFailure.interrupted, 'stage-thin-buffer');
      }
    }
    final previous = element;
    final resumePrevious = !previous.paused;
    // The new element already has playable data. Do not depend on an unmute
    // after a muted Play; browsers can apply separate permission checks there.
    previous.pause();
    candidate.element.muted = previous.muted;
    candidate.element.volume = previous.volume;
    try {
      candidate.playCallMs = candidate.watch.elapsedMilliseconds;
      await Future.any<void>([
        candidate.element.play().toDart.then<void>((_) {}),
        candidate.sourceFailed.future.then((_) => throw const PlaybackStartException(
            PlaybackStartFailure.failed, 'stage-media-error')),
        candidate.cancelled.future.then((_) =>
            throw const PlaybackStartException(
                PlaybackStartFailure.interrupted, 'stage-cancelled')),
      ]).timeout(const Duration(seconds: 3));
      candidate.playAcceptedMs = candidate.watch.elapsedMilliseconds;
      if (!identical(_staged, candidate)) {
        throw const PlaybackStartException(
            PlaybackStartFailure.interrupted, 'stage-cancelled');
      }
      if (candidate.element.paused || candidate.element.readyState < 3) {
        throw const PlaybackStartException(
            PlaybackStartFailure.failed, 'stage-not-playing');
      }
    } catch (error) {
      final stillOwned = identical(_staged, candidate);
      if (stillOwned) cancelStaged();
      // An explicit Pause/account switch cancels ownership and must never be
      // undone by this rollback. Genuine start failure retains A's resource.
      if (stillOwned &&
          resumePrevious &&
          !_disposed &&
          identical(element, previous)) {
        try {
          await play();
        } catch (_) {/* Controller retains the failure. */}
      }
      if (error is PlaybackStartException) rethrow;
      final name = _domExceptionName(error);
      throw PlaybackStartException(
          name == 'NotAllowedError' ||
                  name == 'AbortError' ||
                  error is TimeoutException
              ? PlaybackStartFailure.interrupted
              : PlaybackStartFailure.failed,
          name == 'NotAllowedError' ? 'permission' : 'stage-play-failed');
    }
    record('playback-replaced');
    _handoffEventsSuppressed = true;
    for (final entry in _listeners.entries) {
      previous.removeEventListener(entry.key, entry.value);
    }
    previous.pause();
    element = candidate.element;
    element.loop = previous.loop;
    element.volume = previous.volume;
    element.muted = previous.muted;
    _staged = null;
    _adopting = candidate;
    load(candidate.uri, candidate.track);
    _adopting = null;
    for (final entry in _listeners.entries) {
      element.addEventListener(entry.key, entry.value);
    }
    previous.removeAttribute('src');
    previous.textContent = '';
    previous.load();
    previous.remove();
    _progress.begin(position);
    _deadline = DateTime.now().add(const Duration(seconds: 90));
    _watchdog?.cancel();
    _watchdog =
        Timer.periodic(const Duration(seconds: 1), (_) => _checkProgress());
    Timer.run(() {
      if (_disposed ||
          !identical(element, candidate.element) ||
          _traceId != candidate.traceId) return;
      _handoffEventsSuppressed = false;
      syncMetadata(candidate.track);
      _sampleProgress();
      _updateSession();
      record('stage-committed', {'candidateTraceId': candidate.traceId});
      _events.add('loadedmetadata');
      _events.add('playback-state');
    });
  }

  bool get repeatOne => element.loop;
  set repeatOne(bool enabled) => element.loop = enabled;

  @override
  void load(Uri uri, Map<String, dynamic> track, {String? mimeType}) {
    final adopting = _adopting;
    if (adopting == null) {
      cancelStaged();
      _handoffEventsSuppressed = false;
    }
    _pendingSeek = null;
    _seekApplied = false;
    _sourceFailureCode = null;
    _sourceElementFailed = false;
    _recoveryWatch = null;
    if (adopting == null && _traceId.isNotEmpty) record('playback-replaced');
    final previous = _resourceAttempts[_traceId];
    if (previous != null)
      _resourceAttempts[_traceId] = (
        source: previous.source,
        start: previous.start,
        end: web.window.performance.now()
      );
    _attempt++;
    _watchdog?.cancel();
    _progress.stop(PlaybackPhase.idle);
    _loadedAt = DateTime.now();
    _lastProgressAt = null;
    _pausedAt = null;
    _pauseDurationMs = 0;
    _loadWatch = adopting?.watch ?? (Stopwatch()..start());
    _performanceStart = adopting?.start ?? web.window.performance.now();
    _loadStartMs = _metadataMs = _canPlayMs = _playingEventMs = null;
    _nativePlayCallMs = _playAcceptedMs = _firstProgressMs = null;
    if (adopting != null) {
      _metadataMs = adopting.metadataMs;
      _canPlayMs = adopting.readyMs;
      _nativePlayCallMs = adopting.playCallMs;
      _playAcceptedMs = adopting.playAcceptedMs;
    }
    _startupWaitCount = 0;
    _sourceAssignments++;
    _sourceReloads = 0;
    _everAdvanced = _resume = _renewSource = false;
    _abortedBeforeMetadata = false;
    _track = extractTrackId(track) ?? '';
    _title = '${track['title'] ?? ''}';
    _artist = '${track['artist'] ?? track['author'] ?? track['channel'] ?? ''}';
    _traceId =
        adopting?.traceId ?? web.window.crypto.randomUUID().replaceAll('-', '');
    _deliveryTraceStatus = '';
    if (uri.path.startsWith('/playback/stream/') &&
        (!uri.queryParameters.containsKey('cache_version') ||
            uri.queryParameters.containsKey('playback_session')))
      uri = uri
          .replace(queryParameters: {...uri.queryParameters, 'diag': _traceId});
    _expectedDuration = trackDurationSeconds(track).toDouble();
    _session = uri.queryParameters['playback_session'] ??
        '${DateTime.now().microsecondsSinceEpoch}-$_attempt';
    if (adopting == null) {
      element.removeAttribute('src');
      element.textContent = '';
    }
    if (adopting != null) {
      _typedSource = adopting.source;
      _declaredMediaType = adopting.declaredMediaType;
      _expectedSource = adopting.source?.src ?? element.src;
    } else {
      _typedSource = _assignSource(element, uri, mimeType, _activeSourceError);
      _declaredMediaType = _typedSource == null ? null : mimeType;
      _expectedSource = _typedSource?.src ?? element.src;
    }
    _resourceAttempts[_traceId] =
        (source: _expectedSource, start: _performanceStart, end: null);
    _trimResourceAttempts();
    if (adopting == null) element.load();
    record('load', {
      'sourceMutationReason': adopting == null ? 'track-load' : 'staged-handoff'
    });
    if (adopting == null) syncMetadata(track);
  }

  web.HTMLSourceElement? _assignSource(web.HTMLAudioElement target, Uri uri,
      String? mimeType, void Function(web.HTMLSourceElement) onError) {
    if (const {'audio/mp4', 'audio/webm', 'audio/mpeg', 'audio/ogg', 'audio/wav'}
            .contains(mimeType) &&
        target.canPlayType(mimeType!).isNotEmpty) {
      final source = web.HTMLSourceElement()
        ..src = uri.toString()
        ..type = mimeType;
      source.addEventListener('error', ((web.Event _) => onError(source)).toJS);
      target.append(source);
      return source;
    }
    target.src = uri.toString();
    return null;
  }

  void _activeSourceError(web.HTMLSourceElement source) {
    if (_typedSource != source) return;
    _sourceElementFailed = true;
    _renewSource = false;
    _progress.stop(PlaybackPhase.failed);
    _watchdog?.cancel();
    final attempt = ++_attempt, trace = _traceId;
    record('source-error', {
      'failureCode': _sourceFailureCode ?? 'media_source_error_unknown'
    });
    scheduleMicrotask(() {
      if (attempt == _attempt && trace == _traceId && _typedSource == source)
        _events.add('media-source-error');
    });
  }

  void _trimResourceAttempts() {
    while (_resourceAttempts.length > 32) {
      final oldest = _resourceAttempts.keys.first;
      _resourceAttempts.remove(oldest);
      _recordedResourceStarts.remove(oldest);
    }
  }

  @override
  void syncMetadata(Map<String, dynamic> track) {
    if (extractTrackId(track) == null || extractTrackId(track) != loadedTrackId)
      return;
    if (extractTrackId(track) == loadedTrackId &&
        trackDurationSeconds(track) > 0) {
      _expectedDuration = trackDurationSeconds(track).toDouble();
    }
    _metadataTrack = Map<String, dynamic>.from(track);
    final urls = artworkUrls?.call(track) ?? const <String>[];
    final key = '${loadedTrackId ?? ''}\n${urls.join('\n')}';
    final same = key == _artworkKey;
    if (same &&
        (_artworkPending ||
            _artworkUrl.isNotEmpty ||
            (_artworkAttemptedAt != null &&
                DateTime.now().difference(_artworkAttemptedAt!) <
                    const Duration(seconds: 30)))) {
      _publishMetadata(_artworkUrl);
      return;
    }
    _cancelArtwork?.call();
    final epoch = ++_artworkEpoch;
    final retained = _artworkKey.startsWith('${loadedTrackId ?? ''}\n') &&
            urls.contains(_artworkUrl)
        ? _artworkUrl
        : '';
    _artworkKey = key;
    _artworkUrl = retained;
    _artworkPending = urls.isNotEmpty;
    _artworkAttemptedAt = DateTime.now();
    // Publish text immediately. Only a decoded, current-track cover is added;
    // optional artwork never delays the native Play call.
    _publishMetadata(_artworkUrl);
    unawaited(_selectArtwork(urls, epoch));
  }

  void _publishMetadata(String image) {
    try {
      final track = _metadataTrack;
      web.window.navigator.mediaSession.metadata =
          web.MediaMetadata(web.MediaMetadataInit(
        title: track['title']?.toString() ?? 'Track',
        artist: (track['artist'] ?? track['author'] ?? track['channel'])
                ?.toString() ??
            '',
        album: track['album']?.toString() ?? '',
        artwork: [if (image.isNotEmpty) web.MediaImage(src: image)].toJS,
      ));
    } catch (_) {/* Metadata support does not gate playback. */}
    _updateSession();
  }

  Future<void> _selectArtwork(List<String> urls, int epoch) async {
    final watch = Stopwatch()..start();
    for (var index = 0; index < urls.length; index++) {
      if (epoch != _artworkEpoch) return;
      final image = web.HTMLImageElement()..decoding = 'async';
      final done = Completer<bool>();
      void finish(bool loaded) {
        if (!done.isCompleted) done.complete(loaded);
      }

      final loaded = ((web.Event _) => finish(image.naturalWidth > 0)).toJS;
      final failed = ((web.Event _) => finish(false)).toJS;
      image.addEventListener('load', loaded);
      image.addEventListener('error', failed);
      final timer =
          Timer(const Duration(milliseconds: 1500), () => finish(false));
      _cancelArtwork = () => finish(false);
      image.src = urls[index];
      final ready = await done.future;
      timer.cancel();
      image.removeEventListener('load', loaded);
      image.removeEventListener('error', failed);
      image.removeAttribute('src');
      if (epoch != _artworkEpoch) return;
      _cancelArtwork = null;
      record('media-artwork', {
        'outcome': ready ? 'ready' : 'candidate-failed',
        'candidate': index + 1,
        'candidates': urls.length,
        'elapsedMs': watch.elapsedMilliseconds
      });
      if (ready) {
        _artworkUrl = urls[index];
        _artworkPending = false;
        _publishMetadata(_artworkUrl);
        return;
      }
    }
    if (epoch != _artworkEpoch) return;
    _artworkPending = false;
    record('media-artwork', {
      'outcome': urls.isEmpty ? 'no-source' : 'unavailable',
      'candidates': urls.length,
      'elapsedMs': watch.elapsedMilliseconds
    });
  }

  @override
  void reloadSource() {
    _attempt++;
    _watchdog?.cancel();
    _renewSource = false;
    _sourceReloads++;
    _progress.interrupt();
    _reloadRetainingPosition();
    record('resume-source-reload', {'sourceMutationReason': 'resume-recovery'});
  }

  @override
  Future<void> play() async {
    final attempt = ++_attempt;
    final trace = _traceId;
    _pauseDurationMs = _pausedAt == null
        ? 0
        : DateTime.now().difference(_pausedAt!).inMilliseconds;
    _resume = _everAdvanced;
    _progress.begin(position, confirmResume: _resume);
    _deadline = DateTime.now().add(
        Duration(seconds: _resume ? (fullyBuffered ? 3 : 12) : 90));
    try {
      if (_audioSession?.type != 'playback') _audioSession?.type = 'playback';
    } catch (_) {}
    // A terminal MEDIA_ERR_ABORTED cannot be restarted by play() alone.
    // Reload the same selected URL; do not resolve a different source.
    final abortedStart = _abortedBeforeMetadata && !_everAdvanced &&
        _metadataMs == null && element.readyState == 0 &&
        (_loadWatch?.elapsedMilliseconds ?? 0) >= 750;
    if (sourceMatches &&
        (_sourceElementFailed ||
            abortedStart ||
            element.error?.code == 1 ||
            element.error?.code == 2)) {
      _sourceReloads++;
      _reloadRetainingPosition();
      record('aborted-source-reload',
          {'sourceMutationReason': abortedStart ? 'zero-metadata-abort' : 'native-error-recovery'});
    }
    final reloads = _sourceReloads;
    try {
      _nativePlayCallMs ??= _loadWatch?.elapsedMilliseconds;
      final pending = element.play().toDart;
      _updateSession();
      _watchdog?.cancel();
      // Observation only: this timer does not keep a background page alive.
      _watchdog =
          Timer.periodic(const Duration(seconds: 1), (_) => _checkProgress());
      record('play-call');
      _observeExecution('play-call');
      await pending;
      if (attempt != _attempt) return;
      _playAcceptedMs ??= _loadWatch?.elapsedMilliseconds;
      record('play-accepted');
      _sampleProgress();
      _updateSession();
    } catch (error) {
      final name = _domExceptionName(error);
      // pause() supersedes the Play attempt but can leave its loader stuck.
      // Remember only an abort of this exact, still-unloaded representation.
      if (trace == _traceId && reloads == _sourceReloads &&
          name == 'AbortError' && !_everAdvanced &&
          _metadataMs == null && element.readyState == 0) {
        _abortedBeforeMetadata = true;
      }
      if (attempt != _attempt) return;
      final cause = name == 'NotAllowedError'
          ? 'permission'
          : name == 'AbortError'
              ? 'abort'
              : 'unknown';
      final kind = cause == 'permission' || cause == 'abort'
          ? PlaybackStartFailure.interrupted
          : PlaybackStartFailure.failed;
      if (kind == PlaybackStartFailure.interrupted) {
        _progress.interrupt();
      } else {
        _progress.stop(PlaybackPhase.failed);
        _renewSource = true;
      }
      _watchdog?.cancel();
      _updateSession();
      record('play-rejected', {
        'cause': cause,
        'errorType': error.runtimeType.toString(),
        'exceptionName': name,
        'exceptionMessage': _safeErrorText(_domExceptionField(error, 'message'))
      });
      throw PlaybackStartException(kind, cause);
    }
  }

  String _domExceptionName(Object error) {
    return _domExceptionField(error, 'name');
  }

  String _safeErrorText(String value) {
    return PlaybackDiagnostics.safeText(value);
  }

  void _sampleProgress() {
    if (_pendingSeek != null) return;
    final previous = phase;
    if (_progress.observe(position,
        paused: paused ||
            element.error != null ||
            element.readyState < 3 ||
            _audioSession?.state == 'interrupted',
        seeking: element.seeking)) {
      _lastProgressAt = DateTime.now();
      _firstProgressMs ??= _loadWatch?.elapsedMilliseconds;
      if (!_resume || phase == PlaybackPhase.playing) {
        _deadline = _lastProgressAt!.add(const Duration(seconds: 20));
      }
      _everAdvanced = true;
    }
    if (phase != previous) {
      _updateSession();
      record('playback-state');
      if (_recoveryWatch != null && phase == PlaybackPhase.playing) {
        record('recovery-progress',
            {'recoveryStageMs': _recoveryWatch!.elapsedMilliseconds});
        _recoveryWatch = null;
      }
      _events.add('playback-state');
    }
  }

  void _checkProgress() {
    _observeExecution('watchdog');
    if (!_progress.requested || !sourceMatches) return;
    _sampleProgress();
    final now = DateTime.now();
    if (phase == PlaybackPhase.playing &&
        _lastProgressAt != null &&
        now.difference(_lastProgressAt!).inSeconds >= 4 &&
        !element.seeking) {
      _progress.buffer();
      _updateSession();
      record('playback-stalled');
      _events.add('playback-state');
    }
    // A running browser can recover a frozen, already buffered resume while
    // locked. Other deadlines wait until the foreground samples the element.
    final frozenResume = _progress.resumePending && !paused && fullyBuffered;
    if ((!_foreground || web.document.hidden) && !frozenResume ||
        _audioSession?.state == 'interrupted' ||
        element.seeking) return;
    if (_deadline != null && now.isAfter(_deadline!)) {
      // A lack of progress does not prove that the selected URL is invalid.
      // The controller first reloads that resource, then bounds renewal.
      _progress.interrupt();
      _watchdog?.cancel();
      final reason = _resume
          ? 'resume-stalled'
          : _everAdvanced
              ? 'playback-stalled-timeout'
              : 'startup-stalled';
      _updateSession();
      record(reason, {'failed': true});
      _events.add(reason);
    }
  }

  @override
  void foreground() {
    _foreground = true;
    _observeExecution('foreground');
    if (_hiddenAt != null) {
      record('visibility-return', {
        'hiddenForMs': DateTime.now().difference(_hiddenAt!).inMilliseconds,
        'hiddenTrackChanged': _hiddenTrace != _traceId,
        if (_hiddenTrace == _traceId)
          'hiddenPositionDeltaSeconds': position - _hiddenPosition,
      });
      _hiddenAt = null;
    }
    _restoreSessionIfNeeded();
    if (!_progress.requested) return;
    _sampleProgress();
    _deadline = DateTime.now().add(Duration(
        seconds: _progress.resumePending && fullyBuffered
            ? 3 : _everAdvanced ? 12 : 90));
    _updateSession();
    record('foreground-check');
  }

  void background() {
    unawaited(_archive?.flush() ?? Future.value());
    if (_foreground) {
      _hiddenAt = DateTime.now();
      _hiddenPosition = position;
      _hiddenTrace = _traceId;
    }
    _foreground = false;
    _observeExecution('background');
  }

  web.File _reportFile(Map<String, dynamic> report) => web.File(
      [jsonEncode(report).toJS].toJS,
      'neatie-playback-${report['reportId']}.txt',
      web.FilePropertyBag(type: 'text/plain;charset=utf-8'));

  bool canShareReport(Map<String, dynamic> report) {
    try {
      return web.window.navigator
          .canShare(web.ShareData(files: [_reportFile(report)].toJS));
    } catch (_) {
      return false;
    }
  }

  Future<void> shareReport(Map<String, dynamic> report) async {
    // Invoke share directly within the user's tap, before any await.
    await web.window.navigator
        .share(web.ShareData(
            title: 'Neatie playback report', files: [_reportFile(report)].toJS))
        .toDart;
  }

  void downloadReport(Map<String, dynamic> report) {
    final file = _reportFile(report);
    final url = web.URL.createObjectURL(file);
    final anchor = web.HTMLAnchorElement()
      ..href = url
      ..download = file.name;
    anchor.style.display = 'none';
    web.document.body?.append(anchor);
    anchor.click();
    anchor.remove();
    Timer(const Duration(seconds: 60), () => web.URL.revokeObjectURL(url));
  }

  String _domExceptionField(Object error, String field) {
    // JS promises may reject with DOMException objects that do not preserve
    // their Dart wrapper type across browser implementations.
    try {
      final value = (error as JSObject).getProperty<JSString>(field.toJS);
      return value.toDart;
    } catch (_) {
      return '';
    }
  }

  @override
  void pause() {
    _attempt++;
    _pausedAt = DateTime.now();
    _progress.stop(element.error != null || phase == PlaybackPhase.failed
        ? PlaybackPhase.failed
        : PlaybackPhase.paused);
    _watchdog?.cancel();
    element.pause();
    _observeExecution('explicit-pause');
    _updateSession();
    record('pause-boundary');
  }

  @override
  void seek(double seconds) {
    if (!seconds.isFinite || seconds < 0) return;
    _pendingSeek = seconds;
    _seekApplied = false;
    _restoreSeek();
    _updateSession();
  }

  @override
  void dispose() {
    _disposed = true;
    cancelStaged();
    record('source-clear', {'sourceMutationReason': 'dispose'});
    _artworkEpoch++;
    _cancelArtwork?.call();
    _resourceObserver?.disconnect();
    _observedResources.clear();
    _recordedResourceStarts.clear();
    _persistFailures(immediate: true);
    unawaited(_archive?.close() ?? Future.value());
    _diagnosticEpoch++;
    _attempt++;
    _watchdog?.cancel();
    _audioSession?.onstatechange = null;
    for (final entry in _listeners.entries)
      element.removeEventListener(entry.key, entry.value);
    for (final entry in _pageListeners.entries) {
      if (entry.key == 'freeze' || entry.key == 'resume') {
        web.document.removeEventListener(entry.key, entry.value);
      } else {
        web.window.removeEventListener(entry.key, entry.value);
      }
    }
    for (final action in _actions) {
      web.window.navigator.mediaSession.setActionHandler(action, null);
    }
    element.pause();
    element.removeAttribute('src');
    element.textContent = '';
    element.load();
    element.remove();
    _events.close();
  }
}
