import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider_queue.dart';
import '../../logic/details_provider.dart';
import '../../logic/download_provider.dart';
import '../../logic/library_catalog_provider.dart';
import '../../logic/playlist_provider.dart';
import '../../logic/track_metadata.dart';
import '../../main_details.dart';
import '../../main_dialogs.dart';
import '../../main_player.dart';
import '../app_artwork.dart';

class LibraryStatChip extends StatelessWidget {
  final String label;

  const LibraryStatChip({super.key, required this.label});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: Colors.white.withValues(alpha: 0.8),
          fontSize: 11,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.35,
        ),
      ),
    );
  }
}

class LibraryProfileCard extends StatelessWidget {
  final String displayName;
  final String email;
  final String? avatarUrl;
  final String providerName;
  final int playlistCount;
  final int offlineCount;
  final bool isBusy;
  final double radiusLarge;
  final double radiusMedium;
  final VoidCallback onSignOut;

  const LibraryProfileCard({
    super.key,
    required this.displayName,
    required this.email,
    required this.avatarUrl,
    required this.providerName,
    required this.playlistCount,
    required this.offlineCount,
    required this.isBusy,
    required this.radiusLarge,
    required this.radiusMedium,
    required this.onSignOut,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(radiusLarge),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
      ),
      child: Row(
        children: [
          Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(radiusMedium),
            ),
            alignment: Alignment.center,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(radiusMedium),
              child: avatarUrl != null && avatarUrl!.isNotEmpty
                  ? Image.network(
                      avatarUrl!,
                      fit: BoxFit.cover,
                      errorBuilder: (_, __, ___) => const Icon(
                        Icons.person_outline_rounded,
                        color: Colors.white,
                      ),
                    )
                  : const Icon(
                      Icons.person_outline_rounded,
                      color: Colors.white,
                    ),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  displayName,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 17,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  email,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.56),
                    fontSize: 13,
                  ),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    LibraryStatChip(label: providerName.toUpperCase()),
                    LibraryStatChip(label: '$playlistCount playlists'),
                    LibraryStatChip(label: '$offlineCount offline'),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          TextButton(
            onPressed: isBusy ? null : onSignOut,
            child: const Text(
              'Sign Out',
              style: TextStyle(color: Colors.white),
            ),
          ),
        ],
      ),
    );
  }
}

class LibraryHistoryLane extends ConsumerWidget {
  final String title;
  final List<Map<String, dynamic>> tracks;
  final bool isLoading;

  const LibraryHistoryLane({
    super.key,
    required this.title,
    required this.tracks,
    this.isLoading = false,
  });

