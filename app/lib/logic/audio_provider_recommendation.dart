import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider_request_builder.dart';
import 'auth_provider.dart' show authProvider;
import 'history_manager.dart';
import 'home_feed_cache.dart' as home_feed_cache;
import 'interaction_events.dart' show recommendationSignalStream;
import 'proxy_runtime.dart';
import 'recommendation_api_client.dart';
import 'recommendation_feed_models.dart';
import 'track_metadata.dart';

const String freshAccountHomeMessage =
    'Start listening and exploring. Neatie will walk with you and shape this space around your taste.';

class RecommendationNotifier extends StateNotifier<RecommendationFeedState> {
  final Ref ref;
  RecommendationNotifier(this.ref) : super(const RecommendationFeedState()) {
    _historyTrackSubscription = HistoryManager.trackStream.listen((_) {
      _scheduleSignalDrivenBackgroundRefresh();
    });
    _recommendationSignalSubscription = recommendationSignalStream.listen((_) {
      _scheduleSignalDrivenBackgroundRefresh();
    });
  }
  bool isLoading = true;
  final Set<String> _paginatingRows = <String>{};
  int _requestVersion = 0;
  bool _startupHealthChecked = false;
  Timer? _backgroundRefreshTimer;
  Timer? _queueStatusTimer;
  DateTime? _queueStatusDeadline;
  bool _queueStatusRequestInFlight = false;
  Duration _queueStatusPollDelay = Duration.zero;
  Future<void> _feedOperationTail = Future<void>.value();
  String _backgroundRefreshKey = '';
  String _lastCompletedBackgroundRefreshKey = '';
  final String _launchToken = 'launch-${DateTime.now().microsecondsSinceEpoch}';
  DateTime? _lastCompletedBackgroundRefreshAt;
  Future<void>? _explicitRefreshFuture;
  final List<String> _queuedSessionArtistHints = <String>[];
  final List<String> _queuedSessionQueries = <String>[];
  StreamSubscription<Map<String, dynamic>>? _historyTrackSubscription;
  StreamSubscription<String>? _recommendationSignalSubscription;

  /// Serializes every feed lifecycle operation.  The backend owns promotion;
  /// this guard only prevents overlapping client requests from cancelling or
  /// overwriting one another (for example two simultaneous pull gestures).
  Future<void> _runSerialized(Future<void> Function() operation) {
    final next = _feedOperationTail.then((_) => operation());
    _feedOperationTail = next.catchError((Object _) {});
    return next;
  }

  bool get isPaginating => _paginatingRows.isNotEmpty;
  bool get hasMorePages => state.rows.any((row) => row.hasMore);
  bool isRowPaginating(String rowId) => _paginatingRows.contains(rowId);

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  List<String> _mergeSessionValues(
    Iterable<String> primary,
    Iterable<String> queued, {
    int limit = 8,
  }) {
    final values = <String>[];
    final seen = <String>{};
    for (final rawValue in <String>[...primary, ...queued]) {
      final value = rawValue.trim();
      if (value.isEmpty) continue;
      final normalized = value.toLowerCase();
      if (!seen.add(normalized)) continue;
      values.add(value);
      if (values.length >= limit) break;
    }
    return values;
  }

  bool get _hasQueuedSessionIntent =>
      _queuedSessionArtistHints.isNotEmpty || _queuedSessionQueries.isNotEmpty;

  void queueSessionIntent({
    List<String> artistHints = const <String>[],
    List<String> sessionQueries = const <String>[],
  }) {
    final existingArtistHints = List<String>.from(_queuedSessionArtistHints);
    final existingQueries = List<String>.from(_queuedSessionQueries);
    _queuedSessionArtistHints
      ..clear()
      ..addAll(_mergeSessionValues(
        existingArtistHints,
        artistHints,
        limit: 8,
      ));
    _queuedSessionQueries
      ..clear()
      ..addAll(_mergeSessionValues(
        existingQueries,
        sessionQueries,
        limit: 6,
      ));
  }

  void _clearQueuedSessionIntent() {
    _queuedSessionArtistHints.clear();
    _queuedSessionQueries.clear();
  }

  Future<bool> _ensureProxyHealthyAtStartup() async {
    if (_startupHealthChecked) return true;
    _startupHealthChecked = true;
    final healthy = await probeProxyHealth();
    if (!healthy) {
      debugProxyLog(
        'recommend',
        'startup health check failed for candidates=${compactDiagnosticValue(proxyBaseUrlCandidates)}',
      );
    }
    return healthy;
  }

