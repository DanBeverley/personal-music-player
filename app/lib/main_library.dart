import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'logic/audio_provider.dart';
import 'logic/audio_provider_history.dart';
import 'logic/audio_provider_queue.dart';
import 'logic/auth_provider.dart';
import 'logic/playlist_provider.dart';
import 'screens/playlist_detail_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'widgets/library/library_sections.dart';

const _accentGrey = appAccentGrey;
const double _radiusLarge = appRadiusLarge;
const double _radiusMedium = appRadiusMedium;

class LibraryScreen extends ConsumerWidget {
  const LibraryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final authState = ref.watch(authProvider);
    final libraryAsync = ref.watch(libraryProvider);
    final playlists = ref.watch(playlistProvider);
    final lastPlayedTracks = ref.watch(lastPlayedProvider);
    final isLastPlayedLoading = ref.watch(lastPlayedProvider.notifier).isLoading;
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
    final displayName =
        authState.user?.userMetadata?['full_name']?.toString() ??
            authState.user?.userMetadata?['name']?.toString() ??
            authState.user?.email?.split('@').first ??
            'Listener';
    final avatarUrl =
        authState.user?.userMetadata?['avatar_url']?.toString() ??
            authState.user?.userMetadata?['picture']?.toString();
    final providerName =
        authState.user?.appMetadata['provider']?.toString() ?? 'Supabase';

    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (authState.isConfigured) ...[
              LibraryProfileCard(
                displayName: displayName,
                email: authState.user?.email ?? 'Signed in',
                avatarUrl: avatarUrl,
                providerName: providerName,
                playlistCount: playlists.length,
                offlineCount: downloadedOfflineTracks.length,
                isBusy: authState.isBusy,
                radiusLarge: _radiusLarge,
                radiusMedium: _radiusMedium,
                onSignOut: () async {
                  await ref.read(audioPlayerProvider.notifier).stopPlayback();
                  ref.read(playbackQueueProvider.notifier).clearSession();
                  await ref.read(authProvider.notifier).signOut();
                },
              ),
              const SizedBox(height: 24),
            ],
            LibraryPlaylistsSection(
              playlists: playlists,
              onOpenPlaylist: (playlist) {
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (_) => PlaylistDetailScreen(playlistId: playlist.id),
                  ),
                );
              },
            ),
            const SizedBox(height: 32),
            if (lastPlayedTracks.isNotEmpty) ...[
              LibraryHistoryLane(
                title: 'Last played',
                tracks: lastPlayedTracks,
                isLoading: isLastPlayedLoading,
              ),
              const SizedBox(height: 32),
            ],
            SavedTracksSection(
              libraryAsync: libraryAsync,
              missingOfflineTracks: missingOfflineTracks,
              accentColor: _accentGrey,
            ),
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}
