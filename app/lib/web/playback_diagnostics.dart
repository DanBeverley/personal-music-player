import 'dart:convert';

/// Fixed-size distributions; no frame arrays, strings or logs in frame callbacks.
class FrameTimeDistribution {
  static const bounds = <double>[
    1,
    2,
    4,
    8,
    12,
    16.7,
    20,
    25,
    33.4,
    50,
    75,
    100,
    250,
    500,
    1000
  ];
  final buckets = List<int>.filled(bounds.length + 1, 0);
  int count = 0, over16 = 0, over33 = 0, over50 = 0;
  double maxMs = 0;
  void add(double ms) {
    if (!ms.isFinite || ms < 0) return;
    var index = 0;
    while (index < bounds.length && ms > bounds[index]) index++;
    buckets[index]++;
    count++;
    if (ms > 16.7) over16++;
    if (ms > 33.4) over33++;
    if (ms > 50) over50++;
    if (ms > maxMs) maxMs = ms;
  }

  double get p95UpperMs {
    var total = 0;
    for (var i = 0; i < buckets.length; i++) {
      total += buckets[i];
      if (total >= (count * .95).ceil())
        return count == 0
            ? 0
            : i == bounds.length
                ? maxMs
                : bounds[i];
    }
    return 0;
  }
}

/// Measures missed observations; it cannot establish suspension or audibility.
class PlaybackObservationClock {
  int? _wall;
  double _monotonic = 0, _position = 0;
  String _trace = '', _visibility = '';
  bool _expected = false, _paused = true;

  Map<String, dynamic>? observe(
      {required int wallMs,
      required double monotonicMs,
      required double position,
      required String trace,
      required String visibility,
      required bool expected,
      required bool paused,
      required String trigger}) {
    final wallGap = _wall == null ? 0 : wallMs - _wall!;
    final gap = _wall != null &&
            trace.isNotEmpty &&
            trace == _trace &&
            _expected &&
            wallGap >= 3500
        ? <String, dynamic>{
            'wallGapMs': wallGap,
            'monotonicGapMs': monotonicMs - _monotonic,
            'nativePositionDeltaSeconds': position - _position,
            'previousVisibility': _visibility,
            'previousPaused': _paused,
            'observationTrigger': trigger,
          }
        : null;
    _wall = wallMs;
    _monotonic = monotonicMs;
    _position = position;
    _trace = trace;
    _visibility = visibility;
    _expected = expected;
    _paused = paused;
    return gap;
  }
}