  Future<void> bootstrap() async {
    final authState = ref.read(authProvider);
    if (authState.isConfigured && !authState.isInitialized) {
      isLoading = false;
      if (mounted) {
        state = state.copyWith(requestState: 'idle');
      }
      debugProxyLog(
        'recommend',
        'bootstrap deferred until auth initialization completes for scope=${authState.storageScopeId}',
      );
      return;
    }
    final cachedState = await _loadCachedHomeFeed();
    if (!mounted) return;
    var restoredCachedFeed = false;
    if (cachedState != null && cachedState.hasRows) {
      restoredCachedFeed = true;
      state = cachedState.copyWith(
        requestState: 'complete',
        clearError: true,
        diagnostics: {
          ...cachedState.diagnostics,
          'client_cache_hit': true,
          'client_cache_source': 'last_good_home_feed',
        },
      );
      isLoading = false;
    }
    final proxyHealthy = await _ensureProxyHealthyAtStartup();
    if (!mounted) return;
    if (!proxyHealthy) {
      isLoading = false;
      state = state.hasRows
          ? state.copyWith(clearError: true)
          : state.copyWith(
              requestState: 'failed',
              errorMessage:
                  'Recommendation engine is unreachable. Check proxy/server connection and refresh.',
            );
      return;
    }
    if (restoredCachedFeed) {
      // The local file is offline recovery only. Always reconcile it with the
      // server-owned active version without hiding the restored rows.
      await refreshFromSignals(
        forceRefresh: false,
        promoteReadyOnLaunch: true,
      );
      return;
    }

    await refreshFromSignals(forceRefresh: false, promoteReadyOnLaunch: true);
  }

  String _cacheScopeId() {
    final scope = ref.read(authProvider).storageScopeId.trim();
    return scope.isNotEmpty ? scope : 'guest';
  }

  Future<RecommendationFeedState?> _loadCachedHomeFeed() async {
    return home_feed_cache.loadCachedHomeFeed(_cacheScopeId());
  }

  Future<void> _storeCachedHomeFeed(RecommendationFeedState feedState) async {
    await home_feed_cache.storeCachedHomeFeed(
      scopeId: _cacheScopeId(),
      feedState: feedState,
    );
  }

  Future<void> refreshFromSignals({
    bool forceRefresh = false,
    bool promoteReadyOnLaunch = false,
  }) {
    // Public lifecycle entrypoint: enqueue once, then call private request
    // routines that never enqueue themselves.
    if (forceRefresh) {
      final running = _explicitRefreshFuture;
      if (running != null) return running;
      late final Future<void> operation;
      operation = _runSerialized(_refreshUntilFeedChanges).whenComplete(() {
        if (identical(_explicitRefreshFuture, operation)) {
          _explicitRefreshFuture = null;
        }
      });
      _explicitRefreshFuture = operation;
      return operation;
    }
    return _runSerialized(() => _refreshFromCurrentSignals(
          forceRefresh: false,
          promoteReadyOnLaunch: promoteReadyOnLaunch,
        ));
  }

  Future<void> _refreshFromCurrentSignals({
    required bool forceRefresh,
    String refreshToken = '',
    bool promoteReadyOnLaunch = false,
  }) async {
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    await loadRecommendations(
      seed,
      requestMode: forceRefresh
          ? RecommendationRequestMode.pullToRefresh
          : RecommendationRequestMode.launch,
      allowNetworkCloudQueries: false,
      refreshToken: refreshToken,
      promoteReadyOnLaunch: promoteReadyOnLaunch,
      feedRefreshWaitMs: forceRefresh ? 3000 : 0,
    );
  }

  Future<void> _refreshUntilFeedChanges() async {
    final startingVersion = state.feedVersion;
    final refreshToken =
        'pull-${DateTime.now().microsecondsSinceEpoch.toString()}';

    await _refreshFromCurrentSignals(
      forceRefresh: true,
      refreshToken: refreshToken,
    );
    if (!mounted || state.feedVersion > startingVersion) return;
    final readyDepth =
        (state.diagnostics['ready_feed_depth'] as num?)?.toInt() ??
            (state.diagnostics['ready_feed_count'] as num?)?.toInt() ??
            0;
    final readyTarget =
        (state.diagnostics['ready_feed_target_depth'] as num?)?.toInt() ?? 2;
    final preparing = readyDepth < readyTarget &&
        (state.preparationState == 'preparing' ||
            state.diagnostics['preparation_state'] == 'preparing' ||
            readyDepth == 0);
    if (preparing && state.hasRows) {
      state = state.copyWith(
        requestState: 'complete',
        errorMessage: 'Fresh recommendations are still being prepared.',
      );
    }
    debugProxyLog(
      'recommend',
      'pull outcome=unchanged waitMs=${state.diagnostics['refresh_wait_ms'] ?? 3000} queueDepth=${state.diagnostics['ready_feed_depth'] ?? state.diagnostics['ready_feed_count'] ?? 0} phase=${state.preparationState}',
    );
  }

