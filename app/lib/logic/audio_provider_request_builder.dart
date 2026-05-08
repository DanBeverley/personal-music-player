import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart' show authProvider;
import 'cloud_search_queries.dart';
import 'history_manager.dart';
import 'library_catalog_provider.dart';
import 'playlist_provider.dart';
import 'search_semantics.dart';
import 'track_metadata.dart';

enum RecommendationRequestMode {
  launch,
  quick,
  queueSupplemental,
  pullToRefresh,
  backgroundPrepare,
  rowPage,
  rowContext,
  liveRowRefresh,
  flagshipRefine,
}

class _RecommendationRequestBehavior {
  final bool forceRefresh;
  final bool prepareNextSession;
  final bool preferFreshRows;
  final bool hydrateHeavyRows;

  const _RecommendationRequestBehavior({
    this.forceRefresh = false,
    this.prepareNextSession = false,
    this.preferFreshRows = false,
    this.hydrateHeavyRows = false,
  });
}

_RecommendationRequestBehavior _recommendationRequestBehavior(
  RecommendationRequestMode requestMode, {
  bool preferFreshRows = false,
}) {
  switch (requestMode) {
    case RecommendationRequestMode.pullToRefresh:
      return const _RecommendationRequestBehavior(forceRefresh: true);
    case RecommendationRequestMode.backgroundPrepare:
      return const _RecommendationRequestBehavior(prepareNextSession: true);
    case RecommendationRequestMode.flagshipRefine:
      return _RecommendationRequestBehavior(
        prepareNextSession: true,
        preferFreshRows: preferFreshRows,
        hydrateHeavyRows: true,
      );
    case RecommendationRequestMode.launch:
    case RecommendationRequestMode.quick:
    case RecommendationRequestMode.queueSupplemental:
    case RecommendationRequestMode.rowPage:
    case RecommendationRequestMode.rowContext:
    case RecommendationRequestMode.liveRowRefresh:
      return _RecommendationRequestBehavior(
        preferFreshRows: preferFreshRows,
      );
  }
}

bool _looksLikeRecommendationTasteQuery(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return false;
  if (isMetadataHeavyQuery(normalized)) return false;
  if (normalized.length > 38) {
    return sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
  }
  if (normalized.contains(' mix') ||
      normalized.contains(' playlist') ||
      normalized.contains(' songs') ||
      normalized.contains(' music')) {
    return true;
  }
  return sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
}

