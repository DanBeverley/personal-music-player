import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'logic/audio_provider_history.dart';
import 'logic/details_provider.dart';
import 'logic/followed_artists_provider.dart';
import 'logic/library_catalog_provider.dart';
import 'logic/playlist_provider.dart';
import 'logic/track_metadata.dart';
import 'main_details.dart';
import 'main_dialogs.dart';
import 'screens/playlist_detail_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'ui/neatie_components.dart';
import 'widgets/library/library_sections.dart';
import 'widgets/library/library_update_panel.dart';

const _accentGrey = neatieActive;

class LibraryScreen extends ConsumerStatefulWidget {
  const LibraryScreen({super.key, this.onOpenSearchTab});

  final VoidCallback? onOpenSearchTab;

  @override
  ConsumerState<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends ConsumerState<LibraryScreen> {
  String _selectedFilter = 'Playlists';

  String _albumKey(Map<String, dynamic> track) {
    final album = track['album'];
    final title = album is Map
        ? (album['title'] ?? album['name'])?.toString().trim() ?? ''
        : album?.toString().trim() ?? '';
    final artist = (track['artist'] ??
                track['artist_name'] ??
                track['author'] ??
                track['channel'])
            ?.toString()
            .trim() ??
        '';
    return '${title.toLowerCase()}|${artist.toLowerCase()}';
  }

  List<Map<String, dynamic>> _deriveAlbumShelf(
    Iterable<Map<String, dynamic>> tracks,
  ) {
    final grouped = <String, Map<String, dynamic>>{};
    for (final track in tracks) {
      final album = track['album'];
      final title = album is Map
          ? (album['title'] ?? album['name'])?.toString().trim() ?? ''
          : album?.toString().trim() ?? '';
      if (title.isEmpty) continue;
      final artist = (track['artist'] ??
                  track['artist_name'] ??
                  track['author'] ??
                  track['channel'])
              ?.toString()
              .trim() ??
          '';
      final key = _albumKey(track);
      if (key.trim() == '|') continue;
      final existing = grouped[key];
      final albumId = album is Map
          ? (album['id'] ?? album['browseId'])?.toString()
          : (track['album_id'] ?? track['albumId'])?.toString();
      grouped[key] = {
        ...?existing,
        'id': albumId ?? existing?['id'] ?? extractTrackId(track) ?? key,
        'title': title,
        'artist': artist,
        'artist_name': artist,
        'thumbnail': existing?['thumbnail'] ?? track['thumbnail'],
        'track_count': ((existing?['track_count'] as num?)?.toInt() ?? 0) + 1,
        'seed_track': track,
      };
    }
    final albums = grouped.values.toList(growable: false)
      ..sort((a, b) {
        final countCompare = ((b['track_count'] as num?)?.toInt() ?? 0)
            .compareTo((a['track_count'] as num?)?.toInt() ?? 0);
        if (countCompare != 0) return countCompare;
        return (a['title']?.toString() ?? '')
            .compareTo(b['title']?.toString() ?? '');
      });
    return albums;
  }

  List<Map<String, dynamic>> _deriveArtistShelf(
    Iterable<Map<String, dynamic>> tracks, {
    int limit = 18,
  }) {
    final artists = <String, Map<String, dynamic>>{};
    for (final track in tracks) {
      final trackArtists = extractTrackArtists(track);
      for (final name in trackArtists) {
        final key = name.trim().toLowerCase();
        if (key.isEmpty) continue;
        final existing = artists[key];
        artists[key] = {
          ...?existing,
          'id':
              existing?['id'] ?? track['artist_id'] ?? track['channel_id'] ?? key,
          'name': name,
          'thumbnail': existing?['thumbnail'] ?? track['thumbnail'],
          'track_count': ((existing?['track_count'] as num?)?.toInt() ?? 0) + 1,
        };
      }
    }
    final output = artists.values.toList(growable: false)
      ..sort((a, b) {
        final countCompare = ((b['track_count'] as num?)?.toInt() ?? 0)
            .compareTo((a['track_count'] as num?)?.toInt() ?? 0);
        if (countCompare != 0) return countCompare;
        return (a['name']?.toString() ?? '')
            .compareTo(b['name']?.toString() ?? '');
      });
    return output.take(limit).toList(growable: false);
  }

  Future<void> _openAlbum(Map<String, dynamic> album) async {
    final albumId = album['id']?.toString().trim() ?? '';
    if (albumId.isEmpty) return;
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

  Future<void> _openArtist(Map<String, dynamic> artist) async {
    final resolvedArtist = await resolveArtistReference(ref.read, artist);
    if (!mounted) return;
    final artistId = (resolvedArtist?['id'] ?? resolvedArtist?['browseId'])
            ?.toString()
            .trim() ??
        '';
    if (resolvedArtist == null || artistId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Artist details are not available yet.')),
      );
      return;
    }
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: resolvedArtist,
        ),
      ),
    );
  }

  void _showCreatePlaylistDialog(BuildContext context, WidgetRef ref) {
    final ctrl = TextEditingController();
    showGlassDialog(
      context: context,
      title: 'New Playlist',
      content: TextField(
        controller: ctrl,
        style: const TextStyle(color: Colors.white),
        decoration: const InputDecoration(
          hintText: 'Playlist name',
          hintStyle: TextStyle(color: Colors.white54),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel', style: TextStyle(color: Colors.white54)),
        ),
        TextButton(
          onPressed: () {
            if (ctrl.text.trim().isNotEmpty) {
              ref.read(playlistProvider.notifier).createPlaylist(
                    ctrl.text.trim(),
                  );
            }
            Navigator.pop(context);
          },
          child: const Text('Create', style: TextStyle(color: Colors.white)),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final libraryAsync = ref.watch(libraryProvider);
    final playlists = ref.watch(playlistProvider);
    final lastPlayedTracks = ref.watch(lastPlayedProvider);
    final frequentTracks = ref.watch(frequentlyPlayedProvider);
    final followedArtists = ref.watch(followedArtistsProvider);
    final isLastPlayedLoading =
        ref.watch(lastPlayedProvider.notifier).isLoading;
    final offlineTracks =
        libraryAsync.valueOrNull ?? const <Map<String, dynamic>>[];
    final downloadedOfflineTracks = offlineTracks
        .where((track) => track['is_downloaded_locally'] == true)
        .toList(growable: false);
    final missingOfflineTracks = offlineTracks
        .where(
          (track) =>
              track['is_cloud_saved'] == true &&
              track['is_downloaded_locally'] != true &&
              (extractTrackId(track)?.isNotEmpty ?? false),
        )
        .map((track) => Map<String, dynamic>.from(track))
        .toList(growable: false);
    final combinedLibraryTracks = <Map<String, dynamic>>[
      ...offlineTracks,
      ...lastPlayedTracks,
      ...frequentTracks,
    ];
    final albumShelf = _deriveAlbumShelf(combinedLibraryTracks);
    final frequentArtists = _deriveArtistShelf(frequentTracks);
    final recentArtists = _deriveArtistShelf(lastPlayedTracks);

    return NeatieBackground(
      child: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(18, 18, 18, 180),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              LibraryReferenceHeader(
                onCreatePlaylist: () => _showCreatePlaylistDialog(context, ref),
                onSearch: widget.onOpenSearchTab ?? () {},
                selectedFilter: _selectedFilter,
                onFilterSelected: (filter) {
                  setState(() => _selectedFilter = filter);
                },
              ),
              const SizedBox(height: 28),
              if (_selectedFilter == 'Playlists') ...[
                LibraryQuickAccessSection(
                  savedCount: offlineTracks.length,
                  downloadedCount: downloadedOfflineTracks.length,
                ),
                const SizedBox(height: 18),
                const LibraryUpdatePanel(radiusLarge: neatieRadiusLarge),
                const SizedBox(height: 28),
                LibraryPlaylistsSection(
                  playlists: playlists,
                  onOpenPlaylist: (playlist) {
                    Navigator.push(
                      context,
                      MaterialPageRoute(
                        builder: (_) =>
                            PlaylistDetailScreen(playlistId: playlist.id),
                      ),
                    );
                  },
                ),
                const SizedBox(height: 28),
                if (lastPlayedTracks.isNotEmpty) ...[
                  LibraryHistoryLane(
                    title: 'Last played',
                    tracks: lastPlayedTracks,
                    isLoading: isLastPlayedLoading,
                  ),
                  const SizedBox(height: 28),
                ],
                SavedTracksSection(
                  libraryAsync: libraryAsync,
                  missingOfflineTracks: missingOfflineTracks,
                  accentColor: _accentGrey,
                ),
              ] else if (_selectedFilter == 'Downloaded') ...[
                LibraryQuickAccessSection(
                  savedCount: offlineTracks.length,
                  downloadedCount: downloadedOfflineTracks.length,
                ),
                const SizedBox(height: 28),
                SavedTracksSection(
                  libraryAsync: libraryAsync.whenData(
                    (tracks) => tracks
                        .where((track) => track['is_downloaded_locally'] == true)
                        .toList(growable: false),
                  ),
                  missingOfflineTracks: const [],
                  accentColor: _accentGrey,
                ),
              ] else if (_selectedFilter == 'Albums') ...[
                LibraryAlbumShelfSection(
                  albums: albumShelf,
                  onOpenAlbum: (album) => unawaited(_openAlbum(album)),
                ),
              ] else ...[
                LibraryArtistShelfSection(
                  title: 'Followed artists',
                  artists: followedArtists,
                  onOpenArtist: (artist) => unawaited(_openArtist(artist)),
                  emptyTitle: 'No followed artists yet',
                  emptyBody:
                      'Follow artists from their profile to pin them here.',
                ),
                const SizedBox(height: 28),
                LibraryArtistShelfSection(
                  title: 'Frequently listened artists',
                  artists: frequentArtists,
                  onOpenArtist: (artist) => unawaited(_openArtist(artist)),
                ),
                const SizedBox(height: 28),
                LibraryArtistShelfSection(
                  title: 'Recently played artists',
                  artists: recentArtists,
                  onOpenArtist: (artist) => unawaited(_openArtist(artist)),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