  Future<void> _playTrack(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> track,
  ) async {
    final trackId = extractTrackId(track);
    if (trackId != null) {
      await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
            playlistId: 'history:${title.toLowerCase().replaceAll(' ', '_')}',
            playlistName: title,
            tracks: tracks,
            currentTrack: track,
          );
      if (!context.mounted) return;
    } else {
      final path = track['local_path']?.toString();
      final hasLocalFile = track['is_downloaded_locally'] == true &&
          path != null &&
          path.isNotEmpty;
      if (!hasLocalFile) return;
      final loaded = await ref.read(playbackQueueProvider.notifier).startLocalSession(
            track: track,
            path: path,
          );
      if (!context.mounted) return;
      if (!loaded) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('This downloaded track could not be loaded for playback.'),
            duration: Duration(seconds: 2),
          ),
        );
        return;
      }
    }
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  void _openTrackDetails(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> track,
  ) {
    final trackId = extractTrackId(track);
    if (trackId == null || trackId.isEmpty) return;
    ref.read(trackDetailsProvider.notifier).fetchDetails(trackId);
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => TrackDetailsScreen(track: track)),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (isLoading && tracks.isEmpty) return const SizedBox.shrink();
    if (tracks.isEmpty) return const SizedBox.shrink();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: const TextStyle(
            color: Colors.white,
            fontSize: 24,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 16),
        SizedBox(
          height: 214,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: tracks.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final track = tracks[index];
              final videoId = extractTrackId(track);
              final trackTitle =
                  (track['title'] ?? track['name'] ?? 'Unknown Track')
                      .toString()
                      .trim();
              final trackArtist =
                  (track['author'] ?? track['artist'] ?? track['channel'] ?? '')
                      .toString()
                      .trim();
              return Container(
                width: 182,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.03),
                  borderRadius: BorderRadius.circular(24),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.06),
                    width: 1,
                  ),
                ),
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(24),
                    onTap: () => _playTrack(context, ref, track),
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(18),
                              child: AppArtwork(
                                thumbnail: track['thumbnail'],
                                videoId: videoId,
                                width: double.infinity,
                                height: double.infinity,
                                radius: 18,
                              ),
                            ),
                          ),
                          const SizedBox(height: 12),
                          Text(
                            trackTitle.isEmpty ? 'Unknown Track' : trackTitle,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 15,
                              fontWeight: FontWeight.w700,
                              height: 1.2,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            trackArtist,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.66),
                              fontSize: 12,
                            ),
                          ),
                          const SizedBox(height: 10),
                          Row(
                            children: [
                              InkWell(
                                onTap: () => _playTrack(context, ref, track),
                                borderRadius: BorderRadius.circular(999),
                                child: Container(
                                  width: 38,
                                  height: 38,
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.06),
                                    shape: BoxShape.circle,
                                  ),
                                  child: const Icon(
                                    Icons.play_arrow_rounded,
                                    color: Colors.white,
                                    size: 22,
                                  ),
                                ),
                              ),
                              const Spacer(),
                              IconButton(
                                onPressed: videoId == null
                                    ? null
                                    : () => _openTrackDetails(context, ref, track),
                                icon: Container(
                                  padding: const EdgeInsets.all(8),
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.05),
                                    shape: BoxShape.circle,
                                  ),
                                  child: const Icon(
                                    Icons.info_outline_rounded,
                                    color: Colors.white70,
                                    size: 18,
                                  ),
                                ),
                              ),
                              Consumer(
                                builder: (context, ref, child) {
                                  final task = videoId == null
                                      ? null
                                      : ref.watch(downloadTaskProvider(videoId));
                                  final isActive = task?.phase == DownloadPhase.active;
                                  return IconButton(
                                    icon: Container(
                                      padding: const EdgeInsets.all(8),
                                      decoration: BoxDecoration(
                                        color: Colors.white.withValues(alpha: 0.05),
                                        shape: BoxShape.circle,
                                      ),
                                      child: isActive
                                          ? SizedBox(
                                              width: 18,
                                              height: 18,
                                              child: CircularProgressIndicator(
                                                value: task!.progress > 0
                                                    ? task.progress
                                                    : null,
                                                strokeWidth: 2,
                                                color: const Color(0xFF8E8E93),
                                              ),
                                            )
                                          : Icon(
                                              task?.phase == DownloadPhase.complete
                                                  ? Icons.check_rounded
                                                  : Icons.download_rounded,
                                              color: Colors.white70,
                                              size: 18,
                                            ),
                                    ),
                                    onPressed: videoId == null
                                        ? null
                                        : () {
                                            ref
                                                .read(downloadCenterProvider.notifier)
                                                .downloadTrack(track);
                                          },
                                  );
                                },
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class LibraryPlaylistsSection extends ConsumerWidget {
  final List<Playlist> playlists;
  final ValueChanged<Playlist> onOpenPlaylist;

  const LibraryPlaylistsSection({
    super.key,
    required this.playlists,
    required this.onOpenPlaylist,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Text(
              'Playlists',
              style: TextStyle(
                color: Colors.white,
                fontSize: 24,
                fontWeight: FontWeight.bold,
              ),
            ),
            TextButton.icon(
              onPressed: () {
                final ctrl = TextEditingController();
                showGlassDialog(
                  context: context,
                  title: 'New Playlist',
                  content: TextField(
                    controller: ctrl,
                    style: const TextStyle(color: Colors.white),
                    decoration: const InputDecoration(
                      hintText: 'Playlist Name',
                      hintStyle: TextStyle(color: Colors.white54),
                    ),
                  ),
                  actions: [
                    TextButton(
                      onPressed: () => Navigator.pop(context),
                      child: const Text(
                        'Cancel',
                        style: TextStyle(color: Colors.white54),
                      ),
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
                      child: const Text(
                        'Create',
                        style: TextStyle(color: Colors.white),
                      ),
                    ),
                  ],
                );
              },
              icon: const Icon(Icons.add, color: Colors.white),
              label: const Text(
                'Create',
                style: TextStyle(color: Colors.white),
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        if (playlists.isEmpty)
          const Text(
            'No playlists yet. Create one to get started!',
            style: TextStyle(color: Colors.white54, fontSize: 16),
          )
        else
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: playlists.length,
            itemBuilder: (context, index) {
              final playlist = playlists[index];
              return Container(
                margin: const EdgeInsets.only(bottom: 12),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.03),
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.05),
                    width: 1,
                  ),
                ),
                child: ListTile(
                  onTap: () => onOpenPlaylist(playlist),
                  leading: PlaylistArtworkView(playlist: playlist),
                  title: Text(
                    playlist.name,
                    style: const TextStyle(
                      color: Colors.white,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  subtitle: Text(
                    '${playlist.tracks.length} tracks',
                    style: const TextStyle(color: Colors.white54),
                  ),
                  trailing: PopupMenuButton<String>(
                    icon: const Icon(Icons.more_vert, color: Colors.white70),
                    color: Colors.grey[900],
                    onSelected: (value) {
                      if (value == 'delete') {
                        ref
                            .read(playlistProvider.notifier)
                            .deletePlaylist(playlist.id);
                      } else if (value == 'artwork') {
                        showPlaylistArtworkDialog(
                          context: context,
                          playlist: playlist,
                        );
                      } else if (value == 'rename') {
                        final ctrl = TextEditingController(text: playlist.name);
                        showGlassDialog(
                          context: context,
                          title: 'Rename Playlist',
                          content: TextField(
                            controller: ctrl,
                            style: const TextStyle(color: Colors.white),
                          ),
                          actions: [
                            TextButton(
                              onPressed: () => Navigator.pop(context),
                              child: const Text(
                                'Cancel',
                                style: TextStyle(color: Colors.white54),
                              ),
                            ),
                            TextButton(
                              onPressed: () {
                                if (ctrl.text.trim().isNotEmpty) {
                                  ref
                                      .read(playlistProvider.notifier)
                                      .renamePlaylist(
                                        playlist.id,
                                        ctrl.text.trim(),
                                      );
                                }
                                Navigator.pop(context);
                              },
                              child: const Text(
                                'Save',
                                style: TextStyle(color: Colors.white),
                              ),
                            ),
                          ],
                        );
                      }
                    },
                    itemBuilder: (context) => const [
                      PopupMenuItem(
                        value: 'artwork',
                        child: Text(
                          'Artwork',
                          style: TextStyle(color: Colors.white),
                        ),
                      ),
                      PopupMenuItem(
                        value: 'rename',
                        child: Text(
                          'Rename',
                          style: TextStyle(color: Colors.white),
                        ),
                      ),
                      PopupMenuItem(
                        value: 'delete',
                        child: Text(
                          'Delete',
                          style: TextStyle(color: Colors.red),
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
      ],
    );
  }
}

class SavedTracksSection extends ConsumerWidget {
  final AsyncValue<List<Map<String, dynamic>>> libraryAsync;
  final List<Map<String, dynamic>> missingOfflineTracks;
  final Color accentColor;

  const SavedTracksSection({
    super.key,
    required this.libraryAsync,
    required this.missingOfflineTracks,
    required this.accentColor,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Text(
              'Saved tracks',
              style: TextStyle(
                color: Colors.white,
                fontSize: 24,
                fontWeight: FontWeight.bold,
              ),
            ),
            if (missingOfflineTracks.isNotEmpty)
              TextButton.icon(
                onPressed: () {
                  unawaited(
                    ref
                        .read(downloadCenterProvider.notifier)
                        .downloadTracks(missingOfflineTracks),
                  );
                },
                icon: const Icon(
                  Icons.download_for_offline_rounded,
                  color: Colors.white,
                  size: 18,
                ),
                label: Text(
                  'Download all (${missingOfflineTracks.length})',
                  style: const TextStyle(color: Colors.white),
                ),
              ),
          ],
        ),
        const SizedBox(height: 16),
        libraryAsync.when(
          data: (files) {
            if (files.isEmpty) {
              return const Text(
                'No saved tracks yet.',
                style: TextStyle(color: Colors.white54, fontSize: 16),
              );
            }
            return ListView.builder(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemCount: files.length,
              itemBuilder: (context, index) {
                final track = files[index];
                final name = track['title'] ?? 'Unknown';
                final path = track['local_path']?.toString();
                final isDownloadedLocally =
                    track['is_downloaded_locally'] == true;
                final hasLocalFile =
                    isDownloadedLocally && path != null && path.isNotEmpty;
                final videoId = (track['video_id'] ?? track['id'])?.toString();
                return Container(
                  margin: const EdgeInsets.only(bottom: 12),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.03),
                    borderRadius: BorderRadius.circular(16),
                    border: Border.all(
                      color: Colors.white.withValues(alpha: 0.05),
                      width: 1,
                    ),
                  ),
                  child: Material(
                    color: Colors.transparent,
                    child: InkWell(
                      borderRadius: BorderRadius.circular(16),
                      onTap: () async {
                        final trackId = extractTrackId(track);
                        if (trackId != null) {
                          await ref
                              .read(playbackQueueProvider.notifier)
                              .startPlaylistSession(
                                playlistId: 'library:saved_tracks',
                                playlistName: 'Saved tracks',
                                tracks: files,
                                currentTrack: track,
                              );
                          if (!context.mounted) return;
                          Navigator.of(context).push(
                            MaterialPageRoute(
                              builder: (_) => const FullPlayerScreen(),
                            ),
                          );
                          return;
                        }

                        if (!hasLocalFile) return;
                        final loaded = await ref
                            .read(playbackQueueProvider.notifier)
                            .startLocalSession(
                              track: track,
                              path: path,
                            );
                        if (!context.mounted) return;
                        if (!loaded) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(
                              content: Text(
                                'This downloaded track could not be loaded for playback.',
                              ),
                              duration: Duration(seconds: 2),
                            ),
                          );
                          return;
                        }
                        Navigator.of(context).push(
                          MaterialPageRoute(
                            builder: (_) => const FullPlayerScreen(),
                          ),
                        );
                      },
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Row(
                          children: [
                            AppArtwork(
                              thumbnail: track['thumbnail'],
                              videoId: videoId,
                              width: 70,
                              height: 70,
                              radius: 16,
                            ),
                            const SizedBox(width: 16),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    name,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      color: Colors.white,
                                      fontSize: 16,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 4),
                                  Text(
                                    track['author'] ?? track['artist'] ?? '',
                                    maxLines: 1,
                                    style: TextStyle(
                                      color: Colors.white.withValues(alpha: 0.6),
                                      fontSize: 13,
                                    ),
                                  ),
                                  if (!hasLocalFile)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 4),
                                      child: Text(
                                        'Saved to account - download to listen offline',
                                        style: TextStyle(
                                          color: Colors.white.withValues(alpha: 0.45),
                                          fontSize: 11,
                                          fontWeight: FontWeight.w600,
                                        ),
                                      ),
                                    ),
                                ],
                              ),
                            ),
                            Consumer(
                              builder: (context, ref, child) {
                                final downloadTask = videoId == null
                                    ? null
                                    : ref.watch(downloadTaskProvider(videoId));
                                final isDownloading =
                                    downloadTask?.phase == DownloadPhase.active;
                                final isDownloadedNow = hasLocalFile ||
                                    downloadTask?.phase == DownloadPhase.complete;
                                return Column(
                                  crossAxisAlignment: CrossAxisAlignment.end,
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  children: [
                                    Row(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        if (videoId != null)
                                          IconButton(
                                            icon: Container(
                                              padding: const EdgeInsets.all(6),
                                              decoration: BoxDecoration(
                                                color: Colors.white.withValues(alpha: 0.08),
                                                shape: BoxShape.circle,
                                              ),
                                              child: const Icon(
                                                Icons.info_outline_rounded,
                                                color: Colors.white70,
                                                size: 20,
                                              ),
                                            ),
                                            onPressed: () {
                                              ref
                                                  .read(trackDetailsProvider.notifier)
                                                  .fetchDetails(videoId);
                                              Navigator.of(context).push(
                                                MaterialPageRoute(
                                                  builder: (_) => TrackDetailsScreen(
                                                    track: track,
                                                  ),
                                                ),
                                              );
                                            },
                                          ),
                                        if (!hasLocalFile && videoId != null)
                                          IconButton(
                                            icon: Container(
                                              padding: const EdgeInsets.all(6),
                                              decoration: BoxDecoration(
                                                color: Colors.white.withValues(alpha: 0.08),
                                                shape: BoxShape.circle,
                                              ),
                                              child: isDownloading
                                                  ? SizedBox(
                                                      width: 20,
                                                      height: 20,
                                                      child: CircularProgressIndicator(
                                                        value: downloadTask!.progress > 0
                                                            ? downloadTask.progress
                                                            : null,
                                                        strokeWidth: 2,
                                                        color: accentColor,
                                                      ),
                                                    )
                                                  : Icon(
                                                      isDownloadedNow
                                                          ? Icons.check_rounded
                                                          : Icons.download_for_offline_rounded,
                                                      color: Colors.white70,
                                                      size: 20,
                                                    ),
                                            ),
                                            onPressed: isDownloading
                                                ? null
                                                : () {
                                                    unawaited(
                                                      ref
                                                          .read(downloadCenterProvider.notifier)
                                                          .downloadTrack(track),
                                                    );
                                                  },
                                          ),
                                        IconButton(
                                          icon: Container(
                                            padding: const EdgeInsets.all(6),
                                            decoration: BoxDecoration(
                                              color: Colors.redAccent.withValues(alpha: 0.1),
                                              shape: BoxShape.circle,
                                            ),
                                            child: const Icon(
                                              Icons.delete_outline_rounded,
                                              color: Colors.redAccent,
                                              size: 20,
                                            ),
                                          ),
                                          onPressed: () {
                                            try {
                                              if (hasLocalFile) {
                                                File(path).deleteSync();
                                                final jsonPath = path.replaceAll('.mp3', '.json');
                                                if (File(jsonPath).existsSync()) {
                                                  File(jsonPath).deleteSync();
                                                }
                                              }
                                              unawaited(removeCloudLibraryTrack(videoId));
                                              ref.invalidate(libraryProvider);
                                            } catch (_) {
                                              // Best effort cleanup.
                                            }
                                          },
                                        ),
                                      ],
                                    ),
                                    if (track['filesize'] != null && track['filesize'] > 0)
                                      Padding(
                                        padding: const EdgeInsets.only(right: 8),
                                        child: Text(
                                          '${(track['filesize'] / 1024 / 1024).toStringAsFixed(1)} MB',
                                          style: TextStyle(
                                            color: Colors.white.withValues(alpha: 0.3),
                                            fontSize: 10,
                                            fontWeight: FontWeight.w600,
                                          ),
                                        ),
                                      ),
                                  ],
                                );
                              },
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                );
              },
            );
          },
          loading: () => const Center(
            child: CircularProgressIndicator(color: Colors.white),
          ),
          error: (err, stack) => Center(
            child: Text(
              'Error: $err',
              style: const TextStyle(color: Colors.red),
            ),
          ),
        ),
      ],
    );
  }
}
