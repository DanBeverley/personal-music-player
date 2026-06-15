import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart' show audioPlayerProvider;
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
  final Set<String> _prewarmedRecommendationIds = <String>{};
  bool _startupHealthChecked = false;
  Timer? _backgroundRefreshTimer;
  String _backgroundRefreshKey = '';
  String _lastCompletedBackgroundRefreshKey = '';
  DateTime? _lastCompletedBackgroundRefreshAt;
  RecommendationFeedState? _preparedFeedState;
  String _preparedFeedScopeId = '';
  bool _applyNextPreparedFeedImmediately = false;
  final List<String> _queuedSessionArtistHints = <String>[];
  final List<String> _queuedSessionQueries = <String>[];
  StreamSubscription<Map<String, dynamic>>? _historyTrackSubscription;
  StreamSubscription<String>? _recommendationSignalSubscription;

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

  Future<void> applyQueuedSessionIntent() async {
    if (!_hasQueuedSessionIntent) return;
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    await loadRecommendations(
      seed,
      requestMode: RecommendationRequestMode.launch,
    );
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
      _primeRecommendationRows(state.rows);
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
      _scheduleSignalDrivenBackgroundRefresh();
      return;
    }

    await refreshFromSignals(forceRefresh: false);
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

  Future<void> refreshFromSignals({bool forceRefresh = false}) async {
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    if (forceRefresh) {
      _applyNextPreparedFeedImmediately = true;
      state = state.copyWith(
        requestState: state.hasRows ? 'complete' : state.requestState,
        clearError: true,
        diagnostics: <String, dynamic>{
          ...state.diagnostics,
          'refresh_scheduled': true,
          'refresh_scheduled_at': DateTime.now().toIso8601String(),
        },
      );
      await _scheduleBackgroundRecommendationsRefresh(
        seed,
        explicitRefresh: true,
      );
      return;
    }
    await loadRecommendations(
      seed,
      requestMode: RecommendationRequestMode.launch,
      allowNetworkCloudQueries: false,
    );
  }

  Future<void> applyPreparedFeedOnHomeReturn() async {
    final prepared = _preparedFeedState;
    if (prepared == null || isLoading) return;
    if (_preparedFeedScopeId != _cacheScopeId()) {
      _preparedFeedState = null;
      _preparedFeedScopeId = '';
      return;
    }
    _preparedFeedState = null;
    _preparedFeedScopeId = '';
    _applyNextPreparedFeedImmediately = false;
    state = prepared.copyWith(
      requestState: 'complete',
      clearError: true,
    );
    unawaited(_storeCachedHomeFeed(state));
    _primeRecommendationRows(state.rows);
  }

  void _scheduleSignalDrivenBackgroundRefresh() {
    unawaited(() async {
      final seed = await HistoryManager.getRecommendationSeed();
      if (!mounted) return;
      unawaited(_scheduleBackgroundRecommendationsRefresh(seed));
    }());
  }

  bool _shouldPrepareNextSession(Map<String, dynamic> payload) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return false;
    final diagnostics = Map<String, dynamic>.from(diagnosticsRaw);
    final requestMode = diagnostics['request_mode']?.toString().trim() ?? '';
    final artifactSource =
        diagnostics['artifact_source']?.toString().trim() ?? '';
    final rankingBackend =
        diagnostics['ranking_backend']?.toString().trim() ?? '';
    final promotionStatus =
        diagnostics['promotion_status']?.toString().trim().toLowerCase() ?? '';
    final cacheHit = diagnostics['cache_hit'] == true;
    final heavyPending = diagnostics['heavy_rows_pending'] == true;
    final heavyPartial = diagnostics['heavy_rows_partial'] == true;
    final deferredRowsPending = diagnostics['deferred_rows_pending'] == true;
    final launchTierOnly = diagnostics['launch_tier_only'] == true;
    return cacheHit ||
        requestMode == 'launch_artifact' ||
        requestMode == 'request_build' ||
        artifactSource == 'launch_artifact' ||
        artifactSource == 'request_build' ||
        rankingBackend == 'artifact_launch' ||
        promotionStatus != 'promoted' ||
        deferredRowsPending ||
        launchTierOnly ||
        heavyPending ||
        heavyPartial;
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
    if (explicitRefresh) {
      return _refreshRecommendationsInBackground(
        seedId,
        expectedKey: refreshKey,
        refreshFingerprint: refreshFingerprint,
        explicitRefresh: true,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: extraSessionQueries,
      );
    }
    _backgroundRefreshTimer = Timer(const Duration(milliseconds: 750), () {
      unawaited(
        _refreshRecommendationsInBackground(
          seedId,
          expectedKey: refreshKey,
          refreshFingerprint: refreshFingerprint,
          explicitRefresh: explicitRefresh,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          extraSessionQueries: extraSessionQueries,
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
      debugProxyLog(
        'recommend',
        'background prepare scheduled=${payload['prepared'] == true} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
      );
      final prepared = _feedStateFromPayload(payload);
      final diagnostics = payload['diagnostics'] is Map
          ? Map<String, dynamic>.from(payload['diagnostics'] as Map)
          : const <String, dynamic>{};
      final artifactQuality =
          diagnostics['artifact_quality']?.toString().trim().toLowerCase() ??
              '';
      final refreshOutcome =
          diagnostics['refresh_outcome']?.toString().trim().toLowerCase() ?? '';
      final refreshChanged = diagnostics['refresh_changed'] == true;
      if (prepared.hasRows &&
          artifactQuality != 'rejected' &&
          artifactQuality != 'kept_previous' &&
          refreshOutcome != 'kept_previous' &&
          refreshOutcome != 'unchanged' &&
          refreshOutcome != 'suppressed_same_fingerprint' &&
          (!explicitRefresh || refreshChanged) &&
          home_feed_cache.shouldCacheHomeFeed(prepared)) {
        final stagedFeed = prepared.copyWith(
          requestState: 'complete',
          clearError: true,
        );
        _preparedFeedState = stagedFeed;
        _preparedFeedScopeId = responseScope;
        unawaited(_storeCachedHomeFeed(stagedFeed));
        if (_applyNextPreparedFeedImmediately) {
          _applyNextPreparedFeedImmediately = false;
          _preparedFeedState = null;
          _preparedFeedScopeId = '';
          state = stagedFeed;
          _primeRecommendationRows(stagedFeed.rows);
        }
      } else if (explicitRefresh && mounted) {
        state = state.copyWith(
          requestState: 'complete',
          clearError: true,
          diagnostics: <String, dynamic>{
            ...state.diagnostics,
            'refresh_outcome': refreshOutcome.isEmpty
                ? artifactQuality
                : refreshOutcome,
            'refresh_changed': false,
            'refresh_completed_at': DateTime.now().toIso8601String(),
          },
        );
      }
    } on TimeoutException catch (error) {
      debugProxyLog('recommend', 'background refresh timeout=$error');
    } catch (error) {
      debugProxyLog('recommend', 'background refresh error=$error');
    } finally {
      if (_backgroundRefreshKey == expectedKey) {
        _backgroundRefreshKey = '';
        _applyNextPreparedFeedImmediately = false;
        _lastCompletedBackgroundRefreshKey = refreshFingerprint;
        _lastCompletedBackgroundRefreshAt = DateTime.now();
      }
    }
  }

  @override
  void dispose() {
    _requestVersion++;
    _backgroundRefreshTimer?.cancel();
    unawaited(_historyTrackSubscription?.cancel());
    unawaited(_recommendationSignalSubscription?.cancel());
    super.dispose();
  }

  RecommendationFeedState _feedStateFromPayload(Map<String, dynamic> payload) {
    final activePayload = filterActiveHomeFeedPayload(payload);
    final rows = (activePayload['rows'] as List<dynamic>? ?? const []);
    if (rows.isNotEmpty) {
      final nextState = RecommendationFeedState.fromHomeJson(activePayload);
      if (nextState.hasRows) {
        return nextState;
      }
    }
    final rawRecommendations = activePayload['recommendations'] is List
        ? activePayload['recommendations'] as List<dynamic>
        : payload['recommendations'] is List
            ? payload['recommendations'] as List<dynamic>
            : const <dynamic>[];
    final recommendations = rawRecommendations
            .whereType<Map>()
            .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
            .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
            .toList(growable: false);
    if (recommendations.isNotEmpty) {
      final rebuiltPayload = <String, dynamic>{
        ...activePayload,
        'rows': <Map<String, dynamic>>[
          {
            'id': 'made_for_you',
            'title': 'Made for you',
            'kind': 'made_for_you',
            'item_type': 'track',
            'items': recommendations,
            'next_offset': recommendations.length,
            'has_more': false,
          }
        ],
      };
      final nextState = RecommendationFeedState.fromHomeJson(rebuiltPayload);
      if (nextState.hasRows) {
        return nextState;
      }
    }
    return const RecommendationFeedState(requestState: 'complete');
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

  void _primeRecommendationResults(
    Iterable<dynamic> tracks, {
    int maxIds = 10,
    int lookahead = 18,
  }) {
    final ids = <String>[];
    for (final track in tracks) {
      final id = extractTrackId(track);
      if (id == null || id.isEmpty || !_prewarmedRecommendationIds.add(id)) {
        continue;
      }
      ids.add(id);
      if (ids.length >= maxIds) {
        break;
      }
    }
    if (ids.isEmpty) return;
    if (_prewarmedRecommendationIds.length > 160) {
      _prewarmedRecommendationIds.removeAll(
        _prewarmedRecommendationIds
            .take(_prewarmedRecommendationIds.length - 80),
      );
    }
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            ids,
            lookahead: lookahead,
            immediatePlayback: true,
            currentVideoId: ids.first,
          ),
    );
  }

  void _primeRecommendationRows(List<RecommendationFeedRowState> rows) {
    final visibleTracks = <Map<String, dynamic>>[];
    for (final row in rows) {
      if (row.itemType != 'track') continue;
      visibleTracks.addAll(row.items.take(2));
      if (visibleTracks.length >= 8) {
        break;
      }
    }
    if (visibleTracks.isEmpty) return;
    _primeRecommendationResults(
      visibleTracks,
      maxIds: 3,
      lookahead: 3,
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
        _primeRecommendationRows(nextState.rows);
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
        isLoading = false;
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
        extraArtistHints: mergedArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: mergedSessionQueries,
        allowNetworkCloudQueries: allowNetworkCloudQueries,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      debugProxyLog(
        'recommend',
        'request start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''} mode=$requestMode force=$forceRefresh preferFresh=$preferFreshRows artistHints=${compactDiagnosticValue(body['artist_hints'])} tasteQueries=${compactDiagnosticValue(body['taste_queries'])}',
      );
      final res = await postRecommendation(body);
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = filterActiveHomeFeedPayload(
          jsonDecode(res.body) as Map<String, dynamic>,
        );
        _logRecommendationDiagnostics('main', payload);
        final nextState = _feedStateFromPayload(payload);
        debugProxyLog(
          'recommend',
          'response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
        if (nextState.hasRows) {
          state = nextState.copyWith(
            requestState: 'complete',
            clearError: true,
          );
          if (usedQueuedSessionIntent) {
            _clearQueuedSessionIntent();
          }
          unawaited(_storeCachedHomeFeed(state));
          _primeRecommendationRows(state.rows);
          if (requestMode == RecommendationRequestMode.launch &&
              _shouldPrepareNextSession(payload)) {
            unawaited(
              _scheduleBackgroundRecommendationsRefresh(
                seedId,
                extraArtistHints: mergedArtistHints,
                extraTasteQueries: extraTasteQueries,
                extraSessionQueries: mergedSessionQueries,
              ),
            );
          }
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
          if (diagnostics['fresh_account_empty_home'] == true ||
              diagnostics['client_signal_tier'] == 'cold_start') {
            state = RecommendationFeedState(
              requestState: 'complete',
              rows: const [],
              errorMessage: freshAccountHomeMessage,
              diagnostics: diagnostics,
            );
          } else {
            state = previousState.copyWith(
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
    final canPage = targetRow?.hasMore == true || reserveCount > 0;
    if (targetRow == null ||
        !canPage ||
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
      final pageLimit = targetRow.kind == 'quiet_picks' ? 40 : 8;
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
        final newRow = rowPayload is Map
            ? activeHomeFeedRowFromJson(rowPayload)
            : null;
        if (newRow != null) {
          final mergedItems = <Map<String, dynamic>>[];
          final seen = <String>{};
          for (final track in [...currentRow.items, ...newRow.items]) {
            final key = recommendationRowItemKey(currentRow.itemType, track);
            if (key.trim().isEmpty || !seen.add(key)) continue;
            mergedItems.add(track);
          }
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
                        hasMore: progressed ? newRow.hasMore : false,
                      )
                    : row,
              )
              .toList(growable: false);
          state = state.copyWith(rows: updatedRows);
          debugProxyLog(
            'recommend',
            'row page success row=$rowId appended=${mergedItems.length - currentRow.items.length} nextOffset=${progressed ? newRow.nextOffset : currentRow.nextOffset} hasMore=${progressed ? newRow.hasMore : false}',
          );
          _primeRecommendationResults(
            newRow.items.take(targetRow.kind == 'quiet_picks' ? 5 : 3),
            maxIds: targetRow.kind == 'quiet_picks' ? 5 : 2,
            lookahead: targetRow.kind == 'quiet_picks' ? 8 : 4,
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
      final nextRow = rowPayload is Map
          ? activeHomeFeedRowFromJson(rowPayload)
          : null;
      if (nextRow == null) return;
      final updatedRows = state.rows
          .map((row) => row.id == rowId ? nextRow : row)
          .toList(growable: false);
      state = state.copyWith(rows: updatedRows);
      _primeRecommendationResults(nextRow.items);
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