  void _scheduleQueueStatusMonitor(Map<String, dynamic> diagnostics) {
    final ready = (diagnostics['ready_feed_count'] as num?)?.toInt() ??
        (diagnostics['ready_feed_depth'] as num?)?.toInt() ??
        0;
    final target = (diagnostics['ready_feed_target_depth'] as num?)?.toInt() ??
        (diagnostics['target_ready_feed_depth'] as num?)?.toInt() ??
        2;
    final phase = diagnostics['queue_phase']?.toString() ?? '';
    final inflight = diagnostics['queue_build_inflight'] == true;
    final active = ready < target &&
        (inflight ||
            phase == 'scheduled' ||
            phase == 'building' ||
            phase == 'inventory_building' ||
            phase == 'prepared' ||
            phase == 'delayed' ||
            phase == 'retry' ||
            (diagnostics['retry_at'] as num?) != null);
    if (!active) {
      _queueStatusTimer?.cancel();
      _queueStatusTimer = null;
      _queueStatusDeadline = null;
      _queueStatusPollDelay = Duration.zero;
      return;
    }
    // Keep observing delayed/retry states until the backend reaches terminal
    // state; the active feed remains visible while this monitor is armed.
    _queueStatusDeadline = null;
    if (_queueStatusTimer != null) return;
    _queueStatusTimer = Timer(_queueStatusPollDelay, () {
      _queueStatusTimer = null;
      if (mounted) unawaited(_runQueueStatusRequest());
    });
  }

  Future<void> _runQueueStatusRequest() async {
    if (!mounted || _queueStatusRequestInFlight) return;
    _queueStatusRequestInFlight = true;
    try {
      // Queue status is a passive long-poll and must not occupy the general
      // feed-operation serialization tail used by refresh/promotion.
      await (() async {
        final scope = _cacheScopeId();
        try {
          final res = await postRecommendation({
            'query': '',
            'limit': 0,
            'offset': 0,
            'user_scope_id': scope,
            'feed_queue_status_only': true,
            'feed_queue_revision':
                (state.diagnostics['queue_revision'] as num?)?.toInt() ?? 0,
            'feed_queue_wait_ms': 9000,
          });
          if (!mounted || res.statusCode != 200) return;
          final payload = jsonDecode(res.body) as Map<String, dynamic>;
          final raw = payload['diagnostics'];
          if (raw is! Map) return;
          final diagnostics = Map<String, dynamic>.from(raw);
          final incomingRevision =
              (diagnostics['queue_revision'] as num?)?.toInt() ?? 0;
          final currentRevision =
              (state.diagnostics['queue_revision'] as num?)?.toInt() ?? 0;
          if (incomingRevision < currentRevision) return;
          final responseScope = diagnostics['user_scope_id']?.toString().trim();
          if (responseScope != null &&
              responseScope.isNotEmpty &&
              responseScope != _cacheScopeId()) {
            return;
          }
          final ready = (diagnostics['ready_feed_count'] as num?)?.toInt() ??
              (diagnostics['ready_feed_depth'] as num?)?.toInt() ??
              0;
          final target =
              (diagnostics['ready_feed_target_depth'] as num?)?.toInt() ?? 2;
          state = state.copyWith(
            preparationState: diagnostics['preparation_state']?.toString() ??
                state.preparationState,
            feedAction: 'queue_status',
            diagnostics: {...state.diagnostics, ...diagnostics},
            clearError: ready >= target,
          );
          // A successful bounded wait either observed a revision or reached
          // its timeout. Re-arm immediately so there is no client-side gap
          // between Feed A and Feed B becoming ready.
          _queueStatusPollDelay = Duration.zero;
          _scheduleQueueStatusMonitor({...state.diagnostics, ...diagnostics});
        } catch (_) {
          // Status is advisory; retain the active feed on transient failure.
          _queueStatusPollDelay = const Duration(seconds: 1);
          _scheduleQueueStatusMonitor({
            ...state.diagnostics,
            'queue_build_inflight': true,
            'preparation_state': state.preparationState,
          });
        }
      })();
    } finally {
      _queueStatusRequestInFlight = false;
    }
  }

