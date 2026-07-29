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
final Map<String, Map<String, dynamic>> _searchSelectionOverrides =
    <String, Map<String, dynamic>>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadForegroundInflight =
    <String, Future<SearchFetchResult>>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadBackgroundInflight =
    <String, Future<SearchFetchResult>>{};
const Duration _searchCacheFreshTtl = Duration(minutes: 4);
const Duration _searchCacheMaxTtl = Duration(minutes: 12);

void invalidateSearchPayloadCache() {
  _searchPayloadCache.clear();
}

Future<T> _runSearchRequest<T>(Future<T> future, Duration timeout) {
  return runSearchRequest(future, timeout);
}

String _searchPayloadCacheKey(
  ProviderReader read,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
  String searchMode = '',
  String resultType = '',
  int offset = 0,
}) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = read(authProvider).storageScopeId;
  final normalizedSearchMode =
      searchMode.isNotEmpty ? searchMode : classifySearchQueryMode(query);
  return '$storageScopeId|$limit|$normalizedQuery|${deferSideSurfaces ? 'tracks_first' : 'full'}|$normalizedSearchMode|$resultType|$offset';
}

String _searchSelectionKey(ProviderReader read, String query) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = read(authProvider).storageScopeId;
  return '$storageScopeId|$normalizedQuery';
}

void rememberSearchSelectionOverride(
  ProviderReader read,
  String query, {
  required String entityType,
  required Map<String, dynamic> item,
}) {
  final normalizedQuery = query.trim();
  if (normalizedQuery.isEmpty || item.isEmpty) return;
  final key = _searchSelectionKey(read, normalizedQuery);
  _searchSelectionOverrides[key] = <String, dynamic>{
    'entity_type': entityType,
    'item': entityType == 'track'
        ? normalizeTrack(Map<String, dynamic>.from(item))
        : Map<String, dynamic>.from(item),
    'selected_at': DateTime.now().millisecondsSinceEpoch,
  };
  if (_searchSelectionOverrides.length > 48) {
    final entries = _searchSelectionOverrides.entries.toList()
      ..sort((a, b) {
        final aAt = (a.value['selected_at'] as num?)?.toInt() ?? 0;
        final bAt = (b.value['selected_at'] as num?)?.toInt() ?? 0;
        return aAt.compareTo(bAt);
      });
    for (final entry in entries.take(_searchSelectionOverrides.length - 36)) {
      _searchSelectionOverrides.remove(entry.key);
    }
  }
}

List<Map<String, dynamic>> _promoteSelectedTrack(
  List<dynamic>? values,
  Map<String, dynamic> selected,
) {
  final selectedId = extractTrackId(selected);
  final selectedTitle = selected['title']?.toString().trim().toLowerCase();
  final selectedArtist =
      (selected['artist'] ?? selected['channel'] ?? selected['author'])
          ?.toString()
          .trim()
          .toLowerCase();
  final promoted = <Map<String, dynamic>>[
    normalizeTrack(Map<String, dynamic>.from(selected)),
  ];
  for (final value in values ?? const []) {
    if (value is! Map) continue;
    final track = normalizeTrack(Map<String, dynamic>.from(value));
    final trackId = extractTrackId(track);
    final title = track['title']?.toString().trim().toLowerCase();
    final artist = (track['artist'] ?? track['channel'] ?? track['author'])
        ?.toString()
        .trim()
        .toLowerCase();
    final sameId =
        selectedId != null && selectedId.isNotEmpty && selectedId == trackId;
    final sameEntity = selectedTitle != null &&
        selectedTitle.isNotEmpty &&
        selectedTitle == title &&
        selectedArtist != null &&
        selectedArtist.isNotEmpty &&
        selectedArtist == artist;
    if (!sameId && !sameEntity) promoted.add(track);
  }
  return promoted;
}

Map<String, dynamic> _applySearchSelectionOverride(
  ProviderReader read,
  String query,
  Map<String, dynamic> payload,
) {
  final override = _searchSelectionOverrides[_searchSelectionKey(read, query)];
  if (override == null) return Map<String, dynamic>.from(payload);
  final entityType = override['entity_type']?.toString();
  final item = override['item'];
  if (item is! Map) return Map<String, dynamic>.from(payload);
  final result = Map<String, dynamic>.from(payload);
  final selected = Map<String, dynamic>.from(item);
  if (entityType == 'track') {
    final promotedTracks =
        _promoteSelectedTrack(result['tracks'] as List?, selected);
    result['tracks'] = promotedTracks;
    result['results'] =
        _promoteSelectedTrack(result['results'] as List?, selected);
    result['top_result'] = <String, dynamic>{
      'entity_type': 'track',
      'item': promotedTracks.first,
      'source': 'local_selection_override',
    };
  } else {
    result['top_result'] = <String, dynamic>{
      'entity_type': entityType ?? 'unknown',
      'item': selected,
      'source': 'local_selection_override',
    };
  }
  final diagnostics = Map<String, dynamic>.from(
    (result['diagnostics'] as Map?) ?? const <String, dynamic>{},
  );
  diagnostics['local_selection_override'] = true;
  result['diagnostics'] = diagnostics;
  return result;
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
  String resultType = '',
  int offset = 0,
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
    resultType: resultType,
    offset: offset,
  );
  final freshCached = _lookupSearchPayloadCache(cacheKey);
  if (preferCache && freshCached != null && !backgroundRefresh) {
    return SearchFetchResult(
      status: 'cache_hit',
      payload: _applySearchSelectionOverride(
        read,
        normalizedQuery,
        freshCached.payload,
      ),
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
          resultType: resultType,
          offset: offset,
        ),
      );
      return SearchFetchResult(
        status: 'stale_cache',
        payload: _applySearchSelectionOverride(
          read,
          normalizedQuery,
          staleCached.payload,
        ),
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
        resultType: resultType,
        offset: offset,
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
              payload: _applySearchSelectionOverride(
                read,
                normalizedQuery,
                staleCached.payload,
              ),
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
      return SearchFetchResult(
        status: 'network_success',
        payload: _applySearchSelectionOverride(read, normalizedQuery, payload),
      );
    } on TimeoutException {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return SearchFetchResult(
            status: 'stale_cache',
            payload: _applySearchSelectionOverride(
              read,
              normalizedQuery,
              staleCached.payload,
            ),
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
            payload: _applySearchSelectionOverride(
              read,
              normalizedQuery,
              staleCached.payload,
            ),
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
