import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider_recommendation.dart';
import 'auth_provider.dart';
import 'cloud_search_queries.dart';
import 'history_manager.dart';
import 'interaction_events.dart';
import 'library_catalog_provider.dart';
import 'playback_models.dart';
import 'proxy_runtime.dart';
import 'recommendation_feed_models.dart';
import 'search_semantics.dart';
import 'search_payload_runtime.dart';
import 'track_metadata.dart';

Future<Map<String, dynamic>?> resolveArtistReference(
  ProviderReader read,
  Map<String, dynamic> artist,
) async {
  final directId = (artist['id'] ?? artist['browseId'] ?? artist['artist_id'])
          ?.toString()
          .trim() ??
      '';
  final resolutionStatus =
      artist['resolution_status']?.toString().trim().toLowerCase() ?? '';
  final isCanonicalBrowseId = directId.startsWith('UC');
  if (directId.isNotEmpty &&
      isCanonicalBrowseId &&
      resolutionStatus != 'derived_from_track') {
    return <String, dynamic>{...artist, 'id': directId};
  }
  final artistName = artist['name']?.toString().trim() ?? '';
  if (artistName.isEmpty) return null;
  Map<String, dynamic>? payload;
  try {
    final response = await runSearchRequest(
      appHttpClient.post(
          buildProxyUri('/resolve_artist'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'query': artistName,
            'anchor_artist_id': directId,
            'limit': 4,
            'user_scope_id': read(authProvider).storageScopeId,
            'search_mode': 'entity',
            'defer_side_surfaces': true,
          }),
        ),
      const Duration(seconds: 12),
    );
    if (response.statusCode == 200) {
      payload = jsonDecode(response.body) as Map<String, dynamic>;
    }
  } catch (_) {
    payload = null;
  }
  final directArtist = payload?['artist'];
  if (directArtist is Map) {
    final resolved = Map<String, dynamic>.from(directArtist);
    final resolvedId =
        (resolved['id'] ?? resolved['browseId'])?.toString().trim() ?? '';
    if (resolvedId.isNotEmpty) {
      return <String, dynamic>{...resolved, 'id': resolvedId};
    }
  }
  final candidates = (payload?['artists'] as List<dynamic>? ?? const [])
      .whereType<Map>()
      .map((entry) => Map<String, dynamic>.from(entry))
      .toList(growable: false);
  if (candidates.isEmpty) {
    return isCanonicalBrowseId ? <String, dynamic>{...artist, 'id': directId} : null;
  }
  final normalizedName = artistName.toLowerCase();
  final resolved = candidates.firstWhere(
    (candidate) =>
        candidate['name']?.toString().trim().toLowerCase() == normalizedName,
    orElse: () => candidates.first,
  );
  final resolvedId =
      (resolved['id'] ?? resolved['browseId'])?.toString().trim() ?? '';
  return resolvedId.isEmpty ? null : <String, dynamic>{...resolved, 'id': resolvedId};
}

class TrackDetailsNotifier extends StateNotifier<Map<String, dynamic>?> {
  final Ref ref;

  TrackDetailsNotifier(this.ref) : super(null);

  Future<void> fetchDetails(String videoId) async {
    state = null;
    try {
      final res = await appHttpClient.post(
        buildProxyUri('/track_details'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({"video_id": videoId}),
      );
      if (res.statusCode == 200) {
        state = jsonDecode(res.body);
      }
    } catch (_) {
      // Track details stay best-effort.
    }
  }
}

final trackDetailsProvider =
    StateNotifierProvider<TrackDetailsNotifier, Map<String, dynamic>?>((ref) {
  return TrackDetailsNotifier(ref);
});

class AlbumSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;

  AlbumSearchNotifier(this.ref) : super(const []);
  bool isLoading = false;

  Future<void> search(String query) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final body = await buildSemanticSearchRequestBody(
        ref.read,
        query,
        limit: 12,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/search_albums'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final albums = (payload['albums'] as List<dynamic>? ?? const []);
        state = albums
            .map((album) => Map<String, dynamic>.from(album as Map))
            .toList(growable: false);
        debugProxyLog(
          'search',
          'album query="$query" status=200 results=${state.length} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            query,
            resultCount: state.length,
            searchScope: 'album',
          ),
        );
      } else {
        debugProxyLog(
          'search',
          'album query="$query" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      debugProxyLog('search', 'album query="$query" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final albumSearchProvider =
    StateNotifierProvider<AlbumSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return AlbumSearchNotifier(ref);
});

class AlbumDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? album;
  final String? error;

  const AlbumDetailsState({
    this.isLoading = false,
    this.album,
    this.error,
  });
}

