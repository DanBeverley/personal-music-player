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
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';

class LibraryReferenceHeader extends StatelessWidget {
  const LibraryReferenceHeader({
    super.key,
    required this.onCreatePlaylist,
    required this.onSearch,
    required this.selectedFilter,
    required this.onFilterSelected,
  });

  final VoidCallback onCreatePlaylist;
  final VoidCallback onSearch;
  final String selectedFilter;
  final ValueChanged<String> onFilterSelected;

  @override
  Widget build(BuildContext context) {
    const filters = ['Playlists', 'Albums', 'Artists', 'Downloaded'];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Expanded(
              child: Text(
                'Library',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 26,
                  fontWeight: FontWeight.w800,
                  letterSpacing: -0.7,
                ),
              ),
            ),
            IconButton(
              onPressed: onSearch,
              icon: const Icon(Icons.search_rounded),
              color: Colors.white,
            ),
            IconButton(
              onPressed: onCreatePlaylist,
              icon: const Icon(Icons.add_rounded),
              color: Colors.white,
            ),
          ],
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final filter in filters)
              NeatiePill(
                label: filter,
                selected: selectedFilter == filter,
                onTap: () => onFilterSelected(filter),
              ),
          ],
        ),
      ],
    );
  }
}

class LibraryEmptyStateCard extends StatelessWidget {
  const LibraryEmptyStateCard({
    super.key,
    required this.title,
    required this.body,
  });

  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.025),
      child: Row(
        children: [
          Container(
            width: 46,
            height: 46,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(14),
            ),
            child: const Icon(
              Icons.library_music_rounded,
              color: Colors.white,
              size: 22,
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 15,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  body,
                  style: const TextStyle(
                    color: neatieMutedText,
                    fontSize: 12,
                    height: 1.35,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class LibraryQuickAccessSection extends StatelessWidget {
  const LibraryQuickAccessSection({
    super.key,
    required this.savedCount,
    required this.downloadedCount,
  });

  final int savedCount;
  final int downloadedCount;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        _LibraryQuickAccessTile(
          title: 'Liked Songs',
          subtitle: '$savedCount songs',
          icon: Icons.favorite_rounded,
          gradient: const [Color(0xFF6E4BFF), Color(0xFF8D6BFF)],
        ),
        const SizedBox(height: 12),
        _LibraryQuickAccessTile(
          title: 'Downloaded',
          subtitle: '$downloadedCount songs',
          icon: Icons.download_done_rounded,
          gradient: const [Color(0xFF0EA45B), Color(0xFF158047)],
        ),
      ],
    );
  }
}

class _LibraryQuickAccessTile extends StatelessWidget {
  const _LibraryQuickAccessTile({
    required this.title,
    required this.subtitle,
    required this.icon,
    required this.gradient,
  });

  final String title;
  final String subtitle;
  final IconData icon;
  final List<Color> gradient;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 48,
          height: 48,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(10),
            gradient: LinearGradient(colors: gradient),
          ),
          child: Icon(icon, color: Colors.white, size: 25),
        ),
        const SizedBox(width: 14),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 15,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 3),
              Text(
                subtitle,
                style: const TextStyle(color: neatieMutedText, fontSize: 12),
              ),
            ],
          ),
        ),
      ],
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
      final loaded =
          await ref.read(playbackQueueProvider.notifier).startLocalSession(
                track: track,
                path: path,
              );
      if (!context.mounted) return;
      if (!loaded) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content:
                Text('This downloaded track could not be loaded for playback.'),
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
            fontSize: 19,
            fontWeight: FontWeight.w800,
          ),
        ),
        const SizedBox(height: 12),
        SizedBox(
          height: 174,
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
              return NeatieSurface(
                margin: EdgeInsets.zero,
                radius: 18,
                color: Colors.white.withValues(alpha: 0.035),
                blur: false,
                padding: EdgeInsets.zero,
                width: 132,
                child: InkWell(
                    borderRadius: BorderRadius.circular(18),
                    onTap: () => _playTrack(context, ref, track),
                    child: Padding(
                      padding: const EdgeInsets.all(9),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(12),
                              child: AppArtwork(
                                thumbnail: track['thumbnail'],
                                videoId: videoId,
                                width: double.infinity,
                                height: double.infinity,
                                radius: 12,
                              ),
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            trackTitle.isEmpty ? 'Unknown Track' : trackTitle,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 12.5,
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
                              fontSize: 11,
                            ),
                          ),
                          const SizedBox(height: 6),
                          Row(
                            children: [
                              InkWell(
                                onTap: () => _playTrack(context, ref, track),
                                borderRadius: BorderRadius.circular(999),
                                child: Container(
                                  width: 30,
                                  height: 30,
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.06),
                                    shape: BoxShape.circle,
                                  ),
                                  child: const Icon(
                                    Icons.play_arrow_rounded,
                                    color: Colors.white,
                                    size: 18,
                                  ),
                                ),
                              ),
                              const Spacer(),
                              InkWell(
                                onTap: videoId == null
                                    ? null
                                    : () =>
                                        _openTrackDetails(context, ref, track),
                                borderRadius: BorderRadius.circular(999),
                                child: Container(
                                  width: 30,
                                  height: 30,
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
                              const SizedBox(width: 4),
                              Consumer(
                                builder: (context, ref, child) {
                                  final task = videoId == null
                                      ? null
                                      : ref
                                          .watch(downloadTaskProvider(videoId));
                                  final isActive =
                                      task?.phase == DownloadPhase.active;
                                  return InkWell(
                                    borderRadius: BorderRadius.circular(999),
                                    child: Container(
                                      width: 30,
                                      height: 30,
                                      decoration: BoxDecoration(
                                        color: Colors.white
                                            .withValues(alpha: 0.05),
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
                                              task?.phase ==
                                                      DownloadPhase.complete
                                                  ? Icons.check_rounded
                                                  : Icons.download_rounded,
                                              color: Colors.white70,
                                              size: 18,
                                            ),
                                    ),
                                    onTap: videoId == null
                                        ? null
                                        : () {
                                            ref
                                                .read(downloadCenterProvider
                                                    .notifier)
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
                fontSize: 19,
                fontWeight: FontWeight.w800,
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
        const SizedBox(height: 12),
        if (playlists.isEmpty)
          const Text(
            'No playlists yet. Create one to get started.',
            style: TextStyle(color: neatieMutedText, fontSize: 13),
          )
        else
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: playlists.length,
            itemBuilder: (context, index) {
              final playlist = playlists[index];
              return NeatieSurface(
                margin: const EdgeInsets.only(bottom: 12),
                radius: neatieRadiusMedium,
                color: Colors.white.withValues(alpha: 0.035),
                blur: false,
                padding: EdgeInsets.zero,
                child: InkWell(
                  borderRadius: BorderRadius.circular(neatieRadiusMedium),
                    onTap: () => onOpenPlaylist(playlist),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 10,
                    ),
                    child: Row(
                      children: [
                        PlaylistArtworkView(playlist: playlist),
                        const SizedBox(width: 16),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                playlist.name,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                '${playlist.tracks.length} tracks',
                                style: const TextStyle(color: neatieMutedText),
                              ),
                            ],
                          ),
                        ),
                        PopupMenuButton<String>(
                      icon: const Icon(
                        Icons.more_vert,
                        color: neatieMutedText,
                      ),
                      color: neatieRaised,
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
                          final ctrl =
                              TextEditingController(text: playlist.name);
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
                      ],
                    ),
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
                fontSize: 19,
                fontWeight: FontWeight.w800,
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
        const SizedBox(height: 12),
        libraryAsync.when(
          data: (files) {
            if (files.isEmpty) {
              return const Text(
                'No saved tracks yet.',
                style: TextStyle(color: neatieMutedText, fontSize: 13),
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
                return NeatieSurface(
                  margin: const EdgeInsets.only(bottom: 12),
                  radius: neatieRadiusMedium,
                  color: Colors.white.withValues(alpha: 0.035),
                  blur: false,
                  child: InkWell(
                      borderRadius: BorderRadius.circular(neatieRadiusMedium),
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
                              width: 54,
                              height: 54,
                              radius: 10,
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
                                      fontSize: 14,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 4),
                                  Text(
                                    track['author'] ?? track['artist'] ?? '',
                                    maxLines: 1,
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.6),
                                      fontSize: 12,
                                    ),
                                  ),
                                  if (!hasLocalFile)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 4),
                                      child: Text(
                                        'Saved to account - download to listen offline',
                                        style: TextStyle(
                                          color: Colors.white
                                              .withValues(alpha: 0.45),
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
                                    downloadTask?.phase ==
                                        DownloadPhase.complete;
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
                                                color: Colors.white
                                                    .withValues(alpha: 0.08),
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
                                                  .read(trackDetailsProvider
                                                      .notifier)
                                                  .fetchDetails(videoId);
                                              Navigator.of(context).push(
                                                MaterialPageRoute(
                                                  builder: (_) =>
                                                      TrackDetailsScreen(
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
                                                color: Colors.white
                                                    .withValues(alpha: 0.08),
                                                shape: BoxShape.circle,
                                              ),
                                              child: isDownloading
                                                  ? SizedBox(
                                                      width: 20,
                                                      height: 20,
                                                      child:
                                                          CircularProgressIndicator(
                                                        value: downloadTask!
                                                                    .progress >
                                                                0
                                                            ? downloadTask
                                                                .progress
                                                            : null,
                                                        strokeWidth: 2,
                                                        color: accentColor,
                                                      ),
                                                    )
                                                  : Icon(
                                                      isDownloadedNow
                                                          ? Icons.check_rounded
                                                          : Icons
                                                              .download_for_offline_rounded,
                                                      color: Colors.white70,
                                                      size: 20,
                                                    ),
                                            ),
                                            onPressed: isDownloading
                                                ? null
                                                : () {
                                                    unawaited(
                                                      ref
                                                          .read(
                                                              downloadCenterProvider
                                                                  .notifier)
                                                          .downloadTrack(track),
                                                    );
                                                  },
                                          ),
                                        IconButton(
                                          icon: Container(
                                            padding: const EdgeInsets.all(6),
                                            decoration: BoxDecoration(
                                              color: Colors.redAccent
                                                  .withValues(alpha: 0.1),
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
                                                final jsonPath =
                                                    path.replaceAll(
                                                        '.mp3', '.json');
                                                if (File(jsonPath)
                                                    .existsSync()) {
                                                  File(jsonPath).deleteSync();
                                                }
                                              }
                                              unawaited(removeCloudLibraryTrack(
                                                  videoId));
                                              ref.invalidate(libraryProvider);
                                            } catch (_) {
                                              // Best effort cleanup.
                                            }
                                          },
                                        ),
                                      ],
                                    ),
                                    if (track['filesize'] != null &&
                                        track['filesize'] > 0)
                                      Padding(
                                        padding:
                                            const EdgeInsets.only(right: 8),
                                        child: Text(
                                          '${(track['filesize'] / 1024 / 1024).toStringAsFixed(1)} MB',
                                          style: TextStyle(
                                            color: Colors.white
                                                .withValues(alpha: 0.3),
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