/// Bounded, scoped technical reports. No automatic upload or account identity.
class PlaybackDiagnostics {
  static const maxSavedBytes = 262144;
  static const maxAttempts = 32;
  static const maxTimeline = 256;
  static const _homeFields = {
    'sessionId',
    'feedVersion',
    'queueRevision',
    'readyFeedCount',
    'queueBuildInflight',
    'preparationState',
    'preparationBlockedReason',
    'backgroundSupplyInflight',
    'forceRefresh',
    'refreshFromVersion',
    'feedAction',
    'feedReason',
    'noveltyEnforced',
    'noveltyWouldReject',
  };
  static const _visualFields = {
    'outcome',
    'candidate',
    'candidates',
    'elapsedMs',
    'surface',
    'endSurface',
    'trigger',
    'windowMs',
    'viewChanges',
    'frameCount',
    'buildP95UpperMs',
    'rasterP95UpperMs',
    'frameP95UpperMs',
    'frameMaxMs',
    'frameOver16Count',
    'frameOver33Count',
    'rafCount',
    'rafP95UpperMs',
    'rafMaxMs',
    'rafOver33Count',
    'rafOver50Count',
    'frameTimingSupported',
    'displayRefreshHz',
    'imageCount',
    'imageNoSourceCount',
    'imagePlayerKey',
    'imagePlayerOutcome',
    'imagePlayerAttempts',
    'imageReadyCount',
    'imageFailedCount',
    'imageAttemptCount',
    'imageCancelledCount',
    'imagePendingCount',
    'imageCacheHitCount',
    'imageMaxMs',
    'imageSlowKey',
    'imageSlowOutcome',
    'imageSlowMs',
    'imageSlowAttempts',
    'imageSlowBytesMs',
    'imageSlowFrameAfterBytesMs',
    'imageTotalMs',
    'imageAlternativeCount',
    'responseBytes',
    'jsonDecodeMs',
    'historyEventReadMs',
    'historyRouteWaitMs',
    'historyCatalogLookupMs',
    'historyCountMs',
    'historyTotalMs',
  };
  static const _allowed = {
    'resourceAttemptStartMs',
    'resourceEntriesCompletedOnly',
    'resourceAttemptDurationMs',
    'resourceAttemptFirstByteMs',
    'resourceAttemptConnectMs',
    'resourceAttemptTransferBytes',
    'resourceAttemptBodyBytes',
    'resourceAttemptStatusSupported',
    'resourceAttemptStatus',
    'resourceAttemptServerTimingSupported',
    'resourceAttemptServerTimingCount',
    'resourceAttemptInitiator',
    'resourceAttemptRequestId',
    'resourceAttemptEdgeId',
    'resourceAttemptExpectedBytes',
    'resourceAttemptContentRange',
    'mediaHandlers',
    'mediaPositionAvailable',
    'sourceMutationReason',
    'sourceVerified',
    'choice',
    'bufferedAheadSeconds',
    'seekableRanges',
    'pendingSeekSeconds',
    'requestedPosition',
    'restoredPosition',
    ..._homeFields,
    ..._visualFields,
    'nativeEventTrusted',
    'resourceServerTimingSupported',
    'resourceServerTimingCount',
    'resourceInitiatorType',
    'resourceFirstAttemptMs',
    'resourceFirstAttemptBytes',
    'selected_artist_count',
    'selected_album_count',
    'catalog_candidate_count',
    'recoveryStageMs',
    'networkOnline',
    'documentFocused',
    'candidate_count',
    'fresh_count',
    'repeated_count',
    'selected_count',
    'provider_depth',
    'fresh_supply_exhausted',
    'traceId',
    'candidateTraceId',
    'candidateMediaType',
    'topResultId',
    'topResultType',
    'pageInstance',
    'pageAgeMs',
    'pagePersisted',
    'wallGapMs',
    'largestObservationGapMs',
    'largestObservationGapAt',
    'seeking',
    'ended',
    'monotonicGapMs',
    'nativePositionDeltaSeconds',
    'previousVisibility',
    'previousPaused',
    'observationTrigger',
    'nativeEventDelayMs',
    'mediaSessionState',
    'mediaMetadataPresent',
    'observedMediaSessionState',
    'observedMediaMetadataPresent',
    'hiddenForMs',
    'hiddenPositionDeltaSeconds',
    'hiddenTrackChanged',
    'reportedSilence',
    'deliveryTraceStatus',
    'startedAt',
    'failedAt',
    'interruptedAt',
    'interruptionCount',
    'resumeCount',
    'title',
    'artist',
    'sourceId',
    'cacheVariant',
    'cacheVersion',
    'serverPlaybackRequest',
    'resourceHandlerMs',
    'resourceRouteWaitMs',
    'at',
    'event',
    'track',
    'session',
    'time',
    'paused',
    'muted',
    'volume',
    'ready',
    'network',
    'mediaCode',
    'mediaMessage',
    'bufferedEnd',
    'bufferedRanges',
    'nativeDuration',
    'expectedDuration',
    'catalogueDuration',
    'sourceDuration',
    'durationDeltaSeconds',
    'playbackRate',
    'failureStage',
    'failureType',
    'failureHttpStatus',
    'visibility',
    'audioSession',
    'repeatOne',
    'sourceMatches',
    'elementSession',
    'selectedTrack',
    'loadedTrack',
    'queueIndex',
    'operation',
    'loading',
    'startupInterrupted',
    'cause',
    'errorType',
    'exceptionName',
    'exceptionMessage',
    'awaitingFirstPlayback',
    'hiddenDuringStartup',
    'path',
    'ms',
    'startupMs',
    'declaredMediaType',
    'stableResource',
    'pinnedSource',
    'sourceAssignments',
    'sourceReloads',
    'loadStartMs',
    'metadataMs',
    'canPlayMs',
    'playingEventMs',
    'nativePlayCallMs',
    'playAcceptedMs',
    'firstProgressMs',
    'startupWaitCount',
    'resourceTimingCount',
    'resourceFirstByteMs',
    'resourceTotalMs',
    'resourceTransferBytes',
    'resourceBodyBytes',
    'resourceStartMs',
    'resourceRequestMs',
    'resourceResponseMs',
    'resourceConnectMs',
    'resourceDnsMs',
    'resourceProtocol',
    'resourceExpectedBodyBytes',
    'resourceContentRange',
    'resourceProbeOnly',
    'mediaRoute',
    'nextGapMs',
    'resumeMs',
    'sourceRetained',
    'fullyBuffered',
    'resultCount',
    'preview',
    'first_response_contract_ms',
    'direct_lookup_ms',
    'inputToResultsMs',
    'queryLength',
    'serverMs',
    'serverRequestId',
    'searchSurface',
    'searchOutcome',
    'searchRefreshRetained',
    'searchProviderFailures',
    'searchProvider_songs',
    'searchProvider_videos',
    'searchProvider_artists',
    'searchProvider_top',
    'snapshotHit',
    'retrievalMs',
    'rankingMs',
    'essential_targetMs',
    'artist_surface_completionMs',
    'snapshot_storeMs',
    'localIndexMs',
    'providerTimeoutCount',
    'cachedPrefixBytes',
    'chunkDeferred',
    'startupBytesReady',
    'requestedTrack',
    'preparedSourceId',
    'preparedVariant',
    'preparedVersion',
    'preparedDirect',
    'requestPurpose',
    'requestId',
    'requestBytes',
    'transportRoute',
    'online',
    'failureCode',
    'cacheTier',
    'status',
    'action',
    'kind',
    'reason',
    'error',
    'failed',
    'prepared',
    'preparedAtStart',
    'fullyCached',
    'build',
    'phase',
    'playRequested',
    'pauseMs',
    'resume',
    'sourceAgeSeconds',
    'secondsSinceProgress',
    'recoveryAttempts'
  };
  final List<Map<String, dynamic>> _recent = [];
  final List<Map<String, dynamic>> _incidents = [];
  final List<Map<String, dynamic>> _performance = [];
  final List<Map<String, dynamic>> _attempts = [];
  final List<Map<String, dynamic>> _timeline = [];
  final Map<String, int> _dropped = {};
  final Map<String, String> _deliveryFingerprints = {};
  final Map<String, dynamic> device;
  final void Function(Map<String, dynamic>)? onArchive;
  int _serial = 0;
  String? _savedEncoding;

  // Actual player failures and remote commands outrank speculative API work.
  static int _priority(String reason) => reason.startsWith('api-') ||
          reason.startsWith('history-') ||
          const {
            'prepare-result',
            'search-response',
            'search-rendered',
            'suggest-response',
            'delivery-trace',
            'ui-frame-summary',
            'image-load-summary',
            'image-warmup'
          }.contains(reason)
      ? 0
      : 2;

  static int _auxiliaryIndex(List events) => events.indexWhere((row) =>
      '${row['event']}'.startsWith('api-') ||
      '${row['event']}'.startsWith('history-') ||
      const {
        'prepare-result',
        'search-response',
        'search-rendered',
        'suggest-response',
        'delivery-trace',
        'ui-frame-summary',
        'image-load-summary',
        'image-warmup'
      }.contains(row['event']));

  static int _budgetEventIndex(List events) {
    final auxiliary = _auxiliaryIndex(events);
    if (auxiliary >= 0) return auxiliary;
    // Keep remote commands and failure evidence before routine media events.
    final ordinary = events.indexWhere((row) =>
        row['event'] != 'media-action' &&
        row['failed'] != true &&
        !'${row['event']}'.contains('failed') &&
        !'${row['event']}'.contains('rejected') &&
        row['event'] != 'error');
    return ordinary >= 0 && ordinary < events.length - 1 ? ordinary : 0;
  }

  static int _budgetPerformanceIndex(List rows) {
    final auxiliary = rows.indexWhere((row) => const {
          'prepare',
          'search',
          'suggest',
          'render',
          'artwork',
          'history'
        }.contains(_performanceKind(row as Map)));
    if (auxiliary >= 0) return auxiliary;
    final intermediate = rows.indexWhere((row) =>
        row['event'] != 'playback-startup' &&
        row['event'] != 'resume-progress');
    return intermediate >= 0 ? intermediate : 0;
  }

