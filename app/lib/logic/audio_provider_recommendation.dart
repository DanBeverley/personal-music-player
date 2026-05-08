import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;

import 'audio_provider.dart' show audioPlayerProvider;
import 'audio_provider_request_builder.dart';
import 'auth_provider.dart' show authProvider;
import 'history_manager.dart';
import 'interaction_events.dart' show recommendationSignalStream;
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
  Timer? _deferredHydrationTimer;
  Timer? _continueListeningRefreshTimer;
  Timer? _backgroundRefreshTimer;
  String _deferredHydrationKey = '';
  String _backgroundRefreshKey = '';
  String _flagshipHydrationStreamKey = '';
  final List<String> _queuedSessionArtistHints = <String>[];
  final List<String> _queuedSessionQueries = <String>[];
  StreamSubscription<Map<String, dynamic>>? _historyTrackSubscription;
  StreamSubscription<String>? _recommendationSignalSubscription;
  StreamSubscription<String>? _flagshipHydrationStreamSubscription;

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
    await loadRecommendations(
      seed,
      requestMode: forceRefresh
          ? RecommendationRequestMode.pullToRefresh
          : RecommendationRequestMode.launch,
    );
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
    final flagshipPending = diagnostics['flagship_rows_pending'] == true;
    final flagshipPartial = diagnostics['flagship_rows_partial'] == true;
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
        flagshipPending ||
        flagshipPartial ||
        launchTierOnly ||
        heavyPending ||
        heavyPartial;
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
        requestMode: RecommendationRequestMode.backgroundPrepare,
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
        debugProxyLog(
          'recommend',
          'background refresh status=${res.statusCode} body=${res.body}',
        );
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _logRecommendationDiagnostics('background', payload);
      debugProxyLog(
        'recommend',
        'background prepare scheduled=${payload['prepared'] == true} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
      );
    } on TimeoutException catch (error) {
      debugProxyLog('recommend', 'background refresh timeout=$error');
    } catch (error) {
      debugProxyLog('recommend', 'background refresh error=$error');
    } finally {
      if (_backgroundRefreshKey == expectedKey) {
        _backgroundRefreshKey = '';
      }
    }
  }

  @override
  void dispose() {
    _requestVersion++;
    _deferredHydrationTimer?.cancel();
    _continueListeningRefreshTimer?.cancel();
    _backgroundRefreshTimer?.cancel();
    unawaited(_historyTrackSubscription?.cancel());
    unawaited(_recommendationSignalSubscription?.cancel());
    unawaited(_flagshipHydrationStreamSubscription?.cancel());
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
      visibleTracks.addAll(row.items.take(5));
      if (visibleTracks.length >= 20) {
        break;
      }
    }
    if (visibleTracks.isEmpty) return;
    _primeRecommendationResults(
      visibleTracks,
      maxIds: 8,
      lookahead: 12,
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

  RecommendationFeedRowState? _rowByKind(
    String rowKind, [
    RecommendationFeedState? sourceState,
  ]) {
    for (final row in (sourceState ?? state).rows) {
      if (row.kind == rowKind) {
        return row;
      }
    }
    return null;
  }

  RecommendationFeedRowState? _rowById(
    String rowId, [
    RecommendationFeedState? sourceState,
  ]) {
    for (final row in (sourceState ?? state).rows) {
      if (row.id == rowId) {
        return row;
      }
    }
    return null;
  }

  Set<String> _rowKindsFromDiagnostics(
    Map<String, dynamic> diagnostics,
    String key,
  ) {
    final kinds = <String>{};
    final raw = diagnostics[key];
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

  Set<String> _flagshipDeferredRowKinds(
    Map<String, dynamic> diagnostics,
  ) =>
      _rowKindsFromDiagnostics(diagnostics, 'flagship_deferred_row_kinds');

  Set<String> _flagshipPartialRowKinds(
    Map<String, dynamic> diagnostics,
  ) =>
      _rowKindsFromDiagnostics(diagnostics, 'flagship_partial_row_kinds');

  String _currentFlagshipHydrationStateKey(
    String? seedId, [
    RecommendationFeedState? sourceState,
  ]) {
    final currentState = sourceState ?? state;
    return [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      currentState.sessionId,
      currentState.generatedAt?.toString() ?? '',
    ].join('|');
  }

  bool _isFlagshipRowKind(String kind) =>
      kind == 'quiet_picks' ||
      kind == 'mixed_for_you' ||
      kind == 'trending_by_genre' ||
      kind == 'recommended_albums' ||
      kind == 'recommended_artists';

  Set<String> _flagshipUpgradeRowKinds([
    RecommendationFeedState? sourceState,
  ]) {
    final currentState = sourceState ?? state;
    final upgradeKinds = <String>{
      ..._flagshipDeferredRowKinds(currentState.diagnostics),
      ..._flagshipPartialRowKinds(currentState.diagnostics),
    };
    for (final row in currentState.rows) {
      if (_isFlagshipRowKind(row.kind) &&
          (row.isPending || row.isRefinementActive)) {
        upgradeKinds.add(row.kind);
      }
    }
    return upgradeKinds;
  }

  bool _stateHasPartialFlagshipRows([RecommendationFeedState? sourceState]) {
    final currentState = sourceState ?? state;
    if (_flagshipPartialRowKinds(currentState.diagnostics).isNotEmpty) {
      return true;
    }
    return currentState.rows.any(
      (row) => _isFlagshipRowKind(row.kind) && row.isRefinementActive,
    );
  }

  Duration _nextFlagshipHydrationDelay({
    RecommendationFeedState? sourceState,
    int attempt = 0,
  }) {
    final currentState = sourceState ?? state;
    final upgradeKinds = _flagshipUpgradeRowKinds(currentState);
    final targetRows = currentState.rows.where(
      (row) => upgradeKinds.contains(row.kind) || upgradeKinds.contains(row.id),
    );
    var baseDelayMs = 900;
    for (final row in targetRows) {
      final candidateDelay = row.refineAfterMs;
      if (candidateDelay <= 0) continue;
      baseDelayMs = min(baseDelayMs, candidateDelay);
    }
    final retryPaddingMs = min(attempt * 120, 700);
    return Duration(milliseconds: baseDelayMs + retryPaddingMs);
  }

  Future<void> _cancelFlagshipHydrationStream({String nextKey = ''}) async {
    _flagshipHydrationStreamKey = nextKey;
    final subscription = _flagshipHydrationStreamSubscription;
    _flagshipHydrationStreamSubscription = null;
    await subscription?.cancel();
  }

  void _handleFlagshipStreamEvent({
    required String eventType,
    required Map<String, dynamic> payload,
    required String? seedId,
    required String expectedStateKey,
    required List<String> extraArtistHints,
    required List<String> extraTasteQueries,
  }) {
    if (!mounted ||
        _currentFlagshipHydrationStateKey(seedId) != expectedStateKey) {
      return;
    }
    if (eventType == 'row') {
      final rowPayload = payload['row'];
      if (rowPayload is! Map<String, dynamic>) return;
      final rowId = rowPayload['id']?.toString().trim() ?? '';
      final targetRow = _rowById(rowId) ?? _rowByKind(rowId);
      if (targetRow == null) return;
      final mergedState = _mergeRecommendationRowPayload(
        state,
        payload,
        targetRow: targetRow,
      );
      final mergedRow =
          _rowById(rowId, mergedState) ?? _rowByKind(rowId, mergedState);
      if (mergedRow != null && _rowRefinementAdvanced(targetRow, mergedRow)) {
        state = mergedState;
        _primeRecommendationRows(state.rows);
      }
      return;
    }
    if (eventType == 'complete') {
      _flagshipHydrationStreamKey = '';
      if (_flagshipUpgradeRowKinds().isNotEmpty) {
        _scheduleDeferredFlagshipHydration(
          seedId,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          attempt: 0,
          delay: _nextFlagshipHydrationDelay(),
        );
      }
    }
  }

  Future<bool> _startFlagshipHydrationStream(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
  }) async {
    final upgradeKinds = _flagshipUpgradeRowKinds().toList()..sort();
    if (upgradeKinds.isEmpty || state.sessionId.isEmpty) {
      await _cancelFlagshipHydrationStream();
      return false;
    }
    final stateKey = _currentFlagshipHydrationStateKey(seedId);
    if (stateKey.trim().isEmpty) {
      return false;
    }
    if (_flagshipHydrationStreamKey == stateKey &&
        _flagshipHydrationStreamSubscription != null) {
      return true;
    }
    _deferredHydrationTimer?.cancel();
    await _cancelFlagshipHydrationStream(nextKey: stateKey);
    try {
      final body = await buildRecommendationRequestBody(
        ref,
        seedId,
        limit: 8,
        requestMode: RecommendationRequestMode.flagshipRefine,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        allowNetworkCloudQueries: false,
      );
      if (!mounted || _currentFlagshipHydrationStateKey(seedId) != stateKey) {
        return false;
      }
      body['session_id'] = state.sessionId;
      body['prepare_next_session'] = true;
      body['hydrate_heavy_rows'] = true;
      final request = http.Request(
        'POST',
        buildProxyUri('/recommend/flagship_stream'),
      );
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
      final streamedResponse = await proxyControlHttpClient.send(request);
      if (!mounted || _currentFlagshipHydrationStateKey(seedId) != stateKey) {
        return false;
      }
      if (streamedResponse.statusCode != 200) {
        await _cancelFlagshipHydrationStream();
        return false;
      }
      var currentEvent = 'message';
      final dataLines = <String>[];

      void dispatchEvent() {
        if (dataLines.isEmpty) return;
        final data = dataLines.join('\n');
        dataLines.clear();
        Map<String, dynamic> payload;
        try {
          final decoded = jsonDecode(data);
          if (decoded is! Map<String, dynamic>) return;
          payload = decoded;
        } catch (_) {
          return;
        }
        _handleFlagshipStreamEvent(
          eventType: currentEvent,
          payload: payload,
          seedId: seedId,
          expectedStateKey: stateKey,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
        );
        currentEvent = 'message';
      }

      _flagshipHydrationStreamSubscription = streamedResponse.stream
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(
        (line) {
          if (!mounted ||
              _currentFlagshipHydrationStateKey(seedId) != stateKey) {
            return;
          }
          if (line.isEmpty) {
            dispatchEvent();
            return;
          }
          if (line.startsWith('event:')) {
            currentEvent = line.substring(6).trim();
            return;
          }
          if (line.startsWith('data:')) {
            dataLines.add(line.substring(5).trimLeft());
          }
        },
        onDone: () {
          dispatchEvent();
          _flagshipHydrationStreamSubscription = null;
          if (!mounted || _flagshipHydrationStreamKey != stateKey) return;
          _flagshipHydrationStreamKey = '';
          if (_flagshipUpgradeRowKinds().isNotEmpty) {
            _scheduleDeferredFlagshipHydration(
              seedId,
              extraArtistHints: extraArtistHints,
              extraTasteQueries: extraTasteQueries,
              attempt: 0,
              delay: _nextFlagshipHydrationDelay(),
            );
          }
        },
        onError: (_) {
          _flagshipHydrationStreamSubscription = null;
          if (!mounted || _flagshipHydrationStreamKey != stateKey) return;
          _flagshipHydrationStreamKey = '';
          _scheduleDeferredFlagshipHydration(
            seedId,
            extraArtistHints: extraArtistHints,
            extraTasteQueries: extraTasteQueries,
            attempt: 0,
            delay: _nextFlagshipHydrationDelay(),
          );
        },
        cancelOnError: true,
      );
      return true;
    } catch (_) {
      if (_flagshipHydrationStreamKey == stateKey) {
        _flagshipHydrationStreamKey = '';
      }
      return false;
    }
  }

  Future<bool> _refreshFlagshipRow({
    required String? seedId,
    required String expectedStateKey,
    required RecommendationFeedRowState targetRow,
    required int attempt,
  }) async {
    if (!mounted || state.sessionId.isEmpty) return false;
    final body = buildRecommendationRowRefreshBody(
      ref,
      sessionId: state.sessionId,
      rowId: targetRow.id,
      rowContext: 'flagship_refine',
      limit: max(targetRow.items.length, 8),
      requestMode: RecommendationRequestMode.flagshipRefine,
      preferFreshRows: attempt > 0,
    );
    debugProxyLog(
      'recommend',
      'flagship hydrate row=${targetRow.id} scope=${body['user_scope_id']} pending=${compactDiagnosticValue(_flagshipUpgradeRowKinds().toList()..sort())} attempt=$attempt',
    );
    final res = await runRecommendationRequest(
      proxyControlHttpClient.post(
        buildProxyUri('/recommend'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ),
      recommendRowPageTimeout,
    );
    if (!mounted ||
        _currentFlagshipHydrationStateKey(seedId) != expectedStateKey ||
        res.statusCode != 200) {
      return false;
    }
    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final currentTargetRow = _rowById(targetRow.id) ?? targetRow;
    final mergedState = _mergeRecommendationRowPayload(
      state,
      payload,
      targetRow: currentTargetRow,
    );
    final mergedRow = _rowById(targetRow.id, mergedState);
    final advanced = mergedRow != null &&
        _rowRefinementAdvanced(currentTargetRow, mergedRow);
    if (advanced) {
      state = mergedState;
      _primeRecommendationRows(state.rows);
    }
    return advanced;
  }

  bool _rowRefinementAdvanced(
    RecommendationFeedRowState previousRow,
    RecommendationFeedRowState nextRow,
  ) {
    if (nextRow.rowVersion > previousRow.rowVersion) {
      return true;
    }
    if (nextRow.rowState != previousRow.rowState) {
      return true;
    }
    if (nextRow.isRefinementActive != previousRow.isRefinementActive) {
      return true;
    }
    if (nextRow.items.length != previousRow.items.length) {
      return true;
    }
    final previousKeys = previousRow.items
        .map((item) => recommendationRowItemKey(previousRow.itemType, item))
        .toList(growable: false);
    final nextKeys = nextRow.items
        .map((item) => recommendationRowItemKey(nextRow.itemType, item))
        .toList(growable: false);
    if (previousKeys.length != nextKeys.length) {
      return true;
    }
    for (var index = 0; index < previousKeys.length; index++) {
      if (previousKeys[index] != nextKeys[index]) {
        return true;
      }
    }
    return false;
  }

  List<Map<String, dynamic>> _mergeIncrementalFlagshipItems(
    RecommendationFeedRowState currentRow,
    RecommendationFeedRowState nextRow,
  ) {
    if (!_isFlagshipRowKind(currentRow.kind) ||
        currentRow.kind != nextRow.kind ||
        currentRow.itemType != nextRow.itemType) {
      return nextRow.items;
    }
    if (currentRow.items.isEmpty) {
      return nextRow.items;
    }
    if (currentRow.kind == 'mixed_for_you') {
      return _mergeMixedForYouItems(currentRow, nextRow);
    }
    final nextByKey = <String, Map<String, dynamic>>{};
    for (final item in nextRow.items) {
      nextByKey[recommendationRowItemKey(nextRow.itemType, item)] =
          Map<String, dynamic>.from(item);
    }
    final merged = <Map<String, dynamic>>[];
    final seenKeys = <String>{};
    for (final item in currentRow.items) {
      final key = recommendationRowItemKey(currentRow.itemType, item);
      final replacement = nextByKey.remove(key);
      if (replacement != null) {
        merged.add(replacement);
      } else {
        merged.add(Map<String, dynamic>.from(item));
      }
      seenKeys.add(key);
    }
    for (final item in nextRow.items) {
      final key = recommendationRowItemKey(nextRow.itemType, item);
      if (seenKeys.contains(key)) {
        continue;
      }
      seenKeys.add(key);
      merged.add(Map<String, dynamic>.from(item));
    }
    return merged;
  }

  String _mixTrackSignature(Map<String, dynamic> item) {
    final tracks = (item['tracks'] as List<dynamic>? ?? const [])
        .whereType<Map>()
        .map((track) => extractTrackId(Map<String, dynamic>.from(track)) ?? '')
        .where((id) => id.trim().isNotEmpty)
        .take(12)
        .join('|');
    if (tracks.isNotEmpty) return tracks;
    return [
      item['title']?.toString().trim().toLowerCase() ?? '',
      item['subtitle']?.toString().trim().toLowerCase() ?? '',
      item['track_count']?.toString() ?? '',
    ].join('|');
  }

  String _mixVariantId(Map<String, dynamic> item) {
    final baseId = item['id']?.toString().trim().isNotEmpty == true
        ? item['id'].toString().trim()
        : item['title']?.toString().trim().toLowerCase() ?? 'mix';
    final signature = _mixTrackSignature(item)
        .replaceAll(RegExp(r'[^a-zA-Z0-9]+'), '_')
        .replaceAll(RegExp(r'_+'), '_')
        .replaceAll(RegExp(r'^_|_$'), '');
    final suffix =
        signature.length > 48 ? signature.substring(0, 48) : signature;
    return '$baseId:variant:${suffix.isEmpty ? 'update' : suffix}';
  }

  List<Map<String, dynamic>> _mergeMixedForYouItems(
    RecommendationFeedRowState currentRow,
    RecommendationFeedRowState nextRow,
  ) {
    final nextByKey = <String, Map<String, dynamic>>{};
    for (final item in nextRow.items) {
      nextByKey[recommendationRowItemKey(nextRow.itemType, item)] =
          Map<String, dynamic>.from(item);
    }
    final merged = <Map<String, dynamic>>[];
    final seenKeys = <String>{};
    final deferredVariants = <Map<String, dynamic>>[];
    for (final item in currentRow.items) {
      final currentItem = Map<String, dynamic>.from(item);
      final key = recommendationRowItemKey(currentRow.itemType, currentItem);
      final replacement = nextByKey.remove(key);
      if (replacement == null) {
        merged.add(currentItem);
        seenKeys.add(key);
        continue;
      }
      final currentSignature = _mixTrackSignature(currentItem);
      final nextSignature = _mixTrackSignature(replacement);
      if (currentSignature == nextSignature ||
          currentSignature.trim().isEmpty) {
        merged.add(replacement);
      } else {
        merged.add(currentItem);
        final appended = Map<String, dynamic>.from(replacement)
          ..['id'] = _mixVariantId(replacement);
        deferredVariants.add(appended);
      }
      seenKeys.add(key);
    }
    for (final item in [...deferredVariants, ...nextRow.items]) {
      final candidate = Map<String, dynamic>.from(item);
      final key = recommendationRowItemKey(nextRow.itemType, candidate);
      if (seenKeys.contains(key)) continue;
      seenKeys.add(key);
      merged.add(candidate);
    }
    return merged;
  }

  RecommendationFeedState _mergeRecommendationRowPayload(
    RecommendationFeedState currentState,
    Map<String, dynamic> payload, {
    required RecommendationFeedRowState targetRow,
  }) {
    final rowPayload = payload['row'];
    if (rowPayload is! Map<String, dynamic>) {
      return currentState;
    }
    final nextRow = RecommendationFeedRowState.fromJson(rowPayload);
    final mergedDiagnostics = payload['diagnostics'] is Map
        ? {
            ...currentState.diagnostics,
            ...Map<String, dynamic>.from(payload['diagnostics'] as Map),
          }
        : currentState.diagnostics;
    final mergedRow = nextRow.copyWith(
      items: _mergeIncrementalFlagshipItems(targetRow, nextRow),
      nextOffset: max(nextRow.nextOffset, targetRow.nextOffset),
      hasMore: nextRow.hasMore || targetRow.hasMore,
    );
    final updatedRows = currentState.rows
        .map((row) => row.id == targetRow.id ? mergedRow : row)
        .toList(growable: false);
    var nextState = currentState.copyWith(
      sessionId: (payload['session_id']?.toString().trim().isNotEmpty ?? false)
          ? payload['session_id'].toString()
          : currentState.sessionId,
      generatedAt: (payload['generated_at'] as num?)?.toDouble() ??
          currentState.generatedAt,
      expiresAt:
          (payload['expires_at'] as num?)?.toDouble() ?? currentState.expiresAt,
      rows: updatedRows,
      diagnostics: mergedDiagnostics,
      requestState: 'complete',
      clearError: true,
    );
    final remainingUpgradeKinds = _flagshipUpgradeRowKinds(nextState);
    nextState = nextState.copyWith(
      diagnostics: {
        ...nextState.diagnostics,
        'heavy_rows_hydrated': remainingUpgradeKinds
            .where((kind) =>
                kind == 'recommended_albums' || kind == 'recommended_artists')
            .isEmpty,
      },
    );
    return nextState;
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
        requestMode: RecommendationRequestMode.liveRowRefresh,
        allowNetworkCloudQueries: false,
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
      state = _mergeRecommendationRowPayload(
        state,
        payload,
        targetRow: targetRow,
      );
      final refreshedRow = _rowByKind('continue_listening');
      if (refreshedRow != null) {
        _primeRecommendationResults(refreshedRow.items);
      }
    } catch (_) {
      // Keep the current feed stable if the live row refresh fails.
    } finally {
      _paginatingRows.remove(targetRow.id);
      if (mounted) {
        state = state.copyWith();
      }
    }
  }

  void _scheduleDeferredFlagshipHydration(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    int attempt = 0,
    Duration? delay,
  }) {
    final upgradeKinds = _flagshipUpgradeRowKinds().toList()..sort();
    if (upgradeKinds.isEmpty) {
      _deferredHydrationKey = '';
      return;
    }
    final stateKey = _currentFlagshipHydrationStateKey(seedId);
    if (stateKey.trim().isEmpty) {
      return;
    }
    if (attempt == 0 && _deferredHydrationKey == stateKey) {
      return;
    }
    _deferredHydrationKey = stateKey;
    _deferredHydrationTimer?.cancel();
    _deferredHydrationTimer = Timer(
      delay ?? _nextFlagshipHydrationDelay(attempt: attempt),
      () {
        unawaited(
          _hydrateDeferredFlagshipRows(
            seedId,
            extraArtistHints: extraArtistHints,
            extraTasteQueries: extraTasteQueries,
            expectedStateKey: stateKey,
            attempt: attempt,
          ),
        );
      },
    );
  }

  Future<void> _hydrateDeferredFlagshipRows(
    String? seedId, {
    required String expectedStateKey,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    required int attempt,
  }) async {
    if (!mounted) return;
    final upgradeKinds = _flagshipUpgradeRowKinds().toList()..sort();
    if (upgradeKinds.isEmpty) {
      _deferredHydrationKey = '';
      return;
    }
    final currentStateKey = _currentFlagshipHydrationStateKey(seedId);
    if (currentStateKey != expectedStateKey || isLoading) {
      return;
    }
    void scheduleRetry() {
      if (attempt >= 7) {
        if (_deferredHydrationKey == expectedStateKey) {
          _deferredHydrationKey = '';
        }
        return;
      }
      _scheduleDeferredFlagshipHydration(
        seedId,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        attempt: attempt + 1,
        delay: _nextFlagshipHydrationDelay(attempt: attempt + 1),
      );
    }

    try {
      final targetRows = upgradeKinds
          .map((rowKind) => _rowByKind(rowKind))
          .whereType<RecommendationFeedRowState>()
          .toList(growable: false);
      final results = await Future.wait(
        targetRows.map(
          (targetRow) => _refreshFlagshipRow(
            seedId: seedId,
            expectedStateKey: expectedStateKey,
            targetRow: targetRow,
            attempt: attempt,
          ),
        ),
      );
      if (!mounted || currentStateKey != expectedStateKey) return;
      final refreshedAnyRow = results.any((advanced) => advanced);
      if (!refreshedAnyRow) {
        if (_flagshipUpgradeRowKinds().isNotEmpty) {
          scheduleRetry();
        } else {
          _deferredHydrationKey = '';
        }
        return;
      }
      final nextUpgradeKinds = _flagshipUpgradeRowKinds().toList()..sort();
      debugProxyLog(
        'recommend',
        'flagship hydrate rows=${state.rows.length} pending=${compactDiagnosticValue(nextUpgradeKinds)} session=${state.sessionId}',
      );
      if (nextUpgradeKinds.isNotEmpty && attempt < 7) {
        scheduleRetry();
        return;
      }
      _deferredHydrationKey = '';
    } catch (_) {
      scheduleRetry();
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
    await _cancelFlagshipHydrationStream();
    isLoading = true;
    final preserveVisibleRows = forceRefresh && state.hasRows;
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
      );
      if (!_isRequestCurrent(requestVersion)) return;
      debugProxyLog(
        'recommend',
        'request start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''} mode=$requestMode force=$forceRefresh preferFresh=$preferFreshRows artistHints=${compactDiagnosticValue(body['artist_hints'])} tasteQueries=${compactDiagnosticValue(body['taste_queries'])}',
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
          _primeRecommendationRows(state.rows);
          if (_diagnosticFlag(payload, 'flagship_rows_pending') ||
              _diagnosticFlag(payload, 'flagship_rows_partial') ||
              _stateHasPartialFlagshipRows(nextState)) {
            final streamStarted = await _startFlagshipHydrationStream(
              seedId,
              extraArtistHints: mergedArtistHints,
              extraTasteQueries: extraTasteQueries,
            );
            if (!streamStarted) {
              _scheduleDeferredFlagshipHydration(
                seedId,
                extraArtistHints: mergedArtistHints,
                extraTasteQueries: extraTasteQueries,
              );
            }
          }
          if (!forceRefresh &&
              (_shouldPrepareNextSession(payload) ||
                  _stateHasPartialFlagshipRows(nextState))) {
            _scheduleBackgroundRecommendationsRefresh(
              seedId,
              extraArtistHints: mergedArtistHints,
              extraTasteQueries: extraTasteQueries,
              extraSessionQueries: mergedSessionQueries,
            );
          }
          return;
        }
        debugProxyLog(
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
      debugProxyLog(
        'recommend',
        'row page start row=$rowId kind=${targetRow.kind} offset=${targetRow.nextOffset} session=${state.sessionId} hasMore=${targetRow.hasMore}',
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
        requestMode: RecommendationRequestMode.rowContext,
        allowNetworkCloudQueries: false,
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
