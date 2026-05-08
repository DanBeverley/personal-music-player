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
}) async {
  final normalizedQuery = query.trim();
  final normalizedSearchMode = searchMode.isNotEmpty
      ? searchMode
      : classifySearchQueryMode(normalizedQuery);
  final playlists = read(playlistProvider);
  final libraryTracks = read(libraryProvider).valueOrNull ?? const [];
  final storageScopeId = read(authProvider).storageScopeId;
  final useLightweightContext = normalizedSearchMode == 'exact';
  final useLocalRecentQueriesOnly = normalizedSearchMode != 'taste';
  final searchSignals = await Future.wait<Object?>([
    HistoryManager.getRecentTrackSnapshots(
        limit: useLightweightContext ? 4 : 12),
    HistoryManager.getLastPlayedTrackSnapshots(
        limit: useLightweightContext ? 4 : 8),
    HistoryManager.getFrequentlyPlayedTrackSnapshots(
        limit: useLightweightContext ? 4 : 10),
    HistoryManager.getRecentSeeds(limit: useLightweightContext ? 4 : 12),
    HistoryManager.getFrequentlyPlayedTrackIds(
        limit: useLightweightContext ? 4 : 10),
    useLocalRecentQueriesOnly
        ? Future<List<String>>.value(peekRecentCloudSearchQueries(limit: 6))
        : getRecentCloudSearchQueries(limit: 8),
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
    artistWeight: useLightweightContext ? 0.65 : 1.45,
    albumWeight: useLightweightContext ? 0.35 : 1.0,
  );
  addTrackHints(
    recentTrackSnapshots,
    artistWeight: useLightweightContext ? 0.55 : 1.1,
    albumWeight: useLightweightContext ? 0.28 : 0.82,
  );
  if (!useLightweightContext) {
    addTrackHints(
      topTrackSnapshots,
      artistWeight: 1.7,
      albumWeight: 1.18,
    );
  }

  if (!useLightweightContext) {
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
  }

  for (final track in libraryTracks.take(useLightweightContext ? 8 : 24)) {
    final normalizedTrack = normalizeTrack(track);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId != null && trackId.isNotEmpty) {
      libraryTrackIds.add(trackId);
      if (normalizedTrack['is_downloaded_locally'] == true) {
        offlineTrackIds.add(trackId);
      }
    }
    if (!useLightweightContext) {
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
  }

  bumpSearchSignalWeight(queryWeights, normalizedQuery, 2.45);
  if (normalizedSearchMode == 'taste') {
    bumpSearchSignalWeight(queryWeights, normalizedQuery, 1.25);
  }

  if (!useLightweightContext) {
    for (final queryValue in blendedRecentQueries) {
      if (looksLikeSemanticTasteQuery(queryValue)) {
        bumpSearchSignalWeight(queryWeights, queryValue, 1.18);
      }
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

class _CachedSearchPayload {
  final Map<String, dynamic> payload;
  final DateTime storedAt;

  const _CachedSearchPayload({
    required this.payload,
    required this.storedAt,
  });
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

final Map<String, _CachedSearchPayload> _searchPayloadCache =
    <String, _CachedSearchPayload>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadForegroundInflight =
    <String, Future<SearchFetchResult>>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadBackgroundInflight =
    <String, Future<SearchFetchResult>>{};
const Duration _searchCacheFreshTtl = Duration(minutes: 4);
const Duration _searchCacheMaxTtl = Duration(minutes: 12);

Future<T> _runSearchRequest<T>(Future<T> future, Duration timeout) {
  return runSearchRequest(future, timeout);
}

String _searchPayloadCacheKey(
  ProviderReader read,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
  String searchMode = '',
}) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = read(authProvider).storageScopeId;
  final normalizedSearchMode =
      searchMode.isNotEmpty ? searchMode : classifySearchQueryMode(query);
  return '$storageScopeId|$limit|$normalizedQuery|${deferSideSurfaces ? 'tracks_first' : 'full'}|$normalizedSearchMode';
}

void _storeSearchPayloadCache(
  String cacheKey,
  Map<String, dynamic> payload,
) {
  _searchPayloadCache[cacheKey] = _CachedSearchPayload(
    payload: Map<String, dynamic>.from(payload),
    storedAt: DateTime.now(),
  );
  if (_searchPayloadCache.length > 36) {
    final oldestEntries = _searchPayloadCache.entries.toList()
      ..sort((a, b) => a.value.storedAt.compareTo(b.value.storedAt));
    for (final entry in oldestEntries.take(_searchPayloadCache.length - 24)) {
      _searchPayloadCache.remove(entry.key);
    }
  }
}

_CachedSearchPayload? _lookupSearchPayloadCache(
  String cacheKey, {
  bool allowStale = false,
}) {
  final cached = _searchPayloadCache[cacheKey];
  if (cached == null) return null;
  final age = DateTime.now().difference(cached.storedAt);
  final ttl = allowStale ? _searchCacheMaxTtl : _searchCacheFreshTtl;
  if (age > ttl) {
    if (allowStale) {
      _searchPayloadCache.remove(cacheKey);
    }
    return null;
  }
  return cached;
}

Future<SearchFetchResult> fetchSearchPayload(
  ProviderReader read,
  String query, {
  required int limit,
  required Duration timeout,
  bool preferCache = true,
  bool backgroundRefresh = false,
  bool deferSideSurfaces = false,
  String searchMode = '',
}) async {
  final normalizedQuery = query.trim();
  if (normalizedQuery.isEmpty) {
    return const SearchFetchResult(status: 'empty_query');
  }
  final normalizedSearchMode = searchMode.isNotEmpty
      ? searchMode
      : classifySearchQueryMode(normalizedQuery);
  final cacheKey = _searchPayloadCacheKey(
    read,
    normalizedQuery,
    limit: limit,
    deferSideSurfaces: deferSideSurfaces,
    searchMode: normalizedSearchMode,
  );
  final freshCached = _lookupSearchPayloadCache(cacheKey);
  if (preferCache && freshCached != null && !backgroundRefresh) {
    return SearchFetchResult(
      status: 'cache_hit',
      payload: Map<String, dynamic>.from(freshCached.payload),
    );
  }
  if (!backgroundRefresh) {
    final staleCached = _lookupSearchPayloadCache(cacheKey, allowStale: true);
    if (staleCached != null) {
      unawaited(
        fetchSearchPayload(
          read,
          normalizedQuery,
          limit: limit,
          timeout: timeout,
          preferCache: false,
          backgroundRefresh: true,
          deferSideSurfaces: deferSideSurfaces,
          searchMode: normalizedSearchMode,
        ),
      );
      return SearchFetchResult(
        status: 'stale_cache',
        payload: Map<String, dynamic>.from(staleCached.payload),
      );
    }
  }
  final inflightMap = backgroundRefresh
      ? _searchPayloadBackgroundInflight
      : _searchPayloadForegroundInflight;
  final inflight = inflightMap[cacheKey];
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
        if (!backgroundRefresh) {
          final staleCached =
              _lookupSearchPayloadCache(cacheKey, allowStale: true);
          if (staleCached != null) {
            return SearchFetchResult(
              status: 'stale_cache',
              payload: Map<String, dynamic>.from(staleCached.payload),
              statusCode: timedResponse.statusCode,
            );
          }
        }
        return SearchFetchResult(
          status: 'http_error',
          statusCode: timedResponse.statusCode,
        );
      }
      final payload = jsonDecode(timedResponse.body) as Map<String, dynamic>;
      _storeSearchPayloadCache(cacheKey, payload);
      return SearchFetchResult(status: 'network_success', payload: payload);
    } on TimeoutException {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const SearchFetchResult(status: 'timeout');
    } catch (_) {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const SearchFetchResult(status: 'exception');
    } finally {
      inflightMap.remove(cacheKey);
    }
  }();
  inflightMap[cacheKey] = requestFuture;
  return requestFuture;
}