  void _scheduleSignalDrivenBackgroundRefresh() {
    unawaited(() async {
      final seed = await HistoryManager.getRecommendationSeed();
      if (!mounted) return;
      unawaited(_scheduleBackgroundRecommendationsRefresh(seed));
    }());
  }

  Future<void> _scheduleBackgroundRecommendationsRefresh(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
    bool explicitRefresh = false,
  }) {
    final refreshFingerprint = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      extraArtistHints.join('|'),
      extraTasteQueries.join('|'),
      extraSessionQueries.join('|'),
    ].join('::');
    final lastCompletedAt = _lastCompletedBackgroundRefreshAt;
    final recentlyCompleted = !explicitRefresh &&
        refreshFingerprint == _lastCompletedBackgroundRefreshKey &&
        lastCompletedAt != null &&
        DateTime.now().difference(lastCompletedAt) <
            const Duration(seconds: 45);
    if (_backgroundRefreshKey.startsWith('$refreshFingerprint::') &&
        !explicitRefresh) {
      return Future<void>.value();
    }
    if (refreshFingerprint.trim().isEmpty || recentlyCompleted) {
      return Future<void>.value();
    }
    final refreshKey =
        '$refreshFingerprint::${explicitRefresh ? 'explicit:${DateTime.now().microsecondsSinceEpoch}' : 'automatic'}';
    _backgroundRefreshKey = refreshKey;
    _backgroundRefreshTimer?.cancel();
    _queueStatusTimer?.cancel();
    if (explicitRefresh) {
      return _runSerialized(
        () => _refreshRecommendationsInBackground(
          seedId,
          expectedKey: refreshKey,
          refreshFingerprint: refreshFingerprint,
          explicitRefresh: true,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          extraSessionQueries: extraSessionQueries,
        ),
      );
    }
    _backgroundRefreshTimer = Timer(const Duration(milliseconds: 750), () {
      unawaited(
        _runSerialized(
          () => _refreshRecommendationsInBackground(
            seedId,
            expectedKey: refreshKey,
            refreshFingerprint: refreshFingerprint,
            explicitRefresh: explicitRefresh,
            extraArtistHints: extraArtistHints,
            extraTasteQueries: extraTasteQueries,
            extraSessionQueries: extraSessionQueries,
          ),
        ),
      );
    });
    return Future<void>.value();
  }

  Future<void> _refreshRecommendationsInBackground(
    String? seedId, {
    required String expectedKey,
    required String refreshFingerprint,
    required bool explicitRefresh,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
  }) async {
    if (!mounted || _backgroundRefreshKey != expectedKey || isLoading) {
      return;
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        seedId,
        limit: 8,
        requestMode: explicitRefresh
            ? RecommendationRequestMode.pullToRefresh
            : RecommendationRequestMode.backgroundPrepare,
        avoidIds: explicitRefresh
            ? state.rows
                .expand((row) => row.items)
                .map(extractTrackId)
                .whereType<String>()
                .where((id) => id.isNotEmpty)
                .toSet()
            : const <String>{},
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: extraSessionQueries,
        allowNetworkCloudQueries: false,
        promoteReadyOnLaunch: false,
      );
      if (!mounted || _backgroundRefreshKey != expectedKey) {
        return;
      }
      debugProxyLog(
        'recommend',
        'background refresh start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''}',
      );
      final res = await postRecommendation(body);
      if (!mounted || _backgroundRefreshKey != expectedKey) {
        return;
      }
      if (res.statusCode != 200) {
        debugProxyLog(
          'recommend',
          'background refresh status=${res.statusCode} body=${res.body}',
        );
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final responseScope = body['user_scope_id']?.toString().trim() ?? '';
      if (responseScope.isEmpty || responseScope != _cacheScopeId()) {
        return;
      }
      _logRecommendationDiagnostics('background', payload);
      final diagnostics = payload['diagnostics'] is Map
          ? Map<String, dynamic>.from(payload['diagnostics'] as Map)
          : const <String, dynamic>{};
      debugProxyLog(
        'recommend',
        'background action=${payload['feed_action'] ?? diagnostics['feed_action'] ?? ''} phase=${diagnostics['queue_phase'] ?? diagnostics['preparation_state'] ?? ''} inflight=${diagnostics['queue_build_inflight'] ?? false} ready=${diagnostics['ready_feed_count'] ?? diagnostics['ready_feed_depth'] ?? 0}/${diagnostics['ready_feed_target_depth'] ?? diagnostics['target_ready_feed_depth'] ?? 2} reason=${diagnostics['queue_last_rejection_or_shortage'] ?? ''}',
      );
      final nextState = _feedStateFromPayload(payload);
      if (payload['diagnostics'] is Map) {
        _scheduleQueueStatusMonitor(
          Map<String, dynamic>.from(payload['diagnostics'] as Map),
        );
      }
      if (explicitRefresh &&
          nextState.hasRows &&
          nextState.feedVersion > state.feedVersion &&
          (nextState.feedAction == 'promoted_prepared' ||
              nextState.feedAction == 'built_and_promoted')) {
        state = nextState.copyWith(requestState: 'complete', clearError: true);
        unawaited(_storeCachedHomeFeed(state));
      }
    } on TimeoutException catch (error) {
      debugProxyLog('recommend', 'background refresh timeout=$error');
    } catch (error) {
      debugProxyLog('recommend', 'background refresh error=$error');
    } finally {
      if (_backgroundRefreshKey == expectedKey) {
        _backgroundRefreshKey = '';
        _lastCompletedBackgroundRefreshKey = refreshFingerprint;
        _lastCompletedBackgroundRefreshAt = DateTime.now();
      }
    }
  }

  @override
  void dispose() {
    _requestVersion++;
    _backgroundRefreshTimer?.cancel();
    _queueStatusTimer?.cancel();
    unawaited(_historyTrackSubscription?.cancel());
    unawaited(_recommendationSignalSubscription?.cancel());
    super.dispose();
  }

  RecommendationFeedState _feedStateFromPayload(Map<String, dynamic> payload) {
    final activePayload = filterActiveHomeFeedPayload(payload);
    return RecommendationFeedState.fromHomeJson(activePayload);
  }

  void _logRecommendationDiagnostics(
    String phase,
    Map<String, dynamic> payload,
  ) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return;
    final diagnostics = Map<String, dynamic>.from(diagnosticsRaw);
    final rowStatusSummary = <String, String>{};
    final rowStatusRaw = diagnostics['row_status'];
    if (rowStatusRaw is Map) {
      for (final entry in rowStatusRaw.entries) {
        final key = entry.key?.toString() ?? '';
        if (key.isEmpty) continue;
        final value = entry.value;
        if (value is Map) {
          rowStatusSummary[key] = value['status']?.toString() ?? '';
        }
      }
    }
    debugProxyLog(
      'recommend',
      '$phase timing requestMs=${diagnostics['request_ms']} profileMs=${diagnostics['profile_build_ms']} rowMs=${diagnostics['row_assembly_ms']} stageMs=${compactDiagnosticValue(diagnostics['stage_timings_ms'])} rowStatus=${compactDiagnosticValue(rowStatusSummary)} requestId=${payload['request_id'] ?? diagnostics['request_id'] ?? ''}',
    );
  }

  Future<void> loadQuickRecommendations(String seedId) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
    isLoading = true;
    if (_isRequestCurrent(requestVersion)) {
      state = state.copyWith(requestState: 'loading', clearError: true);
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        seedId,
        limit: 8,
        requestMode: RecommendationRequestMode.quick,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      debugProxyLog(
        'recommend',
        'quick request start scope=${body['user_scope_id']} seed=$seedId force=false',
      );
      final res = await postRecommendation(body);
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode != 200) {
        debugProxyLog(
          'recommend',
          'quick request status=${res.statusCode} body=${res.body}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage: proxyUnavailableMessage,
          );
        }
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _logRecommendationDiagnostics('quick', payload);
      final nextState = _feedStateFromPayload(payload);
      debugProxyLog(
        'recommend',
        'quick response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
      );
      if (!nextState.hasRows) {
        debugProxyLog(
          'recommend',
          'quick response parsed empty rows rawRows=${((payload['rows'] as List?) ?? const []).length}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage:
                'Recommendation engine returned no rows. Pull to refresh and try again.',
          );
        }
        return;
      }
      if (_isRequestCurrent(requestVersion)) {
        state = nextState.copyWith(
          requestState: 'complete',
          clearError: true,
        );
      }
    } on TimeoutException catch (error) {
      debugProxyLog('recommend', 'quick request timeout=$error');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: recommendTimeoutMessage,
        );
      }
    } catch (error) {
      debugProxyLog('recommend', 'quick request error=$error');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: proxyUnavailableMessage,
        );
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = !state.hasRows && state.requestState == 'loading';
        state = state.copyWith();
      }
    }
  }

  Future<void> loadRecommendations(
    String? seedId, {
    RecommendationRequestMode requestMode = RecommendationRequestMode.launch,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
    bool allowNetworkCloudQueries = true,
    String refreshToken = '',
    bool promoteReadyOnLaunch = false,
    int feedRefreshWaitMs = 0,
  }) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
    final forceRefresh = requestMode == RecommendationRequestMode.pullToRefresh;
    const preferFreshRows = false;
    final mergedArtistHints = _mergeSessionValues(
      extraArtistHints,
      _queuedSessionArtistHints,
      limit: 8,
    );
    final mergedSessionQueries = _mergeSessionValues(
      extraSessionQueries,
      _queuedSessionQueries,
      limit: 6,
    );
    final usedQueuedSessionIntent = _hasQueuedSessionIntent;
    final preserveVisibleRows = state.hasRows &&
        (forceRefresh ||
            requestMode == RecommendationRequestMode.launch ||
            requestMode == RecommendationRequestMode.liveRowRefresh);
    isLoading = !preserveVisibleRows;
    if (_isRequestCurrent(requestVersion)) {
      state = preserveVisibleRows
          ? state.copyWith(clearError: true)
          : state.copyWith(requestState: 'loading', clearError: true);
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        seedId,
        limit: 8,
        requestMode: requestMode,
        preferFreshRows: preferFreshRows,
        refreshToken: refreshToken,
        avoidIds: forceRefresh
            ? state.rows
                .expand((row) => row.items)
                .map(extractTrackId)
                .whereType<String>()
                .where((id) => id.isNotEmpty)
                .toSet()
            : const <String>{},
        extraArtistHints: mergedArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: mergedSessionQueries,
        allowNetworkCloudQueries: allowNetworkCloudQueries,
        promoteReadyOnLaunch: promoteReadyOnLaunch,
        launchToken: promoteReadyOnLaunch ? _launchToken : '',
        feedRefreshWaitMs: feedRefreshWaitMs,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      debugProxyLog(
        'recommend',
        'request start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''} mode=$requestMode force=$forceRefresh preferFresh=$preferFreshRows artistHints=${compactDiagnosticValue(body['artist_hints'])} tasteQueries=${compactDiagnosticValue(body['taste_queries'])}',
      );
      final res = await postRecommendation(body);
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final rawPayload = jsonDecode(res.body) as Map<String, dynamic>;
        final responseScope = (rawPayload['user_scope_id'] ??
                (rawPayload['diagnostics'] is Map
                    ? (rawPayload['diagnostics'] as Map)['user_scope_id']
                    : null))
            ?.toString()
            .trim();
        if (responseScope != null &&
            responseScope.isNotEmpty &&
            responseScope != _cacheScopeId()) {
          debugProxyLog('recommend', 'ignored response scope=$responseScope');
          return;
        }
        // A valid scoped response acknowledges the queued weak search signal.
        // Promotion/version changes are independent and are not used as proof
        // that the intent was consumed.
        if (usedQueuedSessionIntent) {
          _clearQueuedSessionIntent();
        }
        final payload = filterActiveHomeFeedPayload(rawPayload);
        _logRecommendationDiagnostics('main', payload);
        if (payload['diagnostics'] is Map) {
          _scheduleQueueStatusMonitor(
            Map<String, dynamic>.from(payload['diagnostics'] as Map),
          );
        }
        final nextState = _feedStateFromPayload(payload);
        debugProxyLog(
          'recommend',
          'response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
        if (nextState.hasRows) {
          final refreshTimedOut =
              forceRefresh && nextState.feedAction == 'unchanged_no_rotation';
          final responseDiagnostics = <String, dynamic>{
            ...state.diagnostics,
            ...nextState.diagnostics,
            if (refreshTimedOut) 'refresh_wait_timed_out': true,
            if (forceRefresh) 'refresh_wait_ms': feedRefreshWaitMs,
          };
          if (state.hasRows && nextState.feedVersion <= state.feedVersion) {
            if (nextState.feedVersion == state.feedVersion && forceRefresh) {
              state = state.copyWith(
                requestState: 'complete',
                feedAction: nextState.feedAction,
                preparationState: nextState.preparationState,
                diagnostics: responseDiagnostics,
                errorMessage: refreshTimedOut
                    ? 'Fresh recommendations are still being prepared.'
                    : null,
                clearError: !refreshTimedOut,
              );
            }
            return;
          }
          state = nextState.copyWith(
            requestState: 'complete',
            errorMessage: refreshTimedOut
                ? 'Fresh recommendations are still being prepared.'
                : null,
            diagnostics: responseDiagnostics,
            clearError: !refreshTimedOut,
          );
          unawaited(_storeCachedHomeFeed(state));
          return;
        }
        debugProxyLog(
          'recommend',
          'response parsed empty rows rawRows=${((payload['rows'] as List?) ?? const []).length}',
        );
        if (_isRequestCurrent(requestVersion)) {
          final payloadDiagnostics = payload['diagnostics'];
          final diagnostics = payloadDiagnostics is Map
              ? Map<String, dynamic>.from(payloadDiagnostics)
              : const <String, dynamic>{};
          final backendPreparing =
              nextState.feedAction == 'preparing_initial' ||
                  nextState.feedAction == 'unchanged_no_rotation' ||
                  nextState.preparationState == 'preparing' ||
                  diagnostics['preparation_state'] == 'preparing' ||
                  diagnostics['ready_feed_count'] == 0 ||
                  diagnostics['ready_feed_depth'] == 0;
          if (backendPreparing && previousState.hasRows) {
            state = previousState.copyWith(
              requestState: 'complete',
              preparationState: 'preparing',
              clearError: true,
              diagnostics: {
                ...previousState.diagnostics,
                ...diagnostics,
                'client_preserved_active_during_preparation': true,
              },
            );
            // Keep a valid active feed visible while the backend prepares the
            // next ready head. Do not poll indefinitely or synthesize rows.
          } else if (diagnostics['fresh_account_empty_home'] == true ||
              diagnostics['client_signal_tier'] == 'cold_start') {
            state = RecommendationFeedState(
              requestState: 'complete',
              rows: const [],
              errorMessage: freshAccountHomeMessage,
              diagnostics: diagnostics,
            );
          } else {
            state = nextState.copyWith(
              requestState: 'failed',
              errorMessage:
                  'Recommendation engine returned no rows. Pull to refresh and try again.',
            );
          }
        }
      } else {
        debugProxyLog(
          'recommend',
          'request status=${res.statusCode} body=${res.body}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage: proxyUnavailableMessage,
          );
        }
      }
    } on TimeoutException catch (e) {
      debugProxyLog('recommend', 'request timeout=$e');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: recommendTimeoutMessage,
        );
      }
    } catch (e) {
      debugProxyLog('recommend', 'request error=$e');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: proxyUnavailableMessage,
        );
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = state.copyWith();
      }
    }
  }

  Future<void> loadMoreRow(String rowId) async {
    if (!isActiveHomeFeedRowKind(rowId)) return;
    RecommendationFeedRowState? targetRow;
    for (final row in state.rows) {
      if (row.id == rowId) {
        targetRow = row;
        break;
      }
    }
    final reserveCount =
        (targetRow?.meta['reserve_count'] as num?)?.toInt() ?? 0;
    const quietPicksMaximum = 48;
    final reachedQuietPicksMaximum = targetRow?.kind == 'quiet_picks' &&
        (targetRow?.items.length ?? 0) >= quietPicksMaximum;
    final canPage = targetRow?.hasMore == true || reserveCount > 0;
    if (targetRow == null ||
        !canPage ||
        reachedQuietPicksMaximum ||
        state.sessionId.isEmpty ||
        _paginatingRows.contains(rowId)) {
      debugProxyLog(
        'recommend',
        'row page skipped row=$rowId found=${targetRow != null} hasMore=${targetRow?.hasMore} reserveCount=$reserveCount session=${state.sessionId.isNotEmpty} paginating=${_paginatingRows.contains(rowId)}',
      );
      return;
    }
    final currentRow = targetRow;
    _paginatingRows.add(rowId);
    if (mounted) {
      state = state.copyWith();
    }
    try {
      final pageLimit = targetRow.kind == 'quiet_picks'
          ? math.min(
              quietPicksMaximum - currentRow.items.length,
              40,
            )
          : 8;
      debugProxyLog(
        'recommend',
        'row page start row=$rowId kind=${targetRow.kind} offset=${targetRow.nextOffset} session=${state.sessionId} hasMore=${targetRow.hasMore} preparedCount=${targetRow.meta['prepared_count']} reserveCount=$reserveCount',
      );
      final body = await buildRecommendationRequestBody(
        ref,
        null,
        limit: pageLimit,
        offset: targetRow.nextOffset,
        requestMode: RecommendationRequestMode.rowPage,
        allowNetworkCloudQueries: false,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = rowId;
      final res = await postRecommendation(
        body,
        timeout: recommendRowPageTimeout,
      );
      if (!mounted) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final rowPayload = payload['row'];
        final newRow =
            rowPayload is Map ? activeHomeFeedRowFromJson(rowPayload) : null;
        if (newRow != null) {
          final mergedItems = <Map<String, dynamic>>[];
          final seen = <String>{};
          for (final track in [...currentRow.items, ...newRow.items]) {
            final key = recommendationRowItemKey(currentRow.itemType, track);
            if (key.trim().isEmpty || !seen.add(key)) continue;
            mergedItems.add(track);
            if (currentRow.kind == 'quiet_picks' &&
                mergedItems.length >= quietPicksMaximum) {
              break;
            }
          }
          final reachedMaximum = currentRow.kind == 'quiet_picks' &&
              mergedItems.length >= quietPicksMaximum;
          final progressed = mergedItems.length > currentRow.items.length ||
              newRow.nextOffset > currentRow.nextOffset;
          final updatedRows = state.rows
              .map(
                (row) => row.id == rowId
                    ? row.copyWith(
                        itemType: newRow.itemType,
                        rowStyle: newRow.rowStyle,
                        meta: newRow.meta,
                        items: mergedItems,
                        nextOffset: progressed
                            ? newRow.nextOffset
                            : currentRow.nextOffset,
                        hasMore: reachedMaximum
                            ? false
                            : (progressed ? newRow.hasMore : false),
                      )
                    : row,
              )
              .toList(growable: false);
          state = state.copyWith(rows: updatedRows);
          debugProxyLog(
            'recommend',
            'row page success row=$rowId appended=${mergedItems.length - currentRow.items.length} nextOffset=${progressed ? newRow.nextOffset : currentRow.nextOffset} hasMore=${reachedMaximum ? false : (progressed ? newRow.hasMore : false)}',
          );
        }
      } else {
        debugProxyLog(
          'recommend',
          'row page status=${res.statusCode} row=$rowId session=${state.sessionId} body=${res.body}',
        );
        if (res.statusCode == 404) {
          final updatedRows = state.rows
              .map(
                (row) => row.id == rowId ? row.copyWith(hasMore: false) : row,
              )
              .toList(growable: false);
          state = state.copyWith(rows: updatedRows, clearError: true);
        }
      }
    } on TimeoutException catch (error) {
      debugProxyLog('recommend', 'row page timeout row=$rowId error=$error');
    } catch (e) {
      debugProxyLog('recommend', 'row page error row=$rowId error=$e');
    } finally {
      _paginatingRows.remove(rowId);
      if (mounted) {
        state = state.copyWith();
      }
    }
  }

  Future<void> selectRowContext(
    String rowId, {
    required String rowContext,
  }) async {
    if (!isActiveHomeFeedRowKind(rowId)) return;
    RecommendationFeedRowState? targetRow;
    for (final row in state.rows) {
      if (row.id == rowId) {
        targetRow = row;
        break;
      }
    }
    if (targetRow == null ||
        state.sessionId.isEmpty ||
        _paginatingRows.contains(rowId) ||
        rowContext.trim().isEmpty) {
      return;
    }
    _paginatingRows.add(rowId);
    if (mounted) {
      state = state.copyWith();
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        null,
        limit: math.max(targetRow.items.length, 6),
        offset: 0,
        requestMode: RecommendationRequestMode.rowContext,
        allowNetworkCloudQueries: false,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = rowId;
      body['row_context'] = rowContext.trim();
      final res = await postRecommendation(
        body,
        timeout: recommendRowPageTimeout,
      );
      if (!mounted || res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rowPayload = payload['row'];
      final nextRow =
          rowPayload is Map ? activeHomeFeedRowFromJson(rowPayload) : null;
      if (nextRow == null) return;
      final updatedRows = state.rows
          .map((row) => row.id == rowId ? nextRow : row)
          .toList(growable: false);
      state = state.copyWith(rows: updatedRows);
    } catch (_) {
      // Keep the existing row visible when lightweight row-context updates fail.
    } finally {
      _paginatingRows.remove(rowId);
      if (mounted) {
        state = state.copyWith();
      }
    }
  }

  Future<void> loadMore([String? _]) async {
    final nextRow = state.rows.firstWhere(
      (row) => row.hasMore && !_paginatingRows.contains(row.id),
      orElse: () => const RecommendationFeedRowState(
        id: '',
        title: '',
        kind: '',
        itemType: 'track',
        rowStyle: '',
        meta: <String, dynamic>{},
        items: [],
        nextOffset: 0,
        hasMore: false,
      ),
    );
    if (nextRow.id.isEmpty) {
      return;
    }
    await loadMoreRow(nextRow.id);
  }
}

final recommendationProvider =
    StateNotifierProvider<RecommendationNotifier, RecommendationFeedState>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  final notifier = RecommendationNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});