  void _trimIncidents() {
    while (_incidents.length > 8) {
      var oldestLowest = 0;
      for (var i = 1; i < _incidents.length; i++) {
        if (_priority('${_incidents[i]['reason']}') <
            _priority('${_incidents[oldestLowest]['reason']}'))
          oldestLowest = i;
      }
      _incidents.removeAt(oldestLowest);
      _drop('incidents');
    }
  }

  PlaybackDiagnostics(this.device, {this.onArchive});

  static const _attemptKeys = {
    'resourceEntriesCompletedOnly',
    'resourceServerTimingSupported',
    'resourceServerTimingCount',
    'resourceInitiatorType',
    'resourceFirstAttemptMs',
    'resourceFirstAttemptBytes',
    'largestObservationGapMs',
    'largestObservationGapAt',
    'previousVisibility',
    'previousPaused',
    'wallGapMs',
    'monotonicGapMs',
    'nativePositionDeltaSeconds',
    'nativeEventDelayMs',
    'mediaSessionState',
    'mediaMetadataPresent',
    'observedMediaSessionState',
    'observedMediaMetadataPresent',
    'hiddenForMs',
    'hiddenPositionDeltaSeconds',
    'hiddenTrackChanged',
    'reportedSilence',
    'deliveryTraceStatus',
    'pageInstance',
    'pageAgeMs',
    'at',
    'session',
    'traceId',
    'track',
    'title',
    'artist',
    'sourceId',
    'cacheVariant',
    'cacheVersion',
    'build',
    'expectedDuration',
    'nativeDuration',
    'catalogueDuration',
    'sourceDuration',
    'durationDeltaSeconds',
    'startupMs',
    'nextGapMs',
    'resumeMs',
    'declaredMediaType',
    'stableResource',
    'prepared',
    'preparedAtStart',
    'startupBytesReady',
    'sourceAssignments',
    'sourceReloads',
    'loadStartMs',
    'metadataMs',
    'canPlayMs',
    'playingEventMs',
    'nativePlayCallMs',
    'playAcceptedMs',
    'firstProgressMs',
    'startupWaitCount',
    'phase',
    'time',
    'mediaCode',
    'failureCode',
    'visibility',
    'resourceTimingCount',
    'resourceFirstByteMs',
    'resourceTotalMs',
    'resourceTransferBytes',
    'resourceBodyBytes',
    'resourceProtocol',
    'resourceExpectedBodyBytes',
    'resourceContentRange',
    'resourceProbeOnly',
    'mediaRoute',
    'serverPlaybackRequest',
    'resourceHandlerMs',
    'resourceRouteWaitMs',
  };

  void _retainAttempt(Map<String, dynamic> row) {
    // API work inherits the active trace for context, not playback attribution.
    if (row.containsKey('path') || row['event'] == 'prepare-result') return;
    final trace = row['traceId'];
    if (trace is! String || !RegExp(r'^[a-f0-9]{32}$').hasMatch(trace)) return;
    var index = _attempts.indexWhere((item) => item['traceId'] == trace);
    if (index < 0) {
      // Only native loads establish an attempt. Preparation cannot evict plays.
      if (row['event'] != 'load') return;
      _attempts.add(
          {'traceId': trace, 'outcome': 'starting', 'startedAt': row['at']});
      if (_attempts.length > maxAttempts) {
        _attempts.removeAt(0);
        _drop('attempts');
      }
      index = _attempts.length - 1;
    }
    final attempt = _attempts[index];
    if (row['event'] == 'resource-samples-capped') {
      attempt['resourceSamplesCapped'] = true;
    }
    if (row['event'] == 'resource-delivery' &&
        row['resourceAttemptStartMs'] is num) {
      final transfers = (attempt['resourceTransfers'] as List?) ?? [];
      final sample = <String, dynamic>{
        for (final key in const {
          'resourceAttemptStartMs',
          'resourceAttemptDurationMs',
          'resourceAttemptFirstByteMs',
          'resourceAttemptConnectMs',
          'resourceAttemptTransferBytes',
          'resourceAttemptBodyBytes',
          'resourceAttemptStatusSupported',
          'resourceAttemptStatus',
          'resourceAttemptServerTimingSupported',
          'resourceAttemptServerTimingCount',
          'resourceAttemptInitiator',
          'resourceAttemptRequestId',
          'resourceAttemptEdgeId',
          'resourceAttemptExpectedBytes',
          'resourceAttemptContentRange'
        })
          if (row[key] != null) key: row[key]
      };
      if (!transfers.any((item) =>
          item is Map &&
          item['resourceAttemptStartMs'] == sample['resourceAttemptStartMs'])) {
        if (transfers.length < 12)
          transfers.add(sample);
        else
          attempt['resourceTransfersDropped'] =
              (attempt['resourceTransfersDropped'] as int? ?? 0) + 1;
      }
      attempt['resourceTransfers'] = transfers;
    }
    for (final key in _attemptKeys) {
      // An unavailable timing sample cannot erase a completed transfer from
      // this exact trace. A different load has its own attempt and fields.
      if (key == 'resourceTimingCount' &&
          row[key] == 0 &&
          (attempt[key] as num? ?? 0) > 0) continue;
      if (key == 'prepared' &&
          !const {'load', 'playback-startup'}.contains(row['event'])) continue;
      if (row[key] != null) attempt[key] = row[key];
    }
    if (row['event'] == 'playback-startup' && row['prepared'] is bool) {
      attempt['preparedAtStart'] ??= row['prepared'];
    }
    if (row['event'] == 'playback-startup') attempt['outcome'] = 'started';
    if (row['event'] == 'observation-gap' && row['wallGapMs'] is num) {
      final largest = attempt['largestObservationGapMs'] as num? ?? 0;
      if (row['wallGapMs'] > largest) {
        attempt['largestObservationGapMs'] = row['wallGapMs'];
        attempt['largestObservationGapAt'] = row['at'];
      }
    }
    if (row['event'] == 'ended') attempt['outcome'] = 'ended';
    if (row['event'] == 'error' ||
        row['event'] == 'source-error' ||
        row['event'] == 'source-failure') {
      attempt['outcome'] = 'media_failed';
      attempt['failedAt'] ??= row['at'];
    }
    if (row['event'] == 'playback-interrupted' ||
        row['event'] == 'audio-session-interrupted') {
      attempt['interruptedAt'] = row['at'];
      attempt['interruptionCount'] =
          (attempt['interruptionCount'] as int? ?? 0) + 1;
    }
    if (row['event'] == 'resume-progress') {
      attempt['resumeCount'] = (attempt['resumeCount'] as int? ?? 0) + 1;
    }
    if (row['event'] == 'playback-replaced' && attempt['outcome'] == 'starting')
      attempt['outcome'] = 'replaced_before_start';
  }

