import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';
import 'cloud_search_queries.dart';
import 'history_manager.dart';
import 'library_catalog_provider.dart';
import 'playlist_provider.dart';
import 'proxy_runtime.dart';
import 'search_semantics.dart';
import 'track_metadata.dart';

typedef ProviderReader = T Function<T>(ProviderListenable<T> provider);

Future<Map<String, dynamic>> buildSemanticSearchRequestBody(
  ProviderReader read,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
  String searchMode = '',
  String resultType = '',
  int offset = 0,
  int? revision,
  int? revisionWaitMs,
}) async {
  final normalizedQuery = query.trim();
  final normalizedSearchMode = searchMode.isNotEmpty
      ? searchMode
      : classifySearchQueryMode(normalizedQuery);
  final storageScopeId = read(authProvider).storageScopeId;
  if (normalizedSearchMode != 'taste') {
    return <String, dynamic>{
      'query': normalizedQuery,
      'limit': limit,
      'user_scope_id': storageScopeId,
      'search_mode': normalizedSearchMode,
      'context_surface': 'interactive_search',
      'defer_side_surfaces': deferSideSurfaces,
      'result_type': resultType,
      'offset': offset,
      if (revision != null) 'revision': revision,
      if (revisionWaitMs != null) 'revision_wait_ms': revisionWaitMs,
    };
  }
  final playlists = read(playlistProvider);
  final libraryTracks = read(libraryProvider).valueOrNull ?? const [];
  final searchSignals = await Future.wait<Object?>([
    HistoryManager.getRecentTrackSnapshots(limit: 12),
    HistoryManager.getLastPlayedTrackSnapshots(limit: 8),
    HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 10),
    HistoryManager.getRecentSeeds(limit: 12),
    HistoryManager.getFrequentlyPlayedTrackIds(limit: 10),
    getRecentCloudSearchQueries(limit: 8),
  ]);

  final recentTrackSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[0] as List);
  final lastPlayedSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[1] as List);
  final topTrackSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[2] as List);
  final recentTrackIds = List<String>.from(searchSignals[3] as List);
  final topTrackIds = List<String>.from(searchSignals[4] as List);
  final recentQueries = List<String>.from(searchSignals[5] as List);

  final artistWeights = <String, double>{};
  final albumWeights = <String, double>{};
  final queryWeights = <String, double>{};
  final blendedRecentQueries = <String>[];
  final libraryTrackIds = <String>[];
  final offlineTrackIds = <String>[];

  void addRecentQuery(String? value) {
    final normalized = value?.trim();
    if (normalized == null || normalized.isEmpty) return;
    if (!blendedRecentQueries.contains(normalized)) {
      blendedRecentQueries.add(normalized);
    }
  }

  void addAlbumWeight(String? value, double weight) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    albumWeights.update(
      normalized,
      (current) => current + weight,
      ifAbsent: () => weight,
    );
  }

  void addTrackHints(
    Iterable<Map<String, dynamic>> tracks, {
    required double artistWeight,
    required double albumWeight,
  }) {
    for (final track in tracks) {
      bumpSearchSignalWeight(
        artistWeights,
        extractSearchArtistHint(track),
        artistWeight,
      );
      bumpSearchSignalWeight(
        queryWeights,
        extractSearchAlbumHint(track),
        albumWeight * 0.8,
      );
      addAlbumWeight(extractSearchAlbumHint(track), albumWeight);
    }
  }

  addRecentQuery(normalizedQuery);
  for (final queryValue in recentQueries) {
    addRecentQuery(queryValue);
  }

  addTrackHints(
    lastPlayedSnapshots,
    artistWeight: 1.45,
    albumWeight: 1.0,
  );
  addTrackHints(
    recentTrackSnapshots,
    artistWeight: 1.1,
    albumWeight: 0.82,
  );
  addTrackHints(
    topTrackSnapshots,
    artistWeight: 1.7,
    albumWeight: 1.18,
  );

  for (final playlist in playlists) {
    bumpSearchSignalWeight(queryWeights, playlist.name, 1.25);
    for (final track in playlist.tracks.take(16)) {
      bumpSearchSignalWeight(
        artistWeights,
        extractSearchArtistHint(track),
        1.28,
      );
      bumpSearchSignalWeight(
        queryWeights,
        extractSearchAlbumHint(track),
        0.84,
      );
      addAlbumWeight(extractSearchAlbumHint(track), 1.0);
    }
  }

  for (final track in libraryTracks.take(24)) {
    final normalizedTrack = normalizeTrack(track);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId != null && trackId.isNotEmpty) {
      libraryTrackIds.add(trackId);
      if (normalizedTrack['is_downloaded_locally'] == true) {
        offlineTrackIds.add(trackId);
      }
    }
    bumpSearchSignalWeight(
      artistWeights,
      extractSearchArtistHint(track),
      1.12,
    );
    bumpSearchSignalWeight(
      queryWeights,
      extractSearchAlbumHint(track),
      0.7,
    );
    addAlbumWeight(extractSearchAlbumHint(track), 0.88);
  }

  bumpSearchSignalWeight(queryWeights, normalizedQuery, 3.7);

  for (final queryValue in blendedRecentQueries) {
    if (looksLikeSemanticTasteQuery(queryValue)) {
      bumpSearchSignalWeight(queryWeights, queryValue, 1.18);
    }
  }

  final rankedArtists = artistWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));
  final rankedAlbums = albumWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));
  final rankedQueries = queryWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));

  return {
    'query': normalizedQuery,
    'limit': limit,
    'user_scope_id': storageScopeId,
    'search_mode': normalizedSearchMode,
    'context_surface': 'interactive_search',
    'defer_side_surfaces': deferSideSurfaces,
    'result_type': resultType,
    'offset': offset,
    'recent_track_ids': recentTrackIds,
    'top_track_ids': topTrackIds,
    'recent_track_snapshots': recentTrackSnapshots,
    'top_track_snapshots': topTrackSnapshots,
    'last_played_tracks': lastPlayedSnapshots,
    'recent_queries': blendedRecentQueries,
    'taste_queries':
        rankedQueries.take(8).map((entry) => entry.key).toList(growable: false),
    'artist_hints':
        rankedArtists.take(8).map((entry) => entry.key).toList(growable: false),
    'album_hints':
        rankedAlbums.take(6).map((entry) => entry.key).toList(growable: false),
    'playlist_names': playlists
        .map((playlist) => playlist.name)
        .take(10)
        .toList(growable: false),
    'library_track_ids': libraryTrackIds.take(28).toList(growable: false),
    'offline_track_ids': offlineTrackIds.take(28).toList(growable: false),
  };
}

