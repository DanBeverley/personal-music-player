import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'interaction_events.dart';
import 'proxy_runtime.dart';
import 'search_payload_runtime.dart';
import 'track_metadata.dart';

class SearchNotifier extends StateNotifier<List<dynamic>> {
  final Ref ref;
  int _requestVersion = 0;
  static const Duration _searchDebounce = Duration(milliseconds: 280);
  static const Duration _searchTimeout = Duration(seconds: 15);

  SearchNotifier(this.ref) : super([]);
  bool isLoading = false;

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      await Future<void>.delayed(_searchDebounce);
      if (requestVersion != _requestVersion) return;
      final fetchResult = await fetchSearchPayload(
        ref.read,
        normalizedQuery,
        limit: 24,
        timeout: _searchTimeout,
        deferSideSurfaces: true,
      );
      if (requestVersion != _requestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final rawResults = (payload['results'] as List<dynamic>? ?? const []);
        state = rawResults
            .whereType<Map>()
            .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
            .where((track) => (extractTrackId(track)?.isNotEmpty ?? false))
            .toList(growable: false);
        debugProxyLog(
          'search',
          'track query="$normalizedQuery" status=200 results=${state.length} similar=${((payload['similar_artists'] as List?) ?? const []).length} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordCloudSearchEvent(
            normalizedQuery,
            resultCount: state.length,
          ),
        );
        unawaited(
          recordProxySearchEvent(
            normalizedQuery,
            resultCount: state.length,
            searchScope: 'track',
          ),
        );
      } else {
        debugProxyLog(
          'search',
          'track query="$normalizedQuery" status=${fetchResult.status}',
        );
        state = [];
      }
    } on TimeoutException catch (error) {
      debugProxyLog('search', 'track query="$normalizedQuery" timeout=$error');
      state = [];
    } catch (error) {
      debugProxyLog('search', 'track query="$normalizedQuery" error=$error');
    } finally {
      final isLatestRequest = requestVersion == _requestVersion;
      if (isLatestRequest) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = [];
  }
}

final searchProvider =
    StateNotifierProvider<SearchNotifier, List<dynamic>>((ref) {
  return SearchNotifier(ref);
});

class SearchPageState {
  final String requestState;
  final String requestId;
  final String modelVersion;
  final String queryIntent;
  final Map<String, dynamic>? topResult;
  final Map<String, dynamic>? leadArtist;
  final Map<String, dynamic>? containingAlbum;
  final List<Map<String, dynamic>> tracks;
  final List<Map<String, dynamic>> artists;
  final List<Map<String, dynamic>> albums;
  final List<Map<String, dynamic>> similarArtists;
  final List<Map<String, dynamic>> similarTracks;
  final List<Map<String, dynamic>> artistTracks;
  final List<Map<String, dynamic>> artistAlbums;
  final List<Map<String, dynamic>> relatedAlbums;
  final List<Map<String, dynamic>> playlists;
  final Map<String, dynamic> pagination;
  final Set<String> loadingSurfaces;
  final Set<String> appendedItemKeys;
  final Map<String, dynamic> diagnostics;
  final String? errorMessage;

  const SearchPageState({
    this.requestState = 'idle',
    this.requestId = '',
    this.modelVersion = '',
    this.queryIntent = 'mixed',
    this.topResult,
    this.leadArtist,
    this.containingAlbum,
    this.tracks = const [],
    this.artists = const [],
    this.albums = const [],
    this.similarArtists = const [],
    this.similarTracks = const [],
    this.artistTracks = const [],
    this.artistAlbums = const [],
    this.relatedAlbums = const [],
    this.playlists = const [],
    this.pagination = const {},
    this.loadingSurfaces = const {},
    this.appendedItemKeys = const {},
    this.diagnostics = const {},
    this.errorMessage,
  });

