import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart'
    show
        HistoryManager,
        audioPlayerProvider,
        compactProxyDiagnosticValue,
        logProxyDiagnostic,
        recommendationSignalStream;
import 'audio_provider_request_builder.dart';
import 'auth_provider.dart' show authProvider;
import 'proxy_runtime.dart';
import 'recommendation_feed_models.dart';
import 'track_metadata.dart';

class RecommendationNotifier extends StateNotifier<RecommendationFeedState> {
  final Ref ref;
  RecommendationNotifier(this.ref) : super(const RecommendationFeedState()) {
    _historyTrackSubscription = HistoryManager.trackStream.listen((_) {
      _queueContinueListeningRefresh();
    });
    _recommendationSignalSubscription = recommendationSignalStream.listen((_) {
      _queueContinueListeningRefresh();
    });
  }
  bool isLoading = true;
  final Set<String> _paginatingRows = <String>{};
  int _requestVersion = 0;
  final Set<String> _prewarmedRecommendationIds = <String>{};
  bool _startupHealthChecked = false;
  Timer? _heavyHydrationTimer;
  Timer? _continueListeningRefreshTimer;
  Timer? _backgroundRefreshTimer;
  String _heavyHydrationKey = '';
  String _backgroundRefreshKey = '';
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
      false,
      const <String>[],
      const <String>[],
      const <String>[],
      state.hasRows,
    );
  }

  Future<bool> _ensureProxyHealthyAtStartup() async {
    if (_startupHealthChecked) return true;
    _startupHealthChecked = true;
    final healthy = await probeProxyHealth();
    if (!healthy) {
      logProxyDiagnostic(
        'recommend',
        'startup health check failed for candidates=${compactProxyDiagnosticValue(proxyBaseUrlCandidates)}',
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
      logProxyDiagnostic(
        'recommend',
        'bootstrap deferred until auth initialization completes for scope=${authState.storageScopeId}',
      );
      return;
    }
    final proxyHealthy = await _ensureProxyHealthyAtStartup();
    if (!mounted) return;
    if (!proxyHealthy) {
      isLoading = false;
      state = state.copyWith(
        requestState: 'failed',
        errorMessage:
            'Recommendation engine is unreachable. Check proxy/server connection and refresh.',
      );
      return;
    }

    await refreshFromSignals(forceRefresh: false);
  }

  Future<void> refreshFromSignals({bool forceRefresh = false}) async {
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    if (forceRefresh) {
      await loadRecommendations(
        seed,
        false,
        const <String>[],
        const <String>[],
        const <String>[],
        true,
      );
      return;
    }
    await loadRecommendations(seed, forceRefresh);
  }

  bool _shouldPrepareNextSession(Map<String, dynamic> payload) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return false;
    final diagnostics = Map<String, dynamic>.from(diagnosticsRaw);
    final requestMode = diagnostics['request_mode']?.toString().trim() ?? '';
    final artifactSource = diagnostics['artifact_source']?.toString().trim() ?? '';
    final rankingBackend = diagnostics['ranking_backend']?.toString().trim() ?? '';
    final promotionStatus =
        diagnostics['promotion_status']?.toString().trim().toLowerCase() ?? '';
    final cacheHit = diagnostics['cache_hit'] == true;
    final heavyPending = diagnostics['heavy_rows_pending'] == true;
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
        heavyPending;
  }

  void _scheduleBackgroundRecommendationsRefresh(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
  }) {
    final refreshKey = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      extraArtistHints.join('|'),
      extraTasteQueries.join('|'),
      extraSessionQueries.join('|'),
    ].join('::');
    if (refreshKey.trim().isEmpty || _backgroundRefreshKey == refreshKey) {
      return;
    }
    _backgroundRefreshKey = refreshKey;
    _backgroundRefreshTimer?.cancel();
    _backgroundRefreshTimer = Timer(const Duration(milliseconds: 750), () {
      unawaited(
        _refreshRecommendationsInBackground(
          seedId,
          expectedKey: refreshKey,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          extraSessionQueries: extraSessionQueries,
        ),
      );
    });
  }

  Future<void> _refreshRecommendationsInBackground(
    String? seedId, {
    required String expectedKey,
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
        forceRefresh: false,
        prepareNextSession: true,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: extraSessionQueries,
      );
      if (!mounted || _backgroundRefreshKey != expectedKey) {
        return;
      }
      logProxyDiagnostic(
        'recommend',
        'background refresh start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''}',
      );
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        recommendRequestTimeout,
      );
      if (!mounted || _backgroundRefreshKey != expectedKey) {
        return;
      }
      if (res.statusCode != 200) {
        logProxyDiagnostic(
          'recommend',
          'background refresh status=${res.statusCode} body=${res.body}',
        );
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _logRecommendationDiagnostics('background', payload);
      logProxyDiagnostic(
        'recommend',
        'background prepare scheduled=${payload['prepared'] == true} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
      );
    } on TimeoutException catch (error) {
      logProxyDiagnostic('recommend', 'background refresh timeout=$error');
    } catch (error) {
      logProxyDiagnostic('recommend', 'background refresh error=$error');
    } finally {
      if (_backgroundRefreshKey == expectedKey) {
        _backgroundRefreshKey = '';
      }
    }
  }

  @override
  void dispose() {
    _requestVersion++;
    _heavyHydrationTimer?.cancel();
    _continueListeningRefreshTimer?.cancel();
    _backgroundRefreshTimer?.cancel();
    unawaited(_historyTrackSubscription?.cancel());
    unawaited(_recommendationSignalSubscription?.cancel());
    super.dispose();
  }

  RecommendationFeedState _feedStateFromPayload(Map<String, dynamic> payload) {
    final rows = (payload['rows'] as List<dynamic>? ?? const []);
    if (rows.isNotEmpty) {
      final nextState = RecommendationFeedState.fromJson(payload);
      if (nextState.hasRows) {
        return nextState;
      }
    }
    final recommendations =
        (payload['recommendations'] as List<dynamic>? ?? const [])
            .whereType<Map>()
            .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
            .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
            .toList(growable: false);
    if (recommendations.isNotEmpty) {
      final rebuiltPayload = <String, dynamic>{
        ...payload,
        'rows': <Map<String, dynamic>>[
          {
            'id': 'recommended_tracks',
            'title': 'Recommended for you',
            'kind': 'recommended_tracks',
            'item_type': 'track',
            'items': recommendations,
            'next_offset': recommendations.length,
            'has_more': false,
          }
        ],
      };
      final nextState = RecommendationFeedState.fromJson(rebuiltPayload);
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
    logProxyDiagnostic(
      'recommend',
      '$phase timing requestMs=${diagnostics['request_ms']} profileMs=${diagnostics['profile_build_ms']} rowMs=${diagnostics['row_assembly_ms']} stageMs=${compactProxyDiagnosticValue(diagnostics['stage_timings_ms'])} rowStatus=${compactProxyDiagnosticValue(rowStatusSummary)} requestId=${payload['request_id'] ?? diagnostics['request_id'] ?? ''}',
    );
  }

  void _primeRecommendationResults(
    Iterable<dynamic> tracks, {
    int maxIds = 6,
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
          ),
    );
  }

  void _primeRecommendationRows(List<RecommendationFeedRowState> rows) {
    final visibleTracks = <Map<String, dynamic>>[];
    for (final row in rows) {
      if (row.itemType != 'track') continue;
      visibleTracks.addAll(row.items.take(3));
      if (visibleTracks.length >= 12) {
        break;
      }
    }
    if (visibleTracks.isEmpty) return;
    _primeRecommendationResults(
      visibleTracks,
      maxIds: 4,
      lookahead: 8,
    );
  }

  bool _diagnosticFlag(
    Map<String, dynamic> payload,
    String key,
  ) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return false;
    final value = diagnosticsRaw[key];
    return value == true || value?.toString().toLowerCase() == 'true';
  }

  RecommendationFeedRowState? _rowByKind(String rowKind) {
    for (final row in state.rows) {
      if (row.kind == rowKind) {
        return row;
      }
    }
    return null;
  }

  Set<String> _deferredRowKindsFromDiagnostics(
    Map<String, dynamic> diagnostics,
  ) {
    final kinds = <String>{};
    final raw = diagnostics['deferred_row_kinds'];
    if (raw is List) {
      for (final value in raw) {
        final kind = value?.toString().trim() ?? '';
        if (kind.isNotEmpty) {
          kinds.add(kind);
        }
      }
    }
    return kinds;
  }

  void _queueContinueListeningRefresh() {
    _continueListeningRefreshTimer?.cancel();
    if (!mounted || isLoading || state.sessionId.isEmpty) return;
    final targetRow = _rowByKind('continue_listening');
    if (targetRow == null || _paginatingRows.contains(targetRow.id)) return;
    _continueListeningRefreshTimer = Timer(
      const Duration(milliseconds: 450),
      () => unawaited(_refreshContinueListeningRow()),
    );
  }

  Future<void> _refreshContinueListeningRow() async {
    final targetRow = _rowByKind('continue_listening');
    if (targetRow == null ||
        state.sessionId.isEmpty ||
        isLoading ||
        _paginatingRows.contains(targetRow.id)) {
      return;
    }
    _paginatingRows.add(targetRow.id);
    if (mounted) {
      state = state.copyWith();
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        null,
        limit: max(targetRow.items.length, 6),
        offset: 0,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = targetRow.id;
      body['row_context'] = 'live_refresh';
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        recommendRowPageTimeout,
      );
      if (!mounted || res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rowPayload = payload['row'];
      if (rowPayload is! Map<String, dynamic>) return;
      final nextRow = RecommendationFeedRowState.fromJson(rowPayload);
      final diagnostics = payload['diagnostics'] is Map
          ? {
              ...state.diagnostics,
              ...Map<String, dynamic>.from(payload['diagnostics'] as Map),
            }
          : state.diagnostics;
      final updatedRows = state.rows
          .map((row) => row.id == targetRow.id ? nextRow : row)
          .toList(growable: false);
      state = state.copyWith(
        rows: updatedRows,
        diagnostics: diagnostics,
        clearError: true,
      );
      _primeRecommendationResults(nextRow.items);
    } catch (_) {
      // Keep the current feed stable if the live row refresh fails.
    } finally {
      _paginatingRows.remove(targetRow.id);
      if (mounted) {
        state = state.copyWith();
      }
    }
  }

  void _scheduleHeavyRowsHydration(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
  }) {
    final stateKey = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      state.sessionId,
      state.generatedAt?.toString() ?? '',
    ].join('|');
    if (stateKey.trim().isEmpty || _heavyHydrationKey == stateKey) {
      return;
    }
    _heavyHydrationKey = stateKey;
    _heavyHydrationTimer?.cancel();
    _heavyHydrationTimer = Timer(const Duration(milliseconds: 900), () {
      unawaited(
        _hydrateHeavyRows(
          seedId,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          expectedStateKey: stateKey,
        ),
      );
    });
  }

  Future<void> _hydrateHeavyRows(
    String? seedId, {
    required String expectedStateKey,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
  }) async {
    if (!mounted) return;
    final currentStateKey = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      state.sessionId,
      state.generatedAt?.toString() ?? '',
    ].join('|');
    if (currentStateKey != expectedStateKey || isLoading) {
      return;
    }
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        seedId,
        limit: 8,
        forceRefresh: false,
        hydrateHeavyRows: true,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
      );
      if (!mounted) return;
      logProxyDiagnostic(
        'recommend',
        'heavy hydrate start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''}',
      );
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        recommendRequestTimeout,
      );
      if (!mounted || currentStateKey != expectedStateKey) return;
      if (res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final nextState = _feedStateFromPayload(payload);
      if (!nextState.hasRows) {
        return;
      }
      final deferredKinds = {
        ..._deferredRowKindsFromDiagnostics(state.diagnostics),
        ..._deferredRowKindsFromDiagnostics(nextState.diagnostics),
      };
      if (deferredKinds.isEmpty) {
        return;
      }
      final existingKinds = state.rows.map((row) => row.kind).toSet();
      final appendedRows = nextState.rows.where((row) {
        return deferredKinds.contains(row.kind) &&
            !existingKinds.contains(row.kind) &&
            row.items.isNotEmpty;
      }).toList(growable: false);
      if (appendedRows.isEmpty) {
        return;
      }
      final remainingDeferredKinds = deferredKinds
          .where((kind) => !appendedRows.any((row) => row.kind == kind))
          .toList(growable: false);
      state = state.copyWith(
        rows: [...state.rows, ...appendedRows],
        diagnostics: {
          ...state.diagnostics,
          ...nextState.diagnostics,
          'deferred_row_kinds': remainingDeferredKinds,
          'deferred_rows_pending': remainingDeferredKinds.isNotEmpty,
          'heavy_rows_hydrated': true,
          'heavy_rows_pending': remainingDeferredKinds.isNotEmpty,
        },
        requestState: 'complete',
        clearError: true,
      );
      _primeRecommendationRows(appendedRows);
      logProxyDiagnostic(
        'recommend',
        'heavy hydrate appended=${appendedRows.length} session=${state.sessionId}',
      );
    } catch (_) {
      return;
    }
  }

  Future<void> loadQuickRecommendations(String seedId) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
    isLoading = true;
    if (_isRequestCurrent(requestVersion)) {
      state = state.copyWith(requestState: 'loading', clearError: true);
    }
    try {
      final body = await buildRecommendationRequestBody(ref, seedId, limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      logProxyDiagnostic(
        'recommend',
        'quick request start scope=${body['user_scope_id']} seed=$seedId force=false',
      );
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        recommendRequestTimeout,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode != 200) {
        logProxyDiagnostic(
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
      logProxyDiagnostic(
        'recommend',
        'quick response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
      );
      if (!nextState.hasRows) {
        logProxyDiagnostic(
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
      logProxyDiagnostic('recommend', 'quick request timeout=$error');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: recommendTimeoutMessage,
        );
      }
    } catch (error) {
      logProxyDiagnostic('recommend', 'quick request error=$error');
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

  Future<void> loadRecommendations([
    String? seedId,
    bool forceRefresh = false,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
    bool preferFreshRows = false,
  ]) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
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
    isLoading = true;
    final preserveVisibleRows = preferFreshRows && state.hasRows;
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
        forceRefresh: forceRefresh,
        preferFreshRows: preferFreshRows,
        extraArtistHints: mergedArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: mergedSessionQueries,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      logProxyDiagnostic(
        'recommend',
        'request start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''} force=$forceRefresh preferFresh=$preferFreshRows artistHints=${compactProxyDiagnosticValue(body['artist_hints'])} tasteQueries=${compactProxyDiagnosticValue(body['taste_queries'])}',
      );
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        recommendRequestTimeout,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        _logRecommendationDiagnostics('main', payload);
        final nextState = _feedStateFromPayload(payload);
        logProxyDiagnostic(
          'recommend',
          'response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
        );
        if (nextState.hasRows) {
          state = nextState.copyWith(
            requestState: 'complete',
            clearError: true,
          );
          if (usedQueuedSessionIntent) {
            _clearQueuedSessionIntent();
          }
          _primeRecommendationRows(state.rows);
          if (!forceRefresh &&
              !preferFreshRows &&
              _diagnosticFlag(payload, 'heavy_rows_pending')) {
            _scheduleHeavyRowsHydration(
              seedId,
              extraArtistHints: mergedArtistHints,
              extraTasteQueries: extraTasteQueries,
            );
          }
          if (!forceRefresh && _shouldPrepareNextSession(payload)) {
            _scheduleBackgroundRecommendationsRefresh(
              seedId,
              extraArtistHints: mergedArtistHints,
              extraTasteQueries: extraTasteQueries,
              extraSessionQueries: mergedSessionQueries,
            );
          }
          return;
        }
        logProxyDiagnostic(
          'recommend',
          'response parsed empty rows rawRows=${((payload['rows'] as List?) ?? const []).length}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage:
                'Recommendation engine returned no rows. Pull to refresh and try again.',
          );
        }
      } else {
        logProxyDiagnostic(
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
      logProxyDiagnostic('recommend', 'request timeout=$e');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: recommendTimeoutMessage,
        );
      }
    } catch (e) {
      logProxyDiagnostic('recommend', 'request error=$e');
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
    RecommendationFeedRowState? targetRow;
    for (final row in state.rows) {
      if (row.id == rowId) {
        targetRow = row;
        break;
      }
    }
    if (targetRow == null ||
        !targetRow.hasMore ||
        state.sessionId.isEmpty ||
        _paginatingRows.contains(rowId)) {
      return;
    }
    final currentRow = targetRow;
    _paginatingRows.add(rowId);
    if (mounted) {
      state = state.copyWith();
    }
    try {
      final pageLimit = targetRow.kind == 'quiet_picks' ? 10 : 8;
      logProxyDiagnostic(
        'recommend',
        'row page start row=$rowId kind=${targetRow.kind} offset=${targetRow.nextOffset} session=${state.sessionId} hasMore=${targetRow.hasMore}',
      );
      final body = await buildRecommendationRequestBody(
        ref,
        null,
        limit: pageLimit,
        offset: targetRow.nextOffset,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = rowId;
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        recommendRowPageTimeout,
      );
      if (!mounted) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final rowPayload = payload['row'];
        if (rowPayload is Map<String, dynamic>) {
          final newRow = RecommendationFeedRowState.fromJson(rowPayload);
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
          logProxyDiagnostic(
            'recommend',
            'row page success row=$rowId appended=${mergedItems.length - currentRow.items.length} nextOffset=${progressed ? newRow.nextOffset : currentRow.nextOffset} hasMore=${progressed ? newRow.hasMore : false}',
          );
          _primeRecommendationResults(
            newRow.items.take(3),
            maxIds: 2,
            lookahead: 4,
          );
        }
      } else {
        logProxyDiagnostic(
          'recommend',
          'row page status=${res.statusCode} row=$rowId session=${state.sessionId} body=${res.body}',
        );
        if (res.statusCode == 404) {
          final updatedRows = state.rows
              .map(
                (row) => row.id == rowId
                    ? row.copyWith(hasMore: false)
                    : row,
              )
              .toList(growable: false);
          state = state.copyWith(rows: updatedRows, clearError: true);
        }
      }
    } on TimeoutException {
      if (mounted) {
        state = state.copyWith(
          requestState: 'failed',
          errorMessage: recommendTimeoutMessage,
        );
      }
    } catch (e) {
      if (mounted) {
        state = state.copyWith(
          requestState: 'failed',
          errorMessage: proxyUnavailableMessage,
        );
      }
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
        limit: max(targetRow.items.length, 6),
        offset: 0,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = rowId;
      body['row_context'] = rowContext.trim();
      final res = await runRecommendationRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        recommendRowPageTimeout,
      );
      if (!mounted || res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rowPayload = payload['row'];
      if (rowPayload is! Map<String, dynamic>) return;
      final nextRow = RecommendationFeedRowState.fromJson(rowPayload);
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