  void attachDelivery(String traceId, Map raw) {
    _savedEncoding = null;
    final index = _attempts.indexWhere((item) => item['traceId'] == traceId);
    if (raw['traceId'] != traceId || raw['requests'] is! List) return;
    final target = index < 0 ? <String, dynamic>{} : _attempts[index];
    const fields = {
      'serverInstance',
      'serverProcessAgeMs',
      'sourceLeaseSeconds',
      'validationAgeMs',
      'mediaTotalBytes',
      'cacheHeadMemoryHits',
      'cacheHeadSharedWaitMs',
      'cacheHeadUnfinished',
      'cacheHeadDetailsDropped',
      'acquisitionAttemptsDropped',
      'cookieSessionState',
      'providerSessionInvalid',
      'requestId',
      'at',
      'edgeRequestId',
      'deliveryHop',
      'protocol',
      'requestedRange',
      'disconnectObserved',
      'bodyChunks',
      'bodyExpectedBytes',
      'bodyRemainingBytes',
      'transferComplete',
      'responseCompleted',
      'allExpectedBytesSent',
      'bodyCompleteMs',
      'disconnectBeforeBodyComplete',
      'lastBodyBytes',
      'lastReceiveType',
      'closureCategory',
      'disconnectMs',
      'lastSendMs',
      'sendExceptionType',
      'sendFailureMs',
      'failedSendBytes',
      'result',
      'status',
      'bytes',
      'sourceId',
      'trackKey',
      'cacheVariant',
      'cacheVersion',
      'range',
      'mediaType',
      'contentLength',
      'contentRange',
      'cacheTier',
      'routeWaitMs',
      'handlerMs',
      'headersMs',
      'cacheLookupMs',
      'objectFirstReadMs',
      'resolutionWaitMs',
      'resolutionId',
      'firstYieldMs',
      'iteratorWaitMs',
      'firstSendMs',
      'firstSendDoneMs',
      'sendWaitMs',
      'maxSendWaitMs',
      'totalMs',
      'failureCode',
      'failureStage',
      'exceptionType',
      'workersAtEntryBorrowed',
      'workersAtEntryTotal',
      'workersAtEntryWaiting',
      'workersAtFirstSendBorrowed',
      'workersAtFirstSendTotal',
      'workersAtFirstSendWaiting',
      'iteratorEnterMs',
      'prefixWarmMs',
      'iteratorReadMs',
      'maxIteratorReadMs',
      'workerReadWaitMs',
      'workersBeforeIteratorBorrowed',
      'workersBeforeIteratorTotal',
      'workersBeforeIteratorWaiting',
      'extractMs',
      'validateMs',
      'persistedLookupMs',
      'extractSlotMs',
      'extractSetupMs',
      'tokenHeadersMs',
      'playerHeadersMs',
      'storedAudioAtStart',
      'prefixAtStart',
      'resolvedUrlAtStart',
      'acquisitionInFlightAtStart',
      'persistedUrlAtStart',
      'resolutionOrigin',
      'resolutionMetricsReused',
      'resolutionAttempts',
      'freshRetryReason',
      'freshRetryClient',
      'sharedResolutionWaitMs',
      'sourceCandidateMs',
      'sourceResolveMs',
      'sourceCandidates',
      'sourceAttempts',
      'acquisitionPath',
      'directClient',
      'audioCodec',
      'formatId',
      'extractorReused',
      'bootstrapReused',
      'initializeMs',
      'bootstrapMs',
      'solverMs',
      'tokenBodyMs',
      'playerBodyMs',
      'watchBodyMs',
      'scriptBodyMs',
      'watchHeadersMs',
      'scriptHeadersMs',
      'tokenRequests',
      'playerRequests',
      'watchRequests',
      'mediaHeadersMs',
      'mediaFirstChunkMs',
      'mediaPrefixMs',
      'mediaPrefixBytes',
      'mediaStatus',
      'startupReadRetained',
    };
    target['delivery'] = [
      for (final row in (raw['requests'] as List).whereType<Map>().take(32))
        {
          for (final key in fields)
            if (row[key] is String ||
                row[key] is bool ||
                row[key] is num && (row[key] as num).isFinite)
              key: row[key] is String ? safeText(row[key]) : row[key],
          for (final name in const ['cacheHeads', 'acquisitionAttempts'])
            if (row[name] is List)
              name: [
                for (final detail in (row[name] as List)
                    .whereType<Map>()
                    .take(name == 'cacheHeads' ? 8 : 6))
                  {
                    for (final key in {
                      ...fields,
                      'variant',
                      'queueMs',
                      'headMs',
                      'result',
                      'status',
                      'errorType',
                      'attempt',
                      'startMs',
                      'sourceDuration',
                      'failureHttpStatus',
                      'failureFlags'
                    })
                      if (detail[key] is String ||
                          detail[key] is bool ||
                          detail[key] is num && (detail[key] as num).isFinite)
                        key: detail[key] is String
                            ? safeText(detail[key])
                            : detail[key],
                  }
              ],
          if (row['resolutions'] is List)
            'resolutions': [
              for (final resolution
                  in (row['resolutions'] as List).whereType<Map>().take(3))
                {
                  for (final key in const [
                    'resolutionId',
                    'sourceId',
                    'extractMs',
                    'validateMs',
                    'persistedLookupMs',
                    'resolutionOrigin',
                    'resolutionAttempts',
                    'freshRetryReason',
                    'freshRetryClient',
                    'directClient',
                    'persistedUrlAtStart',
                  ])
                    if (resolution[key] is String ||
                        resolution[key] is bool ||
                        resolution[key] is num &&
                            (resolution[key] as num).isFinite)
                      key: resolution[key] is String
                          ? safeText(resolution[key])
                          : resolution[key]
                }
            ],
        }
    ];
    final fingerprint =
        jsonEncode([target['delivery'], raw['droppedRequests']]);
    if (_deliveryFingerprints[traceId] == fingerprint) return;
    _deliveryFingerprints[traceId] = fingerprint;
    while (_deliveryFingerprints.length > maxAttempts)
      _deliveryFingerprints.remove(_deliveryFingerprints.keys.first);
    onArchive?.call({
      'event': 'delivery-snapshot',
      'traceId': traceId,
      'at': DateTime.now().toUtc().toIso8601String(),
      'requests': target['delivery'],
      'droppedRequests':
          raw['droppedRequests'] is num ? raw['droppedRequests'] : 0
    });
  }

