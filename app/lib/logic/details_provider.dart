import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart';
import 'audio_provider_recommendation.dart';
import 'auth_provider.dart';

class TrackDetailsNotifier extends StateNotifier<Map<String, dynamic>?> {
  final Ref ref;

  TrackDetailsNotifier(this.ref) : super(null);

  Future<void> fetchDetails(String videoId) async {
    state = null;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(videoId));
    try {
      final res = await appHttpClient.post(
        buildProxyUri('/track_details'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({"video_id": videoId}),
      );
      if (res.statusCode == 200) {
        state = jsonDecode(res.body);
        final similarTracks =
            (state?['similar_tracks'] as List<dynamic>?) ?? const [];
        unawaited(
          ref.read(audioPlayerProvider.notifier).prewarmStreams([
            videoId,
            ...similarTracks
                .take(8)
                .map((track) => track['id'] ?? track['videoId']),
          ]),
        );
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
        ref,
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
        logProxyDiagnostic(
          'search',
          'album query="$query" status=200 results=${state.length} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            query,
            resultCount: state.length,
            searchScope: 'album',
          ),
        );
      } else {
        logProxyDiagnostic(
          'search',
          'album query="$query" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      logProxyDiagnostic('search', 'album query="$query" error=$error');
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
      final tracks = (payload['tracks'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              tracks.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
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
        ref,
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
        logProxyDiagnostic(
          'search',
          'artist query="$trimmed" status=200 results=${state.length} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            trimmed,
            resultCount: state.length,
            searchScope: 'artist',
          ),
        );
      } else {
        logProxyDiagnostic(
          'search',
          'artist query="$trimmed" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      logProxyDiagnostic('search', 'artist query="$trimmed" error=$error');
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
      logProxyDiagnostic(
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
      logProxyDiagnostic(
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
        logProxyDiagnostic(
          'artists',
          'recommended deferred for home_feed because stable feed seeds are not available yet',
        );
        if (!_isRequestCurrent(requestVersion)) return;
        state = previousState;
        return;
      }
      logProxyDiagnostic(
        'artists',
        'recommended start surface=$surface force=$forceRefresh hints=${compactProxyDiagnosticValue(requestArtistHints)} anchors=${normalizedAnchorTracks.length}',
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
        logProxyDiagnostic(
          'artists',
          'recommended surface=$surface status=200 results=${state.length} diagnostics=${compactProxyDiagnosticValue(payload['diagnostics'])}',
        );
      } else {
        logProxyDiagnostic(
          'artists',
          'recommended surface=$surface status=${res.statusCode} body=${res.body}',
        );
        state = previousState;
      }
    } catch (error) {
      logProxyDiagnostic('artists', 'recommended surface=$surface error=$error');
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
      final topSongs = (payload['top_songs'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              topSongs.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
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

  Future<void> fetchLyrics(String? videoId) async {
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
      final res = await appHttpClient
          .get(buildProxyUri('/lyrics/$videoId'))
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
