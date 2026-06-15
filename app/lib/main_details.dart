import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'logic/audio_provider_queue.dart';
import 'logic/details_provider.dart';
import 'logic/download_provider.dart';
import 'logic/playlist_provider.dart';
import 'main_player.dart';
import 'ui/app_theme_tokens.dart';
import 'ui/neatie_components.dart';
import 'widgets/details/details_sections.dart';

const _accentGrey = neatieActive;
const _surfaceGreyAlt = neatieRaised;
const _voidBlack = neatieInk;
const double _radiusLarge = neatieRadiusLarge;

class ArtistDetailsScreen extends ConsumerStatefulWidget {
  final String artistId;
  final Map<String, dynamic> fallbackArtist;

  const ArtistDetailsScreen({
    super.key,
    required this.artistId,
    required this.fallbackArtist,
  });

  @override
  ConsumerState<ArtistDetailsScreen> createState() =>
      _ArtistDetailsScreenState();
}

class _ArtistDetailsScreenState extends ConsumerState<ArtistDetailsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(artistDetailsProvider.notifier).fetchArtist(widget.artistId),
      );
    });
  }

  Future<void> _playArtistTracks(
    Map<String, dynamic> artist,
    List<dynamic> tracks, {
    bool shuffle = false,
  }) async {
    if (tracks.isEmpty) return;
    final navigator = Navigator.of(context);
    final currentTrack = shuffle
        ? tracks[DateTime.now().microsecondsSinceEpoch % tracks.length]
        : tracks.first;
    final artistName = artist['name']?.toString() ?? 'Artist Radio';
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'artist:${widget.artistId}:top-songs',
          playlistName: '$artistName Top Songs',
          tracks: tracks,
          currentTrack: currentTrack,
          shuffle: shuffle,
        );
    if (!mounted) return;
    navigator.push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  Future<void> _openAlbum(Map<String, dynamic> album) async {
    final albumId = album['id']?.toString();
    if (albumId == null || albumId.isEmpty) return;
    unawaited(ref.read(albumDetailsProvider.notifier).fetchAlbum(albumId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => AlbumDetailsScreen(
          albumId: albumId,
          fallbackAlbum: album,
        ),
      ),
    );
  }

  Future<void> _openRelatedArtist(Map<String, dynamic> artist) async {
    final artistId = artist['id']?.toString();
    if (artistId == null || artistId.isEmpty) return;
    unawaited(ref.read(artistDetailsProvider.notifier).fetchArtist(artistId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: artist,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final artistState = ref.watch(artistDetailsProvider);
    final loadedArtist = artistState.artist;
    final artist = loadedArtist != null &&
            loadedArtist['id']?.toString() == widget.artistId
        ? loadedArtist
        : widget.fallbackArtist;
    final topSongs =
        (loadedArtist?['top_songs'] as List<dynamic>? ?? const <dynamic>[]);
    final albums =
        (loadedArtist?['albums'] as List<dynamic>? ?? const <dynamic>[]);
    final relatedArtists =
        (loadedArtist?['related_artists'] as List<dynamic>? ??
            const <dynamic>[]);
    final stats =
        (loadedArtist?['stats'] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map>()
            .map((entry) => Map<String, dynamic>.from(entry))
            .toList(growable: false);
    final description = (artist['description'] ?? '').toString().trim();

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: Text(artist['name']?.toString() ?? 'Artist'),
      ),
      body: NeatieBackground(
        child: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(18, 18, 18, 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ArtistHeroSection(
                  artist: artist,
                  description: description,
                  stats: stats,
                  radiusLarge: _radiusLarge,
                ),
                const SizedBox(height: 22),
                DetailsPlayShuffleActionsRow(
                  playLabel: 'Play top songs',
                  surfaceColor: _surfaceGreyAlt,
                  onPlay: topSongs.isEmpty
                      ? null
                      : () => _playArtistTracks(artist, topSongs),
                  onShuffle: topSongs.isEmpty
                      ? null
                      : () => _playArtistTracks(
                            artist,
                            topSongs,
                            shuffle: true,
                          ),
                ),
                if (albums.isNotEmpty) ...[
                  const SizedBox(height: 28),
                  ArtistAlbumsSection(
                    albums: albums,
                    radiusLarge: _radiusLarge,
                    onOpenAlbum: _openAlbum,
                  ),
                ],
                const SizedBox(height: 28),
                DetailsTrackListSection(
                  title: 'Top songs',
                  tracks: topSongs,
                  isLoading: artistState.isLoading,
                  emptyMessage:
                      artistState.error ??
                      'No top-song data available for this artist.',
                  fallbackSubtitle:
                      artist['name']?.toString() ?? 'Unknown Artist',
                  onPlayTrack: (track) async {
                    final navigator = Navigator.of(context);
                    await ref
                        .read(playbackQueueProvider.notifier)
                        .startPlaylistSession(
                          playlistId: 'artist:${widget.artistId}:top-songs',
                          playlistName:
                              '${artist['name']?.toString() ?? 'Artist'} Top Songs',
                          tracks: topSongs,
                          currentTrack: track,
                        );
                    if (!mounted) return;
                    navigator.push(
                      MaterialPageRoute(
                        builder: (_) => const FullPlayerScreen(),
                      ),
                    );
                  },
                ),
                if (relatedArtists.isNotEmpty) ...[
                  const SizedBox(height: 28),
                  RelatedArtistsSection(
                    artists: relatedArtists,
                    onOpenArtist: _openRelatedArtist,
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
class AlbumDetailsScreen extends ConsumerStatefulWidget {
  final String albumId;
  final Map<String, dynamic> fallbackAlbum;

  const AlbumDetailsScreen({
    super.key,
    required this.albumId,
    required this.fallbackAlbum,
  });

  @override
  ConsumerState<AlbumDetailsScreen> createState() => _AlbumDetailsScreenState();
}

class _AlbumDetailsScreenState extends ConsumerState<AlbumDetailsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(albumDetailsProvider.notifier).fetchAlbum(widget.albumId),
      );
    });
  }

  Future<void> _playAlbum(
    List<dynamic> tracks, {
    bool shuffle = false,
  }) async {
    if (tracks.isEmpty) return;
    final navigator = Navigator.of(context);
    final currentTrack = shuffle
        ? tracks[DateTime.now().microsecondsSinceEpoch % tracks.length]
        : tracks.first;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'album:${widget.albumId}',
          playlistName:
              widget.fallbackAlbum['title']?.toString() ?? 'Album Queue',
          tracks: tracks,
          currentTrack: currentTrack,
          shuffle: shuffle,
        );
    if (!mounted) return;
    navigator.push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  void _saveAlbumAsPlaylist(Map<String, dynamic> album, List<dynamic> tracks) {
    if (tracks.isEmpty) return;
    final title = album['title']?.toString() ?? 'Saved Album';
    final playlist = ref.read(playlistProvider.notifier).createPlaylist(title);
    for (final track in tracks) {
      ref
          .read(playlistProvider.notifier)
          .addTrackToPlaylist(playlist.id, track);
    }
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Saved "$title" as a playlist.'),
        backgroundColor: neatieRaised,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final albumState = ref.watch(albumDetailsProvider);
    final loadedAlbum = albumState.album;
    final album =
        loadedAlbum != null && loadedAlbum['id']?.toString() == widget.albumId
            ? loadedAlbum
            : widget.fallbackAlbum;
    final tracks = (loadedAlbum?['tracks'] as List<dynamic>?) ?? const [];

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: const Text('Album'),
      ),
      body: NeatieBackground(
        child: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                AlbumHeroSection(
                  album: album,
                  tracks: tracks,
                  radiusLarge: _radiusLarge,
                ),
                const SizedBox(height: 22),
                DetailsPlayShuffleActionsRow(
                  playLabel: 'Play Album',
                  surfaceColor: _surfaceGreyAlt,
                  onPlay: tracks.isEmpty ? null : () => _playAlbum(tracks),
                  onShuffle: tracks.isEmpty
                      ? null
                      : () => _playAlbum(tracks, shuffle: true),
                ),
                const SizedBox(height: 12),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Colors.white,
                    side: BorderSide(
                      color: Colors.white.withValues(alpha: 0.14),
                    ),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 14,
                    ),
                  ),
                  onPressed: tracks.isEmpty
                      ? null
                      : () => _saveAlbumAsPlaylist(album, tracks),
                  icon: const Icon(Icons.library_add_rounded),
                  label: const Text('Save Album As Playlist'),
                ),
                const SizedBox(height: 24),
                DetailsTrackListSection(
                  title: 'Tracks',
                  tracks: tracks,
                  isLoading: albumState.isLoading,
                  emptyMessage:
                      albumState.error ?? 'No track data available for this album.',
                  fallbackSubtitle: album['artist']?.toString() ?? '',
                  fallbackThumbnail: album['thumbnail']?.toString(),
                  onPlayTrack: (track) async {
                    final navigator = Navigator.of(context);
                    await ref
                        .read(playbackQueueProvider.notifier)
                        .startPlaylistSession(
                          playlistId: 'album:${widget.albumId}',
                          playlistName:
                              album['title']?.toString() ?? 'Album Queue',
                          tracks: tracks,
                          currentTrack: track,
                        );
                    if (!mounted) return;
                    navigator.push(
                      MaterialPageRoute(
                        builder: (_) => const FullPlayerScreen(),
                      ),
                    );
                  },
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class TrackDetailsScreen extends ConsumerWidget {
  final Map<String, dynamic> track;
  const TrackDetailsScreen({super.key, required this.track});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final details = ref.watch(trackDetailsProvider);
    final videoId = (track['id'] ?? track['videoId'])?.toString();
    final downloadTask =
        videoId == null ? null : ref.watch(downloadTaskProvider(videoId));
    final albumTitleValue = (details?['album_title'] ??
            details?['album'] ??
            track['album_title'] ??
            track['album'])
        ?.toString()
        .trim();
    final albumTitle = albumTitleValue == null || albumTitleValue.isEmpty
        ? null
        : albumTitleValue;
    final albumIdValue =
        (details?['album_id'] ?? track['album_id'])?.toString().trim();
    final albumId =
        albumIdValue == null || albumIdValue.isEmpty ? null : albumIdValue;
    final albumArtist =
        (details?['author'] ?? track['channel'] ?? track['author'] ?? '')
            .toString();

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: const Text(''),
        backgroundColor: Colors.transparent,
        elevation: 0,
      ),
      body: NeatieBackground(
        child: SingleChildScrollView(
          child: Column(
            children: [
              TrackDetailsHeaderSection(track: track, details: details),

              if (albumTitle != null)
                TrackAlbumLinkCard(
                  albumTitle: albumTitle,
                  radiusLarge: _radiusLarge,
                  onTap: albumId == null
                      ? null
                      : () {
                          final resolvedAlbumId = albumId;
                          unawaited(
                            ref.read(albumDetailsProvider.notifier).fetchAlbum(
                                  resolvedAlbumId,
                                ),
                          );
                          Navigator.of(context).push(
                            MaterialPageRoute(
                              builder: (_) => AlbumDetailsScreen(
                                albumId: resolvedAlbumId,
                                fallbackAlbum: {
                                  'id': resolvedAlbumId,
                                  'title': albumTitle,
                                  'artist': albumArtist,
                                  'thumbnail': track['thumbnail'],
                                  'year': '',
                                },
                              ),
                            ),
                          );
                        },
                ),

              TrackDetailsActionsRow(
                track: track,
                downloadTask: downloadTask,
                accentColor: _accentGrey,
                onPlay: () {
                  unawaited(
                    ref.read(playbackQueueProvider.notifier).startRadioSession(
                          track,
                        ),
                  );
                  Navigator.of(context).push(
                    MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
                  );
                },
                onDownload: videoId == null
                    ? null
                    : () {
                        ref
                            .read(downloadCenterProvider.notifier)
                            .downloadTrack(track);
                      },
              ),

              // Similar Tracks
              if (details == null)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.all(32),
                    child: CircularProgressIndicator(color: Colors.white),
                  ),
                )
              else if (details['similar_tracks'] != null &&
                  (details['similar_tracks'] as List).isNotEmpty) ...[
                SimilarTracksSection(
                  tracks: (details['similar_tracks'] as List)
                      .toList(growable: false),
                  onOpenTrack: (trackData) {
                    ref
                        .read(trackDetailsProvider.notifier)
                        .fetchDetails(trackData['id']);
                    Navigator.pushReplacement(
                      context,
                      MaterialPageRoute(
                        builder: (_) => TrackDetailsScreen(track: trackData),
                      ),
                    );
                  },
                ),
              ],
              const SizedBox(height: 80),
            ],
          ),
        ),
      ),
    );
  }
}