  static String? _performanceKind(Map row) {
    final event = '${row['event']}';
    if (event == 'ui-frame-summary') return 'render';
    if (event == 'image-load-summary' || event == 'media-artwork')
      return 'artwork';
    if (event == 'api-response' && row['path'] == '/history_seed')
      return 'history';
    if (event == 'suggest-response') return 'suggest';
    if (event == 'search-response' ||
        event == 'search-rendered' ||
        event == 'api-failure' &&
            row['failureCode'] != 'request_aborted' &&
            const {'/search', '/suggest'}.contains(row['path']))
      return 'search';
    if (event.startsWith('resume-')) return 'resume';
    if (event == 'playback-startup') return 'playback';
    if (event == 'prepare-result') return 'prepare';
    return null;
  }

  void _retainPerformance(Map<String, dynamic> row) {
    final kind = _performanceKind(row);
    if (kind == null) return;
    _performance.add({
      for (final key in const [
        ..._visualFields,
        'at',
        'event',
        'track',
        'session',
        'build',
        'ms',
        'startupMs',
        'declaredMediaType',
        'stableResource',
        'pinnedSource',
        'sourceAssignments',
        'sourceReloads',
        'loadStartMs',
        'metadataMs',
        'canPlayMs',
        'playingEventMs',
        'nativePlayCallMs',
        'playAcceptedMs',
        'firstProgressMs',
        'startupWaitCount',
        'resourceTimingCount',
        'resourceFirstByteMs',
        'resourceTotalMs',
        'resourceTransferBytes',
        'resourceBodyBytes',
        'resourceStartMs',
        'resourceRequestMs',
        'resourceResponseMs',
        'resourceConnectMs',
        'resourceDnsMs',
        'resourceProtocol',
        'nextGapMs',
        'resumeMs',
        'pauseMs',
        'ready',
        'bufferedEnd',
        'bufferedRanges',
        'bufferedAheadSeconds',
        'seekableRanges',
        'pendingSeekSeconds',
        'requestedPosition',
        'restoredPosition',
        'sourceVerified',
        'choice',
        'nativeDuration',
        'expectedDuration',
        'playbackRate',
        'time',
        'visibility',
        'sourceRetained',
        'fullyBuffered',
        'reason',
        'kind',
        'requestId',
        'requestBytes',
        'transportRoute',
        'path',
        'resultCount',
        'preview',
        'inputToResultsMs',
        'queryLength',
        'first_response_contract_ms',
        'direct_lookup_ms',
        'cacheTier',
        'prepared',
        'fullyCached',
        'failureCode',
        'requestedTrack',
        'requestPurpose',
        'failureStage',
        'failureType',
        'failureHttpStatus',
        'serverMs',
        'serverRequestId',
        'searchSurface',
        'searchOutcome',
        'searchRefreshRetained',
        'searchProviderFailures',
        'searchProvider_songs',
        'searchProvider_videos',
        'searchProvider_artists',
        'searchProvider_top',
        'snapshotHit',
        'retrievalMs',
        'rankingMs',
        'essential_targetMs',
        'artist_surface_completionMs',
        'snapshot_storeMs',
        'localIndexMs',
        'providerTimeoutCount'
      ])
        if (row[key] != null) key: row[key],
    });
    final limit = kind == 'resume' || kind == 'search' ? 6 : 4;
    while (_performance.where((item) => _performanceKind(item) == kind).length >
        limit) {
      _performance.removeAt(
          _performance.indexWhere((item) => _performanceKind(item) == kind));
    }
  }

