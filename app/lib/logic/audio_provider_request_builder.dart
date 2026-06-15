import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart' show authProvider;
import 'cloud_search_queries.dart';
import 'history_manager.dart';
import 'library_catalog_provider.dart';
import 'playlist_provider.dart';
import 'proxy_runtime.dart';
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
}

class _RecommendationRequestBehavior {
  final bool prepareNextSession;
  final bool preferFreshRows;

  const _RecommendationRequestBehavior({
    this.prepareNextSession = false,
    this.preferFreshRows = false,
  });
}

_RecommendationRequestBehavior _recommendationRequestBehavior(
  RecommendationRequestMode requestMode, {
  bool preferFreshRows = false,
}) {
  switch (requestMode) {
    case RecommendationRequestMode.pullToRefresh:
      return const _RecommendationRequestBehavior(
        prepareNextSession: true,
        preferFreshRows: true,
      );
    case RecommendationRequestMode.backgroundPrepare:
      return const _RecommendationRequestBehavior(prepareNextSession: true);
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

bool _hasNonEmptySignal(dynamic value) {
  if (value is String) return value.trim().isNotEmpty;
  if (value is Iterable) return value.any(_hasNonEmptySignal);
  if (value is Map) return value.values.any(_hasNonEmptySignal);
  return value != null;
}

bool recommendationRequestHasUserSignals(Map<String, dynamic> body) {
  for (final key in const [
    'seed_id',
    'seed_ids',
    'recent_track_ids',
    'top_track_ids',
    'recent_track_snapshots',
    'top_track_snapshots',
    'last_played_tracks',
    'recent_queries',
    'taste_queries',
    'artist_hints',
    'playlist_names',
    'library_track_ids',
    'offline_track_ids',
  ]) {
    if (_hasNonEmptySignal(body[key])) {
      return true;
    }
  }
  return false;
}

bool recommendationRequestShouldShowFreshStart(Map<String, dynamic> body) {
  if (body['fresh_account_empty_home'] == true) return true;
  final signalTier = body['client_signal_tier']?.toString().trim();
  if (signalTier == 'cold_start') return true;
  return !recommendationRequestHasUserSignals(body);
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
  final requestBuildWatch = Stopwatch()..start();
  final behavior = _recommendationRequestBehavior(
    requestMode,
    preferFreshRows: preferFreshRows,
  );
  final playlists = ref.read(playlistProvider);
  final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
  final storageScopeId = ref.read(authProvider).storageScopeId;
  final hydrateWatch = Stopwatch()..start();
  await HistoryManager.hydrateFromPersistedHistoryIfNeeded(limit: 36);
  hydrateWatch.stop();
  final signalWatch = Stopwatch()..start();
  final requestSignals = await Future.wait<Object?>([
    HistoryManager.getRecentTrackSnapshots(limit: 10),
    HistoryManager.getLastPlayedTrackSnapshots(limit: 8),
    HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 10),
    HistoryManager.getRecentSeeds(limit: 10),
    HistoryManager.getFrequentlyPlayedTrackIds(limit: 10),
      allowNetworkCloudQueries
        ? getRecentCloudSearchQueries(limit: 8)
        : Future<List<String>>.value(peekRecentCloudSearchQueries(limit: 8)),
  ]);
  signalWatch.stop();
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

  final hasUserSignals = seedIds.isNotEmpty ||
      recentTrackIds.isNotEmpty ||
      topTrackIds.isNotEmpty ||
      recentTrackSnapshots.isNotEmpty ||
      topTrackSnapshots.isNotEmpty ||
      lastPlayedSnapshots.isNotEmpty ||
      blendedRecentQueries.isNotEmpty ||
      explicitTasteQueries.isNotEmpty ||
      explicitArtistHints.isNotEmpty ||
      playlists.isNotEmpty ||
      libraryTrackIds.isNotEmpty ||
      offlineTrackIds.isNotEmpty;

  final payload = {
    'query': '',
    'limit': limit,
    'offset': offset,
    'user_scope_id': storageScopeId,
    'client_signal_tier': hasUserSignals ? 'personalized' : 'cold_start',
    'fresh_account_empty_home': !hasUserSignals,
    'force_refresh': false,
    'prepare_next_session': behavior.prepareNextSession,
    'prefer_fresh_rows': behavior.preferFreshRows,
    if (behavior.preferFreshRows)
      'refresh_token': DateTime.now().millisecondsSinceEpoch.toString(),
    if (seedIds.isNotEmpty) 'seed_id': seedIds.first,
    'seed_ids': seedIds.take(5).toList(growable: false),
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
  requestBuildWatch.stop();
  debugProxyLog(
    'recommend',
    'request body timing mode=$requestMode hydrateMs=${hydrateWatch.elapsedMilliseconds} signalMs=${signalWatch.elapsedMilliseconds} totalMs=${requestBuildWatch.elapsedMilliseconds} hasSignals=$hasUserSignals',
  );
  return payload;
}
