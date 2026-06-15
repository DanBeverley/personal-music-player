import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'logic/audio_provider_history.dart';
import 'logic/library_catalog_provider.dart';
import 'logic/playlist_provider.dart';
import 'logic/track_metadata.dart';
import 'main_dialogs.dart';
import 'screens/playlist_detail_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'ui/neatie_components.dart';
import 'widgets/library/library_sections.dart';

const _accentGrey = neatieActive;

class LibraryScreen extends ConsumerStatefulWidget {
  const LibraryScreen({super.key, this.onOpenSearchTab});

  final VoidCallback? onOpenSearchTab;

  @override
  ConsumerState<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends ConsumerState<LibraryScreen> {
  String _selectedFilter = 'Playlists';

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
                const LibraryEmptyStateCard(
                  title: 'Album shelf is coming',
                  body:
                      'Saved albums need a dedicated source before this tab should pretend to be full.',
                ),
              ] else ...[
                const LibraryEmptyStateCard(
                  title: 'Artist shelf is coming',
                  body:
                      'Once we persist followed or saved artists, this tab can become a real artist library.',
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