  bool get hasResults =>
      topResult != null ||
      tracks.isNotEmpty ||
      artists.isNotEmpty ||
      albums.isNotEmpty ||
      similarArtists.isNotEmpty ||
      similarTracks.isNotEmpty ||
      artistTracks.isNotEmpty ||
      artistAlbums.isNotEmpty ||
      relatedAlbums.isNotEmpty ||
      playlists.isNotEmpty;

  factory SearchPageState.fromJson(Map<String, dynamic> json) {
    final rawTopResult = json['top_result'];
    Map<String, dynamic>? normalizedTopResult;
    if (rawTopResult is Map) {
      final topResultMap = Map<String, dynamic>.from(rawTopResult);
      final entityType = topResultMap['entity_type']?.toString() ?? 'track';
      final rawItem = topResultMap['item'];
      if (rawItem is Map) {
        final item = Map<String, dynamic>.from(rawItem);
        topResultMap['item'] =
            entityType == 'track' ? normalizeTrack(item) : item;
        normalizedTopResult = topResultMap;
      }
    }
    final rawTracks = (json['tracks'] as List<dynamic>? ??
        json['results'] as List<dynamic>? ??
        const []);
    final rawArtists = (json['artists'] as List<dynamic>? ?? const []);
    final rawAlbums = (json['albums'] as List<dynamic>? ?? const []);
    final rawSimilarArtists =
        (json['similar_artists'] as List<dynamic>? ?? const []);
    final rawSimilarTracks =
        (json['similar_tracks'] as List<dynamic>? ?? const []);
    final rawArtistTracks =
        (json['artist_tracks'] as List<dynamic>? ?? const []);
    final rawArtistAlbums =
        (json['artist_albums'] as List<dynamic>? ?? const []);
    final rawRelatedAlbums =
        (json['related_albums'] as List<dynamic>? ?? const []);
    final rawPlaylists = (json['playlists'] as List<dynamic>? ?? const []);
    final normalizedTracks = rawTracks
        .whereType<Map>()
        .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .toList(growable: false);
    if (normalizedTopResult == null && normalizedTracks.isNotEmpty) {
      normalizedTopResult = <String, dynamic>{
        'entity_type': 'track',
        'item': normalizedTracks.first,
      };
    }
    return SearchPageState(
      requestState: (json['request_state'] ?? '').toString().trim().isNotEmpty
          ? json['request_state'].toString()
          : ((json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? 'failed'
              : 'complete'),
      requestId: (json['request_id'] ?? '').toString(),
      modelVersion: (json['model_version'] ?? '').toString(),
      queryIntent: (json['query_intent'] ??
              (normalizedTracks.isNotEmpty ? 'track' : 'mixed'))
          .toString(),
      topResult: normalizedTopResult,
      leadArtist: json['lead_artist'] is Map
          ? Map<String, dynamic>.from(json['lead_artist'] as Map)
          : null,
      containingAlbum: json['containing_album'] is Map
          ? Map<String, dynamic>.from(json['containing_album'] as Map)
          : null,
      tracks: normalizedTracks,
      artists: rawArtists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((artist) =>
              (artist['id']?.toString().trim().isNotEmpty ?? false) ||
              (artist['name']?.toString().trim().isNotEmpty ?? false))
          .toList(growable: false),
      albums: rawAlbums
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where(_hasResolvedSearchAlbumArtist)
          .toList(growable: false),
      similarArtists: rawSimilarArtists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((artist) =>
              (artist['id']?.toString().trim().isNotEmpty ?? false) ||
              (artist['name']?.toString().trim().isNotEmpty ?? false))
          .toList(growable: false),
      similarTracks: rawSimilarTracks
          .whereType<Map>()
          .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
          .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
          .toList(growable: false),
      artistTracks: rawArtistTracks
          .whereType<Map>()
          .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
          .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
          .toList(growable: false),
      artistAlbums: rawArtistAlbums
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where(_hasResolvedSearchAlbumArtist)
          .toList(growable: false),
      relatedAlbums: rawRelatedAlbums
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where(_hasResolvedSearchAlbumArtist)
          .toList(growable: false),
      playlists: rawPlaylists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .toList(growable: false),
      pagination: json['pagination'] is Map
          ? Map<String, dynamic>.from(json['pagination'] as Map)
          : const {},
      diagnostics: json['diagnostics'] is Map
          ? Map<String, dynamic>.from(json['diagnostics'] as Map)
          : const {},
      errorMessage:
          (json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? json['error_message'].toString().trim()
              : null,
    );
  }

  SearchPageState copyWith({
    String? requestState,
    String? requestId,
    String? modelVersion,
    String? queryIntent,
    Map<String, dynamic>? topResult,
    Map<String, dynamic>? leadArtist,
    Map<String, dynamic>? containingAlbum,
    bool clearTopResult = false,
    List<Map<String, dynamic>>? tracks,
    List<Map<String, dynamic>>? artists,
    List<Map<String, dynamic>>? albums,
    List<Map<String, dynamic>>? similarArtists,
    List<Map<String, dynamic>>? similarTracks,
    List<Map<String, dynamic>>? artistTracks,
    List<Map<String, dynamic>>? artistAlbums,
    List<Map<String, dynamic>>? relatedAlbums,
    List<Map<String, dynamic>>? playlists,
    Map<String, dynamic>? pagination,
    Set<String>? loadingSurfaces,
    Set<String>? appendedItemKeys,
    Map<String, dynamic>? diagnostics,
    String? errorMessage,
    bool clearError = false,
  }) {
    return SearchPageState(
      requestState: requestState ?? this.requestState,
      requestId: requestId ?? this.requestId,
      modelVersion: modelVersion ?? this.modelVersion,
      queryIntent: queryIntent ?? this.queryIntent,
      topResult: clearTopResult ? null : topResult ?? this.topResult,
      leadArtist: leadArtist ?? this.leadArtist,
      containingAlbum: containingAlbum ?? this.containingAlbum,
      tracks: tracks ?? this.tracks,
      artists: artists ?? this.artists,
      albums: albums ?? this.albums,
      similarArtists: similarArtists ?? this.similarArtists,
      similarTracks: similarTracks ?? this.similarTracks,
      artistTracks: artistTracks ?? this.artistTracks,
      artistAlbums: artistAlbums ?? this.artistAlbums,
      relatedAlbums: relatedAlbums ?? this.relatedAlbums,
      playlists: playlists ?? this.playlists,
      pagination: pagination ?? this.pagination,
      loadingSurfaces: loadingSurfaces ?? this.loadingSurfaces,
      appendedItemKeys: appendedItemKeys ?? this.appendedItemKeys,
      diagnostics: diagnostics ?? this.diagnostics,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
    );
  }
}

bool _hasResolvedSearchAlbumArtist(Map<String, dynamic> album) {
  final title = album['title']?.toString().trim() ?? '';
  final artist = album['artist']?.toString().trim().toLowerCase() ?? '';
  return title.isNotEmpty &&
      artist.isNotEmpty &&
      artist != 'unknown' &&
      artist != 'unknown artist';
}

String _searchArtistKey(Map<String, dynamic> artist) {
  final canonicalId =
      (artist['canonical_artist_id'] ?? artist['canonical_artist_key'] ?? '')
          .toString()
          .trim()
          .toLowerCase();
  final musicBrainzId = (artist['musicbrainz_artist_id'] ??
          artist['artist_mbid'] ??
          artist['mb_artist_id'] ??
          '')
      .toString()
      .trim()
      .toLowerCase();
  if (musicBrainzId.isNotEmpty) return 'mbid:$musicBrainzId';
  if (canonicalId.startsWith('musicbrainz:artist:')) {
    return 'mbid:${canonicalId.substring('musicbrainz:artist:'.length)}';
  }
  final providerId = (artist['provider_artist_id'] ??
          artist['browseId'] ??
          artist['artist_id'] ??
          artist['id'] ??
          '')
      .toString()
      .trim()
      .toLowerCase();
  if (providerId.isNotEmpty &&
      !providerId.startsWith('musicbrainz:artist:') &&
      !providerId.startsWith('artist-name:') &&
      !providerId.startsWith('derived:')) {
    return 'provider:$providerId';
  }
  final name = (artist['normalized_name'] ??
          artist['name'] ??
          artist['artist'] ??
          artist['channel'] ??
          '')
      .toString()
      .trim()
      .toLowerCase();
  return name.isNotEmpty ? 'name:$name' : canonicalId;
}

bool _sameSearchArtist(
  Map<String, dynamic> left,
  Map<String, dynamic> right,
) {
  Set<String> stableTokens(Map<String, dynamic> artist) {
    final key = _searchArtistKey(artist);
    final tokens = <String>{};
    if (key.startsWith('mbid:') || key.startsWith('provider:')) {
      tokens.add(key);
    }
    final providerId = (artist['provider_artist_id'] ??
            artist['browseId'] ??
            artist['artist_id'] ??
            artist['id'] ??
            '')
        .toString()
        .trim()
        .toLowerCase();
    if (providerId.isNotEmpty &&
        !providerId.startsWith('musicbrainz:artist:') &&
        !providerId.startsWith('artist-name:') &&
        !providerId.startsWith('derived:')) {
      tokens.add('provider:$providerId');
    }
    return tokens;
  }

  final leftTokens = stableTokens(left);
  final rightTokens = stableTokens(right);
  if (leftTokens.isNotEmpty && rightTokens.isNotEmpty) {
    return leftTokens.intersection(rightTokens).isNotEmpty;
  }
  final leftName = (left['normalized_name'] ?? left['name'] ?? '')
      .toString()
      .trim()
      .toLowerCase();
  final rightName = (right['normalized_name'] ?? right['name'] ?? '')
      .toString()
      .trim()
      .toLowerCase();
  return leftName.isNotEmpty &&
      leftName == rightName &&
      (leftTokens.isEmpty || rightTokens.isEmpty);
}

class SearchPageNotifier extends StateNotifier<SearchPageState> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;
  String _currentQuery = '';
  static const Duration _searchDebounce = Duration.zero;
  static const Duration _searchTimeout = Duration(seconds: 15);
  static const List<Duration> _progressiveRefreshDelays = <Duration>[
    Duration(milliseconds: 700),
    Duration(milliseconds: 1100),
    Duration(milliseconds: 1700),
    Duration(milliseconds: 2500),
    Duration(milliseconds: 3500),
  ];

  SearchPageNotifier(this.ref) : super(const SearchPageState());

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    _currentQuery = normalizedQuery;
    isLoading = true;
    state = state.copyWith(requestState: 'loading', clearError: true);
    try {
      if (_searchDebounce > Duration.zero) {
        await Future<void>.delayed(_searchDebounce);
        if (requestVersion != _requestVersion) return;
      }
      if (requestVersion != _requestVersion) return;
      final fetchResult = await fetchSearchPayload(
        ref.read,
        normalizedQuery,
        limit: 16,
        timeout: _searchTimeout,
        deferSideSurfaces: false,
      );
      if (requestVersion != _requestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final nextState = SearchPageState.fromJson(payload);
        state = nextState.copyWith(
          requestState: 'complete',
          clearError: true,
          diagnostics: nextState.diagnostics,
        );
        debugProxyLog(
          'search',
          'page query="$normalizedQuery" status=200 tracks=${nextState.tracks.length} artists=${nextState.artists.length} albums=${nextState.albums.length} similar=${nextState.similarArtists.length} diagnostics=${compactDiagnosticValue(nextState.diagnostics)}',
        );
        unawaited(
          recordCloudSearchEvent(
            normalizedQuery,
            resultCount: nextState.tracks.length,
          ),
        );
        unawaited(
          recordProxySearchEvent(
            normalizedQuery,
            resultCount: nextState.tracks.length,
            searchScope: 'search_page',
          ),
        );
        if (_artistSurfaceNeedsRefresh()) {
          unawaited(
            _refreshPendingArtistSurface(
              query: normalizedQuery,
              requestVersion: requestVersion,
            ),
          );
        }
      } else {
        final errorMessage = fetchResult.status == 'timeout'
            ? searchTimeoutMessage
            : searchUnavailableMessage;
        debugProxyLog(
          'search',
          'page query="$normalizedQuery" status=${fetchResult.status}',
        );
        state = SearchPageState(
          requestState: 'failed',
          errorMessage: errorMessage,
          diagnostics: <String, dynamic>{
            'fetch_status': fetchResult.status,
            if (fetchResult.statusCode != null)
              'http_status': fetchResult.statusCode,
          },
        );
      }
    } on TimeoutException catch (error) {
      debugProxyLog('search', 'page query="$normalizedQuery" timeout=$error');
      if (requestVersion != _requestVersion) return;
      state = const SearchPageState(
        requestState: 'failed',
        errorMessage: searchTimeoutMessage,
      );
    } catch (error) {
      debugProxyLog('search', 'page query="$normalizedQuery" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const SearchPageState(
        requestState: 'failed',
        errorMessage: searchUnavailableMessage,
      );
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = state.copyWith();
      }
    }
  }

  bool _artistSurfaceNeedsRefresh() {
    final pendingSurfaces = state.diagnostics['search_pending_surfaces'];
    if (pendingSurfaces is List &&
        pendingSurfaces.any((surface) => surface.toString() == 'artists')) {
      return true;
    }
    final artworkPending =
        (state.diagnostics['artist_artwork_pending'] as num?)?.toInt() ?? 0;
    if (artworkPending > 0) return true;
    final artistsPage = state.pagination['artists'];
    return artistsPage is Map && artistsPage['deferred_expansion'] == true;
  }

  Future<void> _refreshPendingArtistSurface({
    required String query,
    required int requestVersion,
  }) async {
    for (final delay in _progressiveRefreshDelays) {
      if (requestVersion != _requestVersion ||
          query != _currentQuery ||
          !_artistSurfaceNeedsRefresh()) {
        return;
      }
      await Future<void>.delayed(delay);
      if (requestVersion != _requestVersion || query != _currentQuery) return;
      await loadMore(
        'artists',
        expectedQuery: query,
        expectedRequestVersion: requestVersion,
        refreshVisiblePage: true,
      );
    }
  }

  Future<void> loadMore(
    String surface, {
    String? expectedQuery,
    int? expectedRequestVersion,
    bool refreshVisiblePage = false,
  }) async {
    final normalizedSurface = surface.trim().toLowerCase();
    if ((expectedQuery != null && expectedQuery != _currentQuery) ||
        (expectedRequestVersion != null &&
            expectedRequestVersion != _requestVersion)) {
      return;
    }
    final submittedQuery = _currentQuery;
    final submittedRequestVersion = _requestVersion;
    if (_currentQuery.isEmpty ||
        !const {'tracks', 'artists', 'albums', 'playlists'}
            .contains(normalizedSurface) ||
        state.loadingSurfaces.contains(normalizedSurface)) {
      return;
    }
    final page = state.pagination[normalizedSurface];
    final pageMap = page is Map ? Map<String, dynamic>.from(page) : const {};
    final surfaceIsEmpty = switch (normalizedSurface) {
      'tracks' => state.tracks.isEmpty,
      'artists' => state.artists.isEmpty,
      'albums' => state.albums.isEmpty,
      'playlists' => state.playlists.isEmpty,
      _ => false,
    };
    final hasMore =
        surfaceIsEmpty || pageMap.isEmpty || pageMap['has_more'] == true;
    if (!hasMore && !refreshVisiblePage) return;
    final offset = refreshVisiblePage || surfaceIsEmpty
        ? 0
        : (pageMap['next_offset'] as num?)?.toInt() ?? 0;
    state = state.copyWith(
      loadingSurfaces: {...state.loadingSurfaces, normalizedSurface},
      appendedItemKeys: const {},
    );
    try {
      final result = await fetchSearchPayload(
        ref.read,
        submittedQuery,
        limit: 16,
        timeout: _searchTimeout,
        deferSideSurfaces: false,
        resultType: normalizedSurface,
        offset: offset,
      );
      if (!result.hasPayload ||
          submittedQuery != _currentQuery ||
          submittedRequestVersion != _requestVersion) {
        return;
      }
      final next = SearchPageState.fromJson(result.payload!);
      final appendedKeys = <String>{};
      bool hasUsefulValue(dynamic value) {
        if (value == null) return false;
        if (value is String) return value.trim().isNotEmpty;
        if (value is Iterable) return value.isNotEmpty;
        if (value is Map) return value.isNotEmpty;
        return true;
      }

      List<Map<String, dynamic>> appendUnique(
        List<Map<String, dynamic>> current,
        List<Map<String, dynamic>> incoming,
        String Function(Map<String, dynamic>) keyOf,
      ) {
        final output = <Map<String, dynamic>>[...current];
        final positions = <String, int>{};
        for (final entry in current.indexed) {
          final key = keyOf(entry.$2);
          if (key.isNotEmpty) positions[key] = entry.$1;
        }
        for (final item in incoming) {
          final key = keyOf(item);
          if (key.isEmpty) continue;
          final existingIndex = positions[key];
          if (existingIndex != null) {
            output[existingIndex] = <String, dynamic>{
              ...output[existingIndex],
              ...Map<String, dynamic>.fromEntries(
                item.entries.where(
                  (entry) => hasUsefulValue(entry.value),
                ),
              ),
            };
            continue;
          }
          positions[key] = output.length;
          output.add(item);
          appendedKeys.add('$normalizedSurface:$key');
        }
        return output;
      }

      List<Map<String, dynamic>> appendArtists(
        List<Map<String, dynamic>> current,
        List<Map<String, dynamic>> incoming,
      ) {
        final output = <Map<String, dynamic>>[...current];
        for (final item in incoming) {
          final existingIndex = output
              .indexWhere((existing) => _sameSearchArtist(existing, item));
          if (existingIndex >= 0) {
            output[existingIndex] = <String, dynamic>{
              ...output[existingIndex],
              ...Map<String, dynamic>.fromEntries(
                item.entries.where((entry) => hasUsefulValue(entry.value)),
              ),
            };
            continue;
          }
          output.add(item);
          appendedKeys.add(
            '$normalizedSurface:${_searchArtistKey(item)}',
          );
        }
        return output;
      }

      final mergedArtists = normalizedSurface == 'artists'
          ? appendArtists(state.artists, next.artists)
          : state.artists;
      Map<String, dynamic>? refreshedTopResult = state.topResult;
      final incomingTopResult = next.topResult;
      final currentTopItem = state.topResult?['item'];
      if (normalizedSurface == 'artists' &&
          incomingTopResult?['entity_type']?.toString() == 'artist') {
        refreshedTopResult = incomingTopResult;
      } else if (normalizedSurface == 'artists' &&
          state.topResult?['entity_type']?.toString() == 'artist' &&
          currentTopItem is Map) {
        final currentTop = Map<String, dynamic>.from(currentTopItem);
        final richerTop =
            mergedArtists.cast<Map<String, dynamic>?>().firstWhere(
                  (item) => item != null && _sameSearchArtist(item, currentTop),
                  orElse: () => null,
                );
        if (richerTop != null) {
          refreshedTopResult = <String, dynamic>{
            ...state.topResult!,
            'item': <String, dynamic>{...currentTop, ...richerTop},
          };
        }
      }
      Map<String, dynamic>? refreshedLeadArtist = state.leadArtist;
      if (normalizedSurface == 'artists') {
        final currentLead = state.leadArtist;
        final incomingLead = next.leadArtist;
        if (currentLead == null) {
          refreshedLeadArtist = incomingLead;
        } else if (incomingLead != null &&
            _sameSearchArtist(currentLead, incomingLead)) {
          refreshedLeadArtist = <String, dynamic>{
            ...currentLead,
            ...incomingLead,
          };
        }
      }
      final nextPagination = <String, dynamic>{
        ...state.pagination,
        if (next.pagination[normalizedSurface] != null)
          normalizedSurface: next.pagination[normalizedSurface],
      };
      final previousSnapshotRevision =
          (state.diagnostics['search_snapshot_revision'] as num?)?.toInt() ??
              0;
      state = state.copyWith(
        tracks: normalizedSurface == 'tracks'
            ? appendUnique(
                state.tracks, next.tracks, (item) => extractTrackId(item) ?? '')
            : state.tracks,
        artists: mergedArtists,
        albums: normalizedSurface == 'albums'
            ? appendUnique(
                state.albums,
                next.albums,
                (item) => (item['id'] ?? '${item['title']}|${item['artist']}')
                    .toString())
            : state.albums,
        playlists: normalizedSurface == 'playlists'
            ? appendUnique(state.playlists, next.playlists,
                (item) => (item['id'] ?? item['name'] ?? '').toString())
            : state.playlists,
        similarArtists: normalizedSurface == 'artists'
            ? appendArtists(state.similarArtists, next.similarArtists)
            : state.similarArtists,
        artistTracks: normalizedSurface == 'artists'
            ? appendUnique(
                state.artistTracks,
                next.artistTracks,
                (item) => extractTrackId(item) ?? '',
              )
            : state.artistTracks,
        artistAlbums: normalizedSurface == 'artists'
            ? appendUnique(
                state.artistAlbums,
                next.artistAlbums,
                (item) => (item['canonical_album_identity'] ??
                        item['id'] ??
                        '${item['title']}|${item['artist']}')
                    .toString()
                    .toLowerCase(),
              )
            : state.artistAlbums,
        relatedAlbums:
            normalizedSurface == 'artists' || normalizedSurface == 'albums'
                ? appendUnique(
                    state.relatedAlbums,
                    next.relatedAlbums,
                    (item) => (item['canonical_album_identity'] ??
                            item['id'] ??
                            '${item['title']}|${item['artist']}')
                        .toString()
                        .toLowerCase(),
                  )
                : state.relatedAlbums,
        topResult: refreshedTopResult,
        leadArtist: refreshedLeadArtist,
        containingAlbum: state.containingAlbum ?? next.containingAlbum,
        pagination: nextPagination,
        appendedItemKeys: appendedKeys,
        diagnostics: <String, dynamic>{
          ...state.diagnostics,
          ...next.diagnostics,
        },
      );
      if (normalizedSurface == 'artists') {
        final snapshotRevision =
            (state.diagnostics['search_snapshot_revision'] as num?)?.toInt() ??
                0;
        if (snapshotRevision > previousSnapshotRevision) {
          final primaryTrackIds = state.tracks
              .map(extractTrackId)
              .whereType<String>()
              .where((id) => id.isNotEmpty)
              .toSet();
          final visibleMoreCount = state.artistTracks
              .where(
                (track) =>
                    !primaryTrackIds.contains(extractTrackId(track)),
              )
              .length;
          debugProxyLog(
            'search',
            'progressive refresh query="$submittedQuery" snapshot_revision=$snapshotRevision visible_related=${state.similarArtists.length} artist_tracks=${state.artistTracks.length} visible_more=$visibleMoreCount artist_albums=${state.artistAlbums.length} query_albums=${state.albums.length} playlists=${state.playlists.length}',
          );
        }
      }
    } finally {
      state = state.copyWith(
        loadingSurfaces: state.loadingSurfaces.difference({normalizedSurface}),
      );
    }
  }

  Future<void> refreshVisibleArtistMetadata() async {
    if (_currentQuery.isEmpty) return;
    await loadMore('artists', refreshVisiblePage: true);
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    _currentQuery = '';
    state = const SearchPageState(requestState: 'idle');
  }
}

final searchPageProvider =
    StateNotifierProvider<SearchPageNotifier, SearchPageState>((ref) {
  return SearchPageNotifier(ref);
});
