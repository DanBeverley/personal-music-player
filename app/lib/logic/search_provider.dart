import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart' show audioPlayerProvider;
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

  void _primeSearchResults(List<dynamic> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

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
      if (requestVersion != _requestVersion) return;
      final fetchResult = await fetchSearchPayload(
        ref.read,
        normalizedQuery,
        limit: 16,
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
        _primeSearchResults(state);
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
        notifyRecommendationSignal(normalizedQuery);
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
  final List<Map<String, dynamic>> tracks;
  final List<Map<String, dynamic>> artists;
  final List<Map<String, dynamic>> albums;
  final List<Map<String, dynamic>> similarArtists;
  final List<Map<String, dynamic>> similarTracks;
  final List<Map<String, dynamic>> artistTracks;
  final List<Map<String, dynamic>> relatedAlbums;
  final Map<String, dynamic> diagnostics;
  final String? errorMessage;

  const SearchPageState({
    this.requestState = 'idle',
    this.requestId = '',
    this.modelVersion = '',
    this.queryIntent = 'mixed',
    this.topResult,
    this.tracks = const [],
    this.artists = const [],
    this.albums = const [],
    this.similarArtists = const [],
    this.similarTracks = const [],
    this.artistTracks = const [],
    this.relatedAlbums = const [],
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
      relatedAlbums.isNotEmpty;

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
    final rawRelatedAlbums =
        (json['related_albums'] as List<dynamic>? ?? const []);
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
      relatedAlbums: rawRelatedAlbums
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where(_hasResolvedSearchAlbumArtist)
          .toList(growable: false),
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
    bool clearTopResult = false,
    List<Map<String, dynamic>>? tracks,
    List<Map<String, dynamic>>? artists,
    List<Map<String, dynamic>>? albums,
    List<Map<String, dynamic>>? similarArtists,
    List<Map<String, dynamic>>? similarTracks,
    List<Map<String, dynamic>>? artistTracks,
    List<Map<String, dynamic>>? relatedAlbums,
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
      tracks: tracks ?? this.tracks,
      artists: artists ?? this.artists,
      albums: albums ?? this.albums,
      similarArtists: similarArtists ?? this.similarArtists,
      similarTracks: similarTracks ?? this.similarTracks,
      artistTracks: artistTracks ?? this.artistTracks,
      relatedAlbums: relatedAlbums ?? this.relatedAlbums,
      diagnostics: diagnostics ?? this.diagnostics,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
    );
  }
}

List<Map<String, dynamic>> _mergeSearchEntities(
  List<Map<String, dynamic>> current,
  List<Map<String, dynamic>> incoming, {
  required String Function(Map<String, dynamic>) identity,
}) {
  final merged = <Map<String, dynamic>>[];
  final indexes = <String, int>{};
  for (final item in [...current, ...incoming]) {
    final key = identity(item);
    if (key.isEmpty) continue;
    final existingIndex = indexes[key];
    if (existingIndex == null) {
      indexes[key] = merged.length;
      merged.add(item);
      continue;
    }
    merged[existingIndex] = <String, dynamic>{
      ...merged[existingIndex],
      ...item,
    };
  }
  return merged;
}

String _trackSearchIdentity(Map<String, dynamic> track) =>
    extractTrackId(track)?.trim() ?? '';

String _artistSearchIdentity(Map<String, dynamic> artist) {
  final id = artist['id']?.toString().trim() ?? '';
  if (id.isNotEmpty) return id;
  return artist['name']?.toString().trim().toLowerCase() ?? '';
}

String _albumSearchIdentity(Map<String, dynamic> album) {
  final id = album['id']?.toString().trim() ?? '';
  if (id.isNotEmpty) return id;
  final title = album['title']?.toString().trim().toLowerCase() ?? '';
  final artist = album['artist']?.toString().trim().toLowerCase() ?? '';
  return title.isEmpty ? '' : '$title|$artist';
}

bool _hasResolvedSearchAlbumArtist(Map<String, dynamic> album) {
  final title = album['title']?.toString().trim() ?? '';
  final artist = album['artist']?.toString().trim().toLowerCase() ?? '';
  return title.isNotEmpty &&
      artist.isNotEmpty &&
      artist != 'unknown' &&
      artist != 'unknown artist';
}

class SearchPageNotifier extends StateNotifier<SearchPageState> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;
  static const Duration _searchDebounce = Duration.zero;
  static const Duration _searchTimeout = Duration(seconds: 15);
  static const Duration _searchEnrichmentTimeout = Duration(seconds: 8);

  SearchPageNotifier(this.ref) : super(const SearchPageState());

  Future<void> _enrichSearchPageResults(
    String normalizedQuery, {
    required int requestVersion,
  }) async {
    try {
      final fetchResult = await fetchSearchPayload(
        ref.read,
        normalizedQuery,
        limit: 16,
        timeout: _searchEnrichmentTimeout,
        preferCache: true,
        deferSideSurfaces: false,
      );
      if (requestVersion != _requestVersion || !fetchResult.hasPayload) return;
      final nextState = SearchPageState.fromJson(fetchResult.payload!);
      final currentTracks = state.tracks;
      final nextTracks = nextState.tracks;
      if (currentTracks.isNotEmpty &&
          nextTracks.isNotEmpty &&
          _trackSearchIdentity(currentTracks.first) !=
              _trackSearchIdentity(nextTracks.first)) {
        return;
      }
      state = state.copyWith(
        requestId: nextState.requestId,
        modelVersion: nextState.modelVersion,
        queryIntent: nextState.queryIntent,
        topResult: nextState.topResult,
        tracks: _mergeSearchEntities(
          state.tracks,
          nextState.tracks,
          identity: _trackSearchIdentity,
        ),
        artists: _mergeSearchEntities(
          state.artists,
          nextState.artists,
          identity: _artistSearchIdentity,
        ),
        albums: _mergeSearchEntities(
          state.albums,
          nextState.albums,
          identity: _albumSearchIdentity,
        ),
        similarArtists: _mergeSearchEntities(
          state.similarArtists,
          nextState.similarArtists,
          identity: _artistSearchIdentity,
        ),
        similarTracks: _mergeSearchEntities(
          state.similarTracks,
          nextState.similarTracks,
          identity: _trackSearchIdentity,
        ),
        artistTracks: _mergeSearchEntities(
          state.artistTracks,
          nextState.artistTracks,
          identity: _trackSearchIdentity,
        ),
        relatedAlbums: _mergeSearchEntities(
          state.relatedAlbums,
          nextState.relatedAlbums,
          identity: _albumSearchIdentity,
        ),
        diagnostics: <String, dynamic>{
          ...state.diagnostics,
          ...nextState.diagnostics,
          'side_surfaces_enriched': true,
        },
        clearError: true,
      );
    } catch (_) {
      return;
    }
  }

  void _primeSearchResults(List<Map<String, dynamic>> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
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
        deferSideSurfaces: true,
      );
      if (requestVersion != _requestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final nextState = SearchPageState.fromJson(payload);
        state = nextState.copyWith(
          requestState: 'complete',
          clearError: true,
          diagnostics: <String, dynamic>{
            ...nextState.diagnostics,
            'tracks_first_response': true,
          },
        );
        debugProxyLog(
          'search',
          'page query="$normalizedQuery" status=200 tracks=${nextState.tracks.length} artists=${nextState.artists.length} albums=${nextState.albums.length} similar=${nextState.similarArtists.length} diagnostics=${compactDiagnosticValue(nextState.diagnostics)}',
        );
        _primeSearchResults(nextState.tracks);
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
        notifyRecommendationSignal(normalizedQuery);
        final needsEnrichment = nextState.tracks.isNotEmpty &&
            (nextState.artists.isEmpty ||
                nextState.albums.isEmpty ||
                nextState.similarArtists.length < 4 ||
                nextState.similarTracks.length < 6 ||
                nextState.artistTracks.length < 4 ||
                nextState.relatedAlbums.length < 4);
        if (needsEnrichment) {
          unawaited(
            _enrichSearchPageResults(
              normalizedQuery,
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

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const SearchPageState(requestState: 'idle');
  }
}

final searchPageProvider =
    StateNotifierProvider<SearchPageNotifier, SearchPageState>((ref) {
  return SearchPageNotifier(ref);
});