  static String safeText(String value) {
    final text = value
        .replaceAll(RegExp(r'https?://[^\s]+'), '[url]')
        .replaceAll(
            RegExp(r'Bearer\s+\S+', caseSensitive: false), '[credential]')
        .replaceAll(
            RegExp(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
            '[credential]');
    final output = StringBuffer();
    var bytes = 0;
    for (final rune in text.runes) {
      final character = String.fromCharCode(rune);
      // Budget the encoded JSON bytes, including escapes in error messages.
      bytes += rune < 0x20
          ? const {8, 9, 10, 12, 13}.contains(rune)
              ? 2
              : 6
          : rune == 0x22 || rune == 0x5c
              ? 2
              : utf8.encode(character).length;
      if (bytes > 320) break;
      output.write(character);
    }
    return output.toString();
  }

  Map<String, dynamic> _device(Map? raw) => {
        for (final key in [
          'userAgent',
          'platform',
          'language',
          'navigationType'
        ])
          if (raw?[key] != null) key: safeText('${raw![key]}'),
        if (raw?['pageWasDiscarded'] is bool)
          'pageWasDiscarded': raw!['pageWasDiscarded'],
      };

  Map<String, dynamic> _safe(Map row) => {
        for (final entry in row.entries)
          if (_allowed.contains(entry.key) &&
              _safeTransferField(entry.key, entry.value) &&
              (entry.value == null ||
                  entry.value is String ||
                  entry.value is bool ||
                  entry.value is num && (entry.value as num).isFinite))
            entry.key.toString():
                entry.value is String ? safeText(entry.value) : entry.value,
      };

  bool _safeTransferField(Object? key, Object? value) {
    if (value == null) return true;
    switch (key) {
      case 'candidateMediaType':
        return const {'audio/mp4', 'audio/webm', 'audio/mpeg', 'audio/ogg', 'audio/wav'}.contains(value);
      case 'topResultType':
        return const {'track', 'artist', 'album'}.contains(value);
      case 'topResultId':
        return value is String && RegExp(r'^[A-Za-z0-9:_-]{1,256}$').hasMatch(value);
      case 'imagePlayerKey':
        return value is String &&
            RegExp(r'^[A-Za-z0-9_-]{1,64}$').hasMatch(value);
      case 'imagePlayerOutcome':
      case 'imageSlowOutcome':
        return const {'ready', 'failed', 'no-source'}.contains(value);
      case 'imageSlowKey':
        return value is String &&
            RegExp(r'^[A-Za-z0-9_:-]{1,80}$').hasMatch(value);
      case 'candidateTraceId':
      case 'resourceAttemptRequestId':
        return value is String && RegExp(r'^[a-f0-9]{32}$').hasMatch(value);
      case 'resourceAttemptEdgeId':
        return value is String &&
            RegExp(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
                .hasMatch(value);
      case 'resourceAttemptExpectedBytes':
        return value is int && value >= 0 && value <= 999999999999999;
      case 'resourceAttemptContentRange':
        return value is String &&
            RegExp(r'^bytes [0-9]{1,15}-[0-9]{1,15}/[0-9]{1,15}$')
                .hasMatch(value);
      default:
        return true;
    }
  }

  void restore(String? saved) {
    _savedEncoding = null;
    _recent.clear();
    _incidents.clear();
    _performance.clear();
    _attempts.clear();
    _timeline.clear();
    _dropped.clear();
    if (saved == null || utf8.encode(saved).length > maxSavedBytes) return;
    try {
      final decoded = jsonDecode(saved);
      if (decoded is Map && decoded['attempts'] is List) {
        for (final raw in (decoded['attempts'] as List)
            .whereType<Map>()
            .take(maxAttempts)) {
          final safe = _safe(raw);
          _retainAttempt({...safe, 'event': 'load'});
          final trace = safe['traceId'];
          if (trace is String) {
            for (final transfer in (raw['resourceTransfers'] is List
                    ? raw['resourceTransfers'] as List
                    : const [])
                .whereType<Map>()
                .take(12)) {
              _retainAttempt({
                ..._safe(transfer),
                'traceId': trace,
                'event': 'resource-delivery'
              });
            }
            attachDelivery(
                trace, {'traceId': trace, 'requests': raw['delivery']});
            final index = _attempts.indexWhere((x) => x['traceId'] == trace);
            if (index >= 0 &&
                const {
                  'starting',
                  'started',
                  'ended',
                  'media_failed',
                  'replaced_before_start'
                }.contains(raw['outcome']))
              _attempts[index]['outcome'] = raw['outcome'];
            if (index >= 0) {
              if (raw['resourceSamplesCapped'] == true) {
                _attempts[index]['resourceSamplesCapped'] = true;
              }
              if (raw['resourceTransfersDropped'] is int &&
                  raw['resourceTransfersDropped'] >= 0) {
                _attempts[index]['resourceTransfersDropped'] =
                    raw['resourceTransfersDropped'];
              }
              for (final key in [
                'startedAt',
                'failedAt',
                'interruptedAt',
                'interruptionCount',
                'resumeCount'
              ]) {
                if (safe[key] != null) _attempts[index][key] = safe[key];
              }
            }
          }
        }
      }
      if (decoded is Map && decoded['timeline'] is List) {
        _timeline.addAll((decoded['timeline'] as List)
            .whereType<Map>()
            .take(maxTimeline)
            .map(_safe));
      }
      if (decoded is Map && decoded['retention'] is Map) {
        for (final entry in (decoded['retention'] as Map).entries) {
          if (const {'attempts', 'timeline', 'incidents', 'storageWritesFailed'}
                  .contains(entry.key) &&
              entry.value is int &&
              entry.value >= 0) {
            _dropped[entry.key as String] = entry.value as int;
          }
        }
      }
      if (decoded is Map && decoded['performance'] is List) {
        for (final item
            in (decoded['performance'] as List).whereType<Map>().take(24)) {
          _retainPerformance(_safe(item));
        }
      }
      final entries = decoded is List ? decoded : decoded['incidents'] as List;
      for (final item in entries.reversed.take(8).toList().reversed) {
        final rows = item is List ? item : item['events'] as List;
        final events = rows.whereType<Map>().take(24).map(_safe).toList();
        if (events.isEmpty) continue;
        _incidents.add({
          'id': item is Map ? safeText('${item['id']}') : 'legacy-${_serial++}',
          'reason':
              item is Map ? safeText('${item['reason']}') : 'previous-failure',
          'at': item is Map && item['at'] is String
              ? safeText(item['at'])
              : events.last['at'],
          'track': events.last['track'],
          'session': events.last['session'],
          'events': events,
          'device': item is Map
              ? _device(item['device'] as Map?)
              : {'userAgent': 'not retained'},
          'context': item is Map && item['context'] is Map
              ? _safe(item['context'])
              : events.last
        });
      }
    } catch (_) {/* Invalid/legacy storage must not block playback. */}
  }

  void record(Map<String, dynamic> row) {
    _savedEncoding = null;
    final safe = _safe(row);
    onArchive?.call(safe);
    _retainAttempt(safe);
    _retainPerformance(safe);
    _recent.add(safe);
    if (_recent.length > 80) _recent.removeAt(0);
    if (!const {'timeupdate', 'progress', 'api-response', 'delivery-observed'}
        .contains(safe['event'])) {
      _timeline.add({
        for (final key in const {
          ..._homeFields,
          'serverRequestId',
          'requestId',
          'requestPurpose',
          'serverMs',
          'ms',
          'at',
          'event',
          'traceId',
          'track',
          'session',
          'pageInstance',
          'pageAgeMs',
          'time',
          'phase',
          'visibility',
          'audioSession',
          'pagePersisted',
          'wallGapMs',
          'monotonicGapMs',
          'nativePositionDeltaSeconds',
          'previousVisibility',
          'previousPaused',
          'observationTrigger',
          'nativeEventDelayMs',
          'nativeEventTrusted',
          'hiddenForMs',
          'hiddenPositionDeltaSeconds',
          'reportedSilence',
          'playRequested',
          'mediaCode',
          'failureCode',
          'reason',
          'action',
          'kind',
          'startupMs',
          'resumeMs',
          'sourceReloads',
          'sourceMutationReason',
          'sourceVerified',
          'choice',
          'bufferedAheadSeconds',
          'seekableRanges',
          'pendingSeekSeconds',
          'requestedPosition',
          'restoredPosition',
          'mediaSessionState',
          'mediaMetadataPresent',
          'mediaHandlers',
          'mediaPositionAvailable',
          'paused',
          'sourceMatches',
          'fullyBuffered',
          'failed'
        })
          if (safe[key] != null) key: safe[key]
      });
      if (_timeline.length > maxTimeline) {
        final auxiliary = _auxiliaryIndex(_timeline);
        _timeline.removeAt(auxiliary < 0 ? 0 : auxiliary);
        _drop('timeline');
      }
    }
    // Keep a short aftermath, without appending unrelated preparation or tracks.
    if (_incidents.isNotEmpty) {
      final latest = _incidents.last;
      final events = latest['events'] as List;
      if (latest['session'] == safe['session'] &&
          latest['track'] == safe['track']) {
        if (events.length >= 24 && _priority('${safe['event']}') > 0) {
          final auxiliary = _auxiliaryIndex(events);
          if (auxiliary >= 0) events.removeAt(auxiliary);
        }
        if (events.length < 24) events.add(safe);
      }
    }
    final event = '${safe['event']}';
    final failure = event == 'error' ||
        event == 'playback-interrupted' ||
        event == 'media-aborted' ||
        event == 'api-failure' && safe['failureCode'] != 'request_aborted' ||
        event.contains('failed') ||
        event.contains('rejected') ||
        safe['failed'] == true ||
        safe['status'] is num && safe['status'] >= 400;
    // A manual silence report is an incident even without a native error.
    // Keep ownership here so replaying the archive reconstructs it too.
    if (failure || event == 'silence-reported') capture(event);
  }

  void capture(String reason) {
    _savedEncoding = null;
    // Repeated state captures are read-only reports, never incident eviction.
    if (reason == 'user-report') return;
    final last = _recent.isEmpty ? <String, dynamic>{} : _recent.last;
    final events = _recent
        .where((row) =>
            row['session'] == last['session'] && row['track'] == last['track'])
        .toList()
        .reversed
        .take(16)
        .toList()
        .reversed
        .toList();
    final repeated = _incidents.lastOrNull;
    if (repeated != null &&
        repeated['reason'] == reason &&
        repeated['session'] == last['session'] &&
        repeated['track'] == last['track']) return;
    _incidents.add({
      'id':
          '${DateTime.now().toUtc().microsecondsSinceEpoch.toRadixString(36)}-${_serial++}',
      'reason': safeText(reason),
      'at': last['at'],
      'track': last['track'],
      'session': last['session'],
      'events': events,
      'context': last,
      'device': _device(device)
    });
    _trimIncidents();
  }

  String encodeSaved() {
    if (_savedEncoding != null) return _savedEncoding!;
    final source = <String, dynamic>{
      'version': 3,
      'incidents': _incidents,
      'performance': _performance,
      'attempts': _attempts,
      'timeline': _timeline,
      'retention': {..._dropped},
    };
    final encoded = jsonEncode(source);
    final bytes = utf8.encode(encoded).length;
    if (bytes <= maxSavedBytes)
      return _savedEncoding = encoded;
    final output = _copy(source);
    _bound(output, maxSavedBytes, pretty: false, initialBytes: bytes);
    return _savedEncoding = jsonEncode(output);
  }

  Map<String, dynamic> report({bool current = false, bool full = false}) {
    final lastRow = _recent.lastOrNull ?? <String, dynamic>{};
    var incident = current || _incidents.isEmpty
        ? <String, dynamic>{
            'id':
                'current-${DateTime.now().toUtc().microsecondsSinceEpoch.toRadixString(36)}',
            'reason': 'user-report',
            'at': lastRow['at'],
            'track': lastRow['track'],
            'session': lastRow['session'],
            'context': lastRow,
            'device': _device(device),
            'events': _recent
                .where((row) =>
                    row['track'] == lastRow['track'] &&
                    row['session'] == lastRow['session'])
                .toList()
                .reversed
                .take(24)
                .toList()
                .reversed
                .toList(),
          }
        : _incidents.last;
    if (!current && _incidents.isNotEmpty) {
      for (final item in _incidents.reversed) {
        if (_priority('${item['reason']}') > _priority('${incident['reason']}'))
          incident = item;
      }
    }
    final events = incident['events'] as List;
    final last = incident['context'] as Map? ??
        (events.isEmpty ? <String, dynamic>{} : events.last as Map);
    final output = _copy({
      'version': 3,
      'reportType': full ? 'session' : 'compact',
      'reportId': incident['id'],
      'createdAt': DateTime.now().toUtc().toIso8601String(),
      'device': incident['device'],
      'summary': {
        'reason': incident['reason'],
        'occurredAt': incident['at'],
        'build': last['build'],
        'phase': last['phase'],
        'mediaCode': last['mediaCode'],
        'visibility': last['visibility'],
        'audioSession': last['audioSession'],
        'track': incident['track'],
        'session': incident['session'],
        'requestedTrack': last['requestedTrack'],
        'failureCode': last['failureCode'],
        'cacheTier': last['cacheTier'],
        'requestPurpose': last['requestPurpose']
      },
      'events': events,
      'performance': _performance,
      'attempts': _attempts,
      'timeline': _timeline,
      'retention': {
        ..._dropped,
        'attemptLimit': maxAttempts,
        'timelineLimit': maxTimeline
      },
      'incidents': [
        for (final item in _incidents)
          {
            'reportId': item['id'],
            'reason': item['reason'],
            'at': item['at'],
            'track': item['track'],
            'session': item['session'],
            'context': {
              for (final key in const [
                'mediaCode',
                'mediaMessage',
                'phase',
                'time',
                'nativeDuration',
                'expectedDuration',
                'bufferedRanges',
                'playbackRate',
                'paused',
                'ready',
                'network',
                'bufferedEnd',
                'visibility',
                'audioSession',
                'wallGapMs',
                'monotonicGapMs',
                'nativePositionDeltaSeconds',
                'mediaSessionState',
                'mediaMetadataPresent',
                'reportedSilence',
                'cause',
                'failureCode',
                'requestPurpose',
                'recoveryAttempts',
                'build'
              ])
                if ((item['context'] as Map?)?[key] != null)
                  key: (item['context'] as Map)[key],
            },
            if (full) 'events': item['events'],
          }
      ],
    });
    _bound(output, full ? maxSavedBytes : 32768);
    return output;
  }

  void _drop(String key) {
    _savedEncoding = null;
    _dropped[key] = (_dropped[key] ?? 0) + 1;
  }

  void storageWriteFailed() => _drop('storageWritesFailed');

  // Budgeting changes only list membership, attempt/incident maps and retention.
  // Copy those owners; immutable event/delivery bodies need no JSON round trip.
  static Map<String, dynamic> _copy(Map<String, dynamic> value) => {
        ...value,
        for (final key in const ['attempts', 'incidents', 'events', 'timeline', 'performance'])
          if (value[key] is List)
            key: [
              for (final row in value[key] as List)
                if ((key == 'attempts' || key == 'incidents') && row is Map)
                  {
                    ...row,
                    for (final entry in row.entries)
                      if (entry.value is List) entry.key: List.of(entry.value as List),
                  }
                else
                  row,
            ],
        'retention': {...value['retention'] as Map},
      };

  static void _bound(Map<String, dynamic> output, int limit,
      {bool pretty = true, int? initialBytes}) {
    final encoder = pretty ? const JsonEncoder.withIndent('  ') : const JsonEncoder();
    int size(dynamic value, {int depth = 0}) {
      final text = encoder.convert(value);
      // A standalone pretty fragment gains its surrounding indentation on
      // every line. Replacement deltas cancel the unchanged first-line prefix.
      return utf8.encode(text).length +
          (pretty ? depth * 2 * ('\n'.allMatches(text).length + 1) : 0);
    }
    var remaining = initialBytes ?? size(output);
    void remove(List rows, int index, {int depth = 1}) {
      final count = rows.length;
      final removed = size(rows.removeAt(index), depth: depth + 1);
      remaining -= pretty
          ? removed + (count > 1 ? 2 : 2 + depth * 2)
          : removed + (count > 1 ? 1 : 0);
    }
    final retention = output['retention'] as Map;
    void dropped(String key) {
      final before = size(retention, depth: 1);
      retention[key] = (retention[key] as int? ?? 0) + 1;
      remaining += size(retention, depth: 1) - before;
    }
    final attempts = output['attempts'] as List;
    final performance = output['performance'] as List;
    final timeline = output['timeline'] as List;
    final incidents = output['incidents'] as List;
    final events = output['events'] as List? ?? [];
    while (remaining > limit) {
      // Detailed delivery rows are much larger than the essential play ledger.
      final detailed = attempts
          .cast<Map>()
          .where((a) => a['delivery'] is List && a['deliveryReduced'] != true)
          .firstOrNull;
      if (detailed != null) {
        final before = size(detailed, depth: 2);
        detailed['delivery'] = [
          for (final raw in detailed['delivery'] as List)
            {
              for (final key in const {
                'requestId',
                'edgeRequestId',
                'deliveryHop',
                'disconnectObserved',
                'responseCompleted',
                'allExpectedBytesSent',
                'bodyCompleteMs',
                'disconnectBeforeBodyComplete',
                'disconnectMs',
                'sendExceptionType',
                'failedSendBytes',
                'sourceId',
                'status',
                'result',
                'cacheVariant',
                'storedAudioAtStart',
                'resolutionOrigin',
                'failureCode',
                'failureStage',
                'cacheLookupMs',
                'extractMs',
                'validateMs',
                'firstSendMs',
                'totalMs',
                'range',
                'bytes'
              })
                if ((raw as Map)[key] != null) key: raw[key],
              if (raw['acquisitionAttempts'] is List)
                'acquisitionAttempts': [
                  for (final detail in (raw['acquisitionAttempts'] as List)
                      .whereType<Map>()
                      .take(6))
                    {
                      for (final key in const [
                        'directClient',
                        'result',
                        'extractMs',
                        'validateMs',
                        'failureStage',
                        'failureHttpStatus',
                        'cookieSessionState'
                      ])
                        if (detail[key] != null) key: detail[key],
                    }
                ],
            }
        ];
        detailed['deliveryReduced'] = true;
        remaining += size(detailed, depth: 2) - before;
        dropped('deliveryDetailsReduced');
      } else if (events.length > 8) {
        remove(events, _budgetEventIndex(events));
        dropped('exportEvents');
      } else if (incidents
          .cast<Map>()
          .any((i) => (i['events'] as List? ?? []).length > 4)) {
        final rows = incidents.cast<Map>().firstWhere(
            (i) => (i['events'] as List? ?? []).length > 4)['events'] as List;
        remove(rows, _budgetEventIndex(rows), depth: 3);
        dropped('incidentDetailsReduced');
      } else if (timeline.length > (limit == 32768 ? 24 : 64)) {
        remove(timeline, 0);
        dropped('exportTimeline');
      } else if (performance.length > 4) {
        remove(performance, _budgetPerformanceIndex(performance));
        dropped('exportPerformance');
      } else if (attempts.cast<Map>().any((a) => a['summaryOnly'] != true)) {
        final attempt =
            attempts.cast<Map>().firstWhere((a) => a['summaryOnly'] != true);
        final before = size(attempt, depth: 2);
        final summary = {
          for (final key in const {
            'traceId',
            'pageInstance',
            'track',
            'title',
            'artist',
            'sourceId',
            'startedAt',
            'at',
            'outcome',
            'cacheVariant',
            'startupMs',
            'firstProgressMs',
            'resumeMs',
            'failureCode',
            'mediaCode',
            'failedAt',
            'interruptedAt',
            'interruptionCount',
            'resumeCount',
            'visibility',
            'largestObservationGapMs',
            'largestObservationGapAt',
            'reportedSilence'
          })
            if (attempt[key] != null) key: attempt[key],
          'summaryOnly': true
        };
        attempt
          ..clear()
          ..addAll(summary);
        remaining += size(attempt, depth: 2) - before;
        dropped('attemptDetailsReduced');
      } else if (timeline.isNotEmpty) {
        remove(timeline, 0);
        dropped('exportTimeline');
      } else if (attempts.length > 1) {
        remove(attempts, 0);
        dropped('exportAttempts');
      } else if (performance.isNotEmpty) {
        remove(performance, _budgetPerformanceIndex(performance));
        dropped('exportPerformance');
      } else if (incidents.length > 1) {
        remove(incidents, 0);
        dropped('exportIncidents');
      } else if (events.length > 1) {
        remove(events, _budgetEventIndex(events));
        dropped('exportEvents');
      } else {
        break;
      }
    }
  }

  static String summary(Map<String, dynamic> report) {
    final info = report['summary'] as Map;
    return 'Report ${report['reportId']}\n${info['occurredAt'] ?? report['createdAt']}\n'
        'Issue: ${info['reason']} · State: ${info['phase'] ?? 'unknown'}\n'
        'Build: ${info['build'] ?? 'unknown'} · Media error: ${info['mediaCode'] ?? 'none'}\n'
        'Page: ${info['visibility'] ?? 'unknown'} · Audio session: ${info['audioSession'] ?? 'unavailable'}\n'
        'Device: ${(report['device'] as Map?)?['userAgent'] ?? 'not retained'}\n'
        'Retained: ${(report['attempts'] as List?)?.length ?? 0} plays, '
        '${(report['timeline'] as List?)?.length ?? 0} timeline events · '
        'Storage failures: ${(report['retention'] as Map?)?['storageWritesFailed'] ?? 0}';
  }
}