Future<Map<String, dynamic>> buildRecommendationRequestBody(
  Ref ref,
  String? seedId, {
  required int limit,
  int offset = 0,
  Set<String> avoidIds = const <String>{},
  RecommendationRequestMode requestMode = RecommendationRequestMode.launch,
  bool preferFreshRows = false,
  List<String> extraArtistHints = const <String>[],
  List<String> extraTasteQueries = const <String>[],
  List<String> extraSessionQueries = const <String>[],
  bool allowNetworkCloudQueries = true,
}) async {
  final behavior = _recommendationRequestBehavior(
    requestMode,
    preferFreshRows: preferFreshRows,
  );
  final playlists = ref.read(playlistProvider);
  final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
  final storageScopeId = ref.read(authProvider).storageScopeId;
  final requestSignals = await Future.wait<Object?>([
    HistoryManager.getRecentTrackSnapshots(
      limit: behavior.forceRefresh ? 14 : 10,
    ),
    HistoryManager.getLastPlayedTrackSnapshots(limit: 8),
    HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 10),
    HistoryManager.getRecentSeeds(limit: behavior.forceRefresh ? 14 : 10),
    HistoryManager.getFrequentlyPlayedTrackIds(limit: 10),
    allowNetworkCloudQueries
        ? getRecentCloudSearchQueries(limit: 8)
        : Future<List<String>>.value(peekRecentCloudSearchQueries(limit: 8)),
  ]);
  final recentTrackSnapshots =
      List<Map<String, dynamic>>.from(requestSignals[0] as List);
  final lastPlayedSnapshots =
      List<Map<String, dynamic>>.from(requestSignals[1] as List);
  final topTrackSnapshots =
      List<Map<String, dynamic>>.from(requestSignals[2] as List);
  final recentTrackIds = List<String>.from(requestSignals[3] as List);
  final topTrackIds = List<String>.from(requestSignals[4] as List);
  final recentQueries = List<String>.from(requestSignals[5] as List);

  final seedIds = <String>[];
  final blendedRecentQueries = <String>[];
  final explicitTasteQueries = <String>[];
  final explicitArtistHints = <String>[];
  final libraryTrackIds = <String>[];
  final offlineTrackIds = <String>[];

  void addUniqueValue(List<String> values, String? value) {
    final normalized = value?.trim();
    if (normalized == null || normalized.isEmpty) return;
    if (!values.contains(normalized)) {
      values.add(normalized);
    }
  }

  void addSeed(String? id) {
    final normalized = id?.trim();
    if (normalized == null || normalized.isEmpty) return;
    if (!seedIds.contains(normalized)) {
      seedIds.add(normalized);
    }
  }

  for (final query in extraTasteQueries) {
    addUniqueValue(explicitTasteQueries, query);
    addUniqueValue(blendedRecentQueries, query);
  }
  for (final query in extraSessionQueries) {
    addUniqueValue(blendedRecentQueries, query);
    if (_looksLikeRecommendationTasteQuery(query)) {
      addUniqueValue(explicitTasteQueries, query);
    }
  }
  for (final query in recentQueries) {
    addUniqueValue(blendedRecentQueries, query);
  }

  for (final artistHint in extraArtistHints) {
    addUniqueValue(explicitArtistHints, artistHint);
  }

  addSeed(seedId);
  for (final snapshot in recentTrackSnapshots) {
    addSeed(extractTrackId(snapshot));
  }
  for (final recentSeed in recentTrackIds) {
    addSeed(recentSeed);
  }
  for (final frequentSeed in topTrackIds.take(6)) {
    addSeed(frequentSeed);
  }

  for (final track in libraryTracks.take(24)) {
    final normalizedTrack = normalizeTrack(track);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId == null || trackId.isEmpty) continue;
    libraryTrackIds.add(trackId);
    if (normalizedTrack['is_downloaded_locally'] == true) {
      offlineTrackIds.add(trackId);
    }
  }

  return {
    'query': '',
    'limit': limit,
    'offset': offset,
    'user_scope_id': storageScopeId,
    'force_refresh': behavior.forceRefresh,
    'prepare_next_session': behavior.prepareNextSession,
    'prefer_fresh_rows': behavior.preferFreshRows,
    if (behavior.preferFreshRows)
      'refresh_token': DateTime.now().millisecondsSinceEpoch.toString(),
    'hydrate_heavy_rows': behavior.hydrateHeavyRows,
    if (seedIds.isNotEmpty) 'seed_id': seedIds.first,
    'seed_ids': seedIds
        .take(behavior.forceRefresh ? 6 : 5)
        .toList(growable: false),
    'recent_track_ids': recentTrackIds,
    'top_track_ids': topTrackIds,
    'recent_track_snapshots': recentTrackSnapshots,
    'top_track_snapshots': topTrackSnapshots,
    'last_played_tracks': lastPlayedSnapshots,
    'recent_queries': blendedRecentQueries,
    'playlist_names': playlists
        .map((playlist) => playlist.name)
        .take(10)
        .toList(growable: false),
    'library_track_ids': libraryTrackIds.take(28).toList(growable: false),
    'offline_track_ids': offlineTrackIds.take(28).toList(growable: false),
    if (explicitArtistHints.isNotEmpty)
      'artist_hints': explicitArtistHints.take(6).toList(growable: false),
    if (explicitTasteQueries.isNotEmpty)
      'taste_queries': explicitTasteQueries.take(8).toList(growable: false),
    'avoid_ids': avoidIds.take(40).toList(growable: false),
  };
}

Map<String, dynamic> buildRecommendationRowRefreshBody(
  Ref ref, {
  required String sessionId,
  required String rowId,
  String rowContext = 'flagship_refine',
  int limit = 8,
  RecommendationRequestMode requestMode =
      RecommendationRequestMode.flagshipRefine,
  bool preferFreshRows = false,
}) {
  final behavior = _recommendationRequestBehavior(
    requestMode,
    preferFreshRows: preferFreshRows,
  );
  final storageScopeId = ref.read(authProvider).storageScopeId;
  return {
    'query': '',
    'user_scope_id': storageScopeId,
    'session_id': sessionId,
    'row_id': rowId,
    'row_context': rowContext,
    'offset': 0,
    'limit': limit,
    'prefer_fresh_rows': behavior.preferFreshRows,
    if (behavior.preferFreshRows)
      'refresh_token': DateTime.now().millisecondsSinceEpoch.toString(),
    'prepare_next_session': behavior.prepareNextSession,
    'hydrate_heavy_rows': behavior.hydrateHeavyRows,
  };
}