class AlbumDetailsNotifier extends StateNotifier<AlbumDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  AlbumDetailsNotifier(this.ref) : super(const AlbumDetailsState());

  Future<void> fetchAlbum(String? albumId) async {
    if (albumId == null || albumId.isEmpty) {
      state = const AlbumDetailsState(error: 'Album unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const AlbumDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/album/$albumId'))
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const AlbumDetailsState(error: 'Album unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      state = AlbumDetailsState(album: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const AlbumDetailsState(error: 'Album unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const AlbumDetailsState();
  }
}

final albumDetailsProvider =
    StateNotifierProvider<AlbumDetailsNotifier, AlbumDetailsState>((ref) {
  return AlbumDetailsNotifier(ref);
});

class ArtistSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;

  ArtistSearchNotifier(this.ref) : super(const []);

  Future<void> search(String query) async {
    final trimmed = query.trim();
    if (trimmed.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final body = await buildSemanticSearchRequestBody(
        ref.read,
        trimmed,
        limit: 12,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/search_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
        debugProxyLog(
          'search',
          'artist query="$trimmed" status=200 results=${state.length} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            trimmed,
            resultCount: state.length,
            searchScope: 'artist',
          ),
        );
      } else {
        debugProxyLog(
          'search',
          'artist query="$trimmed" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      debugProxyLog('search', 'artist query="$trimmed" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final artistSearchProvider =
    StateNotifierProvider<ArtistSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return ArtistSearchNotifier(ref);
});

class RecommendedArtistsNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  RecommendedArtistsNotifier(this.ref) : super(const []);

  void _bumpWeight(Map<String, double> weights, String? value, double weight) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    weights.update(
      normalized,
      (current) => current + weight,
      ifAbsent: () => weight,
    );
  }

  void _bumpTrackArtists(
    Map<String, double> weights,
    dynamic rawTrack,
    double weight,
  ) {
    for (final artist in extractTrackArtists(rawTrack)) {
      _bumpWeight(weights, artist, weight);
    }
  }

  bool _looksLikeArtistOrSongQuery(String query) {
    final normalized = query.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    if (isMetadataHeavyQuery(normalized)) return false;
    if (normalized.contains(' playlist') ||
        normalized.contains(' mix') ||
        normalized.contains(' mood') ||
        normalized.contains(' songs like')) {
      return false;
    }
    return true;
  }

  List<Map<String, dynamic>> _normalizeAnchorTracks(
    Iterable<dynamic> rawTracks, {
    int limit = 6,
  }) {
    final tracks = <Map<String, dynamic>>[];
    final seen = <String>{};
    for (final rawTrack in rawTracks) {
      if (rawTrack is! Map) continue;
      final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
      final key = recommendationRowItemKey('track', track);
      if (key.trim().isEmpty || !seen.add(key)) continue;
      tracks.add(track);
      if (tracks.length >= limit) {
        break;
      }
    }
    return tracks;
  }

  Future<void> bootstrap() async {
    final authState = ref.read(authProvider);
    if (authState.isConfigured && !authState.isInitialized) {
      isLoading = false;
      if (mounted) {
        state = [...state];
      }
      debugProxyLog(
        'artists',
        'bootstrap deferred until auth initialization completes for scope=${authState.storageScopeId}',
      );
      return;
    }
    final recState = ref.read(recommendationProvider);
    if (!recState.hasRows) {
      isLoading = false;
      if (mounted) {
        state = [...state];
      }
      debugProxyLog(
        'artists',
        'bootstrap deferred until recommendation rows are available for scope=${authState.storageScopeId}',
      );
      return;
    }
    await loadRecommendedArtists(
      seedArtistHints: recState.visibleTracks
          .expand(extractTrackArtists)
          .take(6)
          .toSet()
          .toList(growable: false),
      surface: 'home_feed',
    );
  }

  Future<void> loadRecommendedArtists({
    List<String> seedArtistHints = const [],
    List<String> seedTasteQueries = const [],
    String surface = 'home_feed',
    List<Map<String, dynamic>> anchorTracks = const [],
    bool forceRefresh = false,
  }) async {
    final requestVersion = ++_requestVersion;
    final previousState = List<Map<String, dynamic>>.from(state);
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      final artistWeights = <String, double>{};
      final recState = ref.read(recommendationProvider);
      final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
      final artistSignals = await Future.wait<Object?>([
        HistoryManager.getLastPlayedTrackSnapshots(limit: 10),
        HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 12),
        HistoryManager.getRecentTrackSnapshots(limit: 12),
        getRecentCloudSearchQueries(limit: 8),
      ]);
      if (!_isRequestCurrent(requestVersion)) return;
      final lastPlayedSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[0] as List);
      final frequentSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[1] as List);
      final recentSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[2] as List);
      final recentQueries = List<String>.from(artistSignals[3] as List);
      final normalizedAnchorTracks = _normalizeAnchorTracks(anchorTracks);
      final effectiveSeedArtistHints = seedArtistHints.isNotEmpty
          ? seedArtistHints
          : surface == 'home_feed'
              ? recState.visibleTracks
                  .expand(extractTrackArtists)
                  .take(6)
                  .toSet()
                  .toList(growable: false)
              : const <String>[];

      for (final artist in effectiveSeedArtistHints) {
        _bumpWeight(artistWeights, artist, 2.8);
      }
      for (final track in lastPlayedSnapshots) {
        _bumpTrackArtists(artistWeights, track, 2.2);
      }
      for (final track in frequentSnapshots) {
        _bumpTrackArtists(artistWeights, track, 1.9);
      }
      for (final track in recentSnapshots) {
        _bumpTrackArtists(artistWeights, track, 1.4);
      }

      for (final track in libraryTracks.take(24)) {
        _bumpTrackArtists(artistWeights, track, 1.1);
      }

      final canonicalQueries = surface == 'home_feed'
          ? <String>[
              ...seedTasteQueries.where(_looksLikeArtistOrSongQuery),
            ]
          : <String>[
              ...seedTasteQueries.where(_looksLikeArtistOrSongQuery),
              ...recentQueries.where(_looksLikeArtistOrSongQuery),
            ];

      final rankedArtists = artistWeights.entries.toList()
        ..sort((a, b) => b.value.compareTo(a.value));
      final requestArtistHints = rankedArtists
          .take(8)
          .map((entry) => entry.key)
          .toList(growable: false);
      if (surface == 'home_feed' &&
          requestArtistHints.isEmpty &&
          normalizedAnchorTracks.isEmpty) {
        debugProxyLog(
          'artists',
          'recommended deferred for home_feed because stable feed seeds are not available yet',
        );
        if (!_isRequestCurrent(requestVersion)) return;
        state = previousState;
        return;
      }
      debugProxyLog(
        'artists',
        'recommended start surface=$surface force=$forceRefresh hints=${compactDiagnosticValue(requestArtistHints)} anchors=${normalizedAnchorTracks.length}',
      );

      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/recommended_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'query': canonicalQueries.isEmpty ? '' : canonicalQueries.first,
              'limit': 8,
              'surface': surface,
              'force_refresh': forceRefresh,
              'artist_hints': requestArtistHints,
              'anchor_artist_hints':
                  effectiveSeedArtistHints.take(6).toList(growable: false),
              'anchor_track_snapshots': normalizedAnchorTracks,
              'recent_queries':
                  canonicalQueries.take(6).toList(growable: false),
              'recent_track_snapshots': recentSnapshots,
              'top_track_snapshots': frequentSnapshots,
              'last_played_tracks': lastPlayedSnapshots,
            }),
          )
          .timeout(const Duration(seconds: 10));
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
        debugProxyLog(
          'artists',
          'recommended surface=$surface status=200 results=${state.length} diagnostics=${compactDiagnosticValue(payload['diagnostics'])}',
        );
      } else {
        debugProxyLog(
          'artists',
          'recommended surface=$surface status=${res.statusCode} body=${res.body}',
        );
        state = previousState;
      }
    } catch (error) {
      debugProxyLog('artists', 'recommended surface=$surface error=$error');
      if (!_isRequestCurrent(requestVersion)) return;
      state = previousState;
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final recommendedArtistsProvider = StateNotifierProvider<
    RecommendedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  return RecommendedArtistsNotifier(ref);
});