class SearchFetchResult {
  final Map<String, dynamic>? payload;
  final String status;
  final int? statusCode;

  const SearchFetchResult({
    required this.status,
    this.payload,
    this.statusCode,
  });

  bool get hasPayload => payload != null;
}

final Map<String, Future<SearchFetchResult>> _searchPayloadInflight =
    <String, Future<SearchFetchResult>>{};

Future<T> _runSearchRequest<T>(Future<T> future, Duration timeout) {
  return runSearchRequest(future, timeout);
}

String _searchRequestKey(
  ProviderReader read,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
  String searchMode = '',
  String resultType = '',
  int offset = 0,
  int? revision,
  int? revisionWaitMs,
}) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = read(authProvider).storageScopeId;
  final normalizedSearchMode =
      searchMode.isNotEmpty ? searchMode : classifySearchQueryMode(query);
  return '$storageScopeId|$limit|$normalizedQuery|${deferSideSurfaces ? 'tracks_first' : 'full'}|$normalizedSearchMode|$resultType|$offset|${revision ?? 0}|${revisionWaitMs ?? 0}';
}

Future<SearchFetchResult> fetchSearchPayload(
  ProviderReader read,
  String query, {
  required int limit,
  required Duration timeout,
  bool deferSideSurfaces = false,
  String searchMode = '',
  String resultType = '',
  int offset = 0,
  int? revision,
  int? revisionWaitMs,
}) async {
  final normalizedQuery = query.trim();
  if (normalizedQuery.isEmpty) {
    return const SearchFetchResult(status: 'empty_query');
  }
  final normalizedSearchMode = searchMode.isNotEmpty
      ? searchMode
      : classifySearchQueryMode(normalizedQuery);
  final requestKey = _searchRequestKey(
    read,
    normalizedQuery,
    limit: limit,
    deferSideSurfaces: deferSideSurfaces,
    searchMode: normalizedSearchMode,
    resultType: resultType,
    offset: offset,
    revision: revision,
    revisionWaitMs: revisionWaitMs,
  );
  final inflight = _searchPayloadInflight[requestKey];
  if (inflight != null) {
    return inflight;
  }

  final requestFuture = () async {
    try {
      final body = await buildSemanticSearchRequestBody(
        read,
        normalizedQuery,
        limit: limit,
        deferSideSurfaces: deferSideSurfaces,
        searchMode: normalizedSearchMode,
        resultType: resultType,
        offset: offset,
        revision: revision,
        revisionWaitMs: revisionWaitMs,
      );
      final timedResponse = await _runSearchRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/search'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        timeout,
      );
      if (timedResponse.statusCode != 200) {
        return SearchFetchResult(
          status: 'http_error',
          statusCode: timedResponse.statusCode,
        );
      }
      final payload = jsonDecode(timedResponse.body) as Map<String, dynamic>;
      return SearchFetchResult(
        status: 'network_success',
        payload: payload,
      );
    } on TimeoutException {
      return const SearchFetchResult(status: 'timeout');
    } catch (_) {
      return const SearchFetchResult(status: 'exception');
    } finally {
      _searchPayloadInflight.remove(requestKey);
    }
  }();
  _searchPayloadInflight[requestKey] = requestFuture;
  return requestFuture;
}