final searchSimilarArtistsProvider = StateNotifierProvider<
    RecommendedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  return RecommendedArtistsNotifier(ref);
});

class ArtistDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? artist;
  final String? error;

  const ArtistDetailsState({
    this.isLoading = false,
    this.artist,
    this.error,
  });
}

class ArtistDetailsNotifier extends StateNotifier<ArtistDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  ArtistDetailsNotifier(this.ref) : super(const ArtistDetailsState());

  Future<void> fetchArtist(String? artistId) async {
    if (artistId == null || artistId.isEmpty) {
      state = const ArtistDetailsState(error: 'Artist unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const ArtistDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/artist/$artistId'))
          .timeout(const Duration(seconds: 14));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const ArtistDetailsState(error: 'Artist unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      state = ArtistDetailsState(artist: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const ArtistDetailsState(error: 'Artist unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const ArtistDetailsState();
  }
}

final artistDetailsProvider =
    StateNotifierProvider<ArtistDetailsNotifier, ArtistDetailsState>((ref) {
  return ArtistDetailsNotifier(ref);
});

class LyricsNotifier extends StateNotifier<TrackLyricsState> {
  int _requestVersion = 0;

  LyricsNotifier() : super(const TrackLyricsState());

  Future<void> fetchLyrics(
    String? videoId, {
    String? title,
    String? artist,
  }) async {
    if (videoId == null || videoId.isEmpty) {
      clear();
      return;
    }
    if (state.videoId == videoId) {
      return;
    }

    final requestVersion = ++_requestVersion;
    state = TrackLyricsState(isLoading: true, videoId: videoId);
    try {
      final query = <String, String>{
        if (title != null && title.trim().isNotEmpty) 'title': title.trim(),
        if (artist != null && artist.trim().isNotEmpty)
          'artist': artist.trim(),
      };
      final uri = buildProxyUri('/lyrics/$videoId').replace(
        queryParameters: query.isEmpty ? null : query,
      );
      final res = await appHttpClient
          .get(uri)
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;

      if (res.statusCode != 200) {
        state = TrackLyricsState(
          videoId: videoId,
          error: 'Lyrics unavailable right now.',
        );
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rawLines = (payload['lines'] as List<dynamic>? ?? const []);
      final lines = rawLines
          .map((raw) =>
              TrackLyricsLine.fromJson(Map<String, dynamic>.from(raw as Map)))
          .where((line) => line.text.trim().isNotEmpty)
          .toList(growable: false);

      state = TrackLyricsState(
        videoId: videoId,
        hasLyrics: payload['has_lyrics'] == true && lines.isNotEmpty,
        hasTimestamps: payload['has_timestamps'] == true,
        source: payload['source']?.toString(),
        lines: lines,
      );
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = TrackLyricsState(
        videoId: videoId,
        error: 'Lyrics unavailable right now.',
      );
    }
  }

  void clear() {
    _requestVersion++;
    state = const TrackLyricsState();
  }
}

final lyricsProvider =
    StateNotifierProvider<LyricsNotifier, TrackLyricsState>((ref) {
  return LyricsNotifier();
});

class LyricsMeaningNotifier extends StateNotifier<LyricsMeaningState> {
  final Ref ref;
  int _requestVersion = 0;

  LyricsMeaningNotifier(this.ref) : super(const LyricsMeaningState());

  Future<void> fetchMeaning({
    required String? videoId,
    required String title,
    required String? artist,
    String? album,
    String? year,
    String? source,
    List<TrackLyricsLine> lines = const [],
  }) async {
    final resolvedVideoId = videoId?.trim() ?? '';
    if (resolvedVideoId.isEmpty) {
      state = const LyricsMeaningState(error: 'No track is loaded yet.');
      return;
    }
    if (state.videoId == resolvedVideoId &&
        state.hasInsight &&
        !state.isLoading) {
      return;
    }

    final requestVersion = ++_requestVersion;
    state = LyricsMeaningState(isLoading: true, videoId: resolvedVideoId);
    try {
      final authState = ref.read(authProvider);
      final body = {
        'video_id': resolvedVideoId,
        'title': title,
        if (artist != null && artist.trim().isNotEmpty) 'artist': artist.trim(),
        if (album != null && album.trim().isNotEmpty) 'album': album.trim(),
        if (year != null && year.trim().isNotEmpty) 'year': year.trim(),
        if (source != null && source.trim().isNotEmpty) 'source': source.trim(),
        'user_scope_id': authState.storageScopeId,
        'lines': lines
            .map((line) => {
                  'index': line.index,
                  'text': line.text,
                  if (line.startTimeMs != null)
                    'start_time_ms': line.startTimeMs,
                  if (line.endTimeMs != null) 'end_time_ms': line.endTimeMs,
                })
            .toList(growable: false),
      };
      final res = await appHttpClient
          .post(
            buildProxyUri('/lyrics/$resolvedVideoId/meaning'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 50));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = LyricsMeaningState(
          videoId: resolvedVideoId,
          error: 'Neatie could not interpret this song right now.',
        );
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      state = LyricsMeaningState(
        videoId: resolvedVideoId,
        insight: LyricsMeaningInsight.fromJson(payload),
      );
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = LyricsMeaningState(
        videoId: resolvedVideoId,
        error: 'Neatie could not interpret this song right now.',
      );
    }
  }

  void clear() {
    _requestVersion++;
    state = const LyricsMeaningState();
  }
}

final lyricsMeaningProvider =
    StateNotifierProvider<LyricsMeaningNotifier, LyricsMeaningState>((ref) {
  return LyricsMeaningNotifier(ref);
});
