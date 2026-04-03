part of 'main.dart';

class _LibraryStatChip extends StatelessWidget {
  final String label;

  const _LibraryStatChip({required this.label});

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

class _LibraryHistoryLane extends ConsumerWidget {
  final String title;
  final List<Map<String, dynamic>> tracks;
  final bool isLoading;

  const _LibraryHistoryLane({
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

  void _openTrackDetails(BuildContext context, WidgetRef ref, Map<String, dynamic> track) {
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
                  borderRadius: BorderRadius.circular(_radiusLarge),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.06),
                    width: 1,
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: Colors.black.withValues(alpha: 0.18),
                      blurRadius: 16,
                      offset: const Offset(0, 8),
                    ),
                  ],
                ),
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(_radiusLarge),
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
                                                color: _accentGrey,
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

class LibraryScreen extends ConsumerWidget {
  const LibraryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final authState = ref.watch(authProvider);
    final libraryAsync = ref.watch(libraryProvider);
    final playlists = ref.watch(playlistProvider);
    final lastPlayedTracks = ref.watch(lastPlayedProvider);
    final isLastPlayedLoading = ref.watch(lastPlayedProvider.notifier).isLoading;
    final offlineTracks = libraryAsync.valueOrNull ?? const <Map<String, dynamic>>[];
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
    final avatarUrl = authState.user?.userMetadata?['avatar_url']?.toString() ??
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
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(18),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.03),
                  borderRadius: BorderRadius.circular(_radiusLarge),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.08),
                  ),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.06),
                        borderRadius: BorderRadius.circular(_radiusMedium),
                      ),
                      alignment: Alignment.center,
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(_radiusMedium),
                        child: avatarUrl != null && avatarUrl.isNotEmpty
                            ? Image.network(
                                avatarUrl,
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
                            authState.user?.email ?? 'Signed in',
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
                              _LibraryStatChip(label: providerName.toUpperCase()),
                              _LibraryStatChip(label: '${playlists.length} playlists'),
                              _LibraryStatChip(label: '${downloadedOfflineTracks.length} offline'),
                            ],
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    TextButton(
                      onPressed: authState.isBusy
                          ? null
                          : () async {
                              await ref
                                  .read(audioPlayerProvider.notifier)
                                  .stopPlayback();
                              ref
                                  .read(playbackQueueProvider.notifier)
                                  .clearSession();
                              await ref.read(authProvider.notifier).signOut();
                            },
                      child: const Text(
                        'Sign Out',
                        style: TextStyle(color: Colors.white),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 24),
            ],
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Playlists', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
                TextButton.icon(
                  onPressed: () {
                    final ctrl = TextEditingController();
                    showGlassDialog(
                      context: context,
                      title: 'New Playlist',
                      content: TextField(
                        controller: ctrl,
                        style: const TextStyle(color: Colors.white),
                        decoration: const InputDecoration(hintText: 'Playlist Name', hintStyle: TextStyle(color: Colors.white54)),
                      ),
                      actions: [
                        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                        TextButton(onPressed: () {
                          if (ctrl.text.trim().isNotEmpty) {
                            ref.read(playlistProvider.notifier).createPlaylist(ctrl.text.trim());
                          }
                          Navigator.pop(context);
                        }, child: const Text('Create', style: TextStyle(color: Colors.white)))
                      ]
                    );
                  }, 
                  icon: const Icon(Icons.add, color: Colors.white), 
                  label: const Text('Create', style: TextStyle(color: Colors.white))
                )
              ],
            ),
            const SizedBox(height: 16),
            if (playlists.isEmpty)
              const Text('No playlists yet. Create one to get started!', style: TextStyle(color: Colors.white54, fontSize: 16))
            else
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: playlists.length,
                itemBuilder: (context, index) {
                  final p = playlists[index];
                  return Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.03),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                      boxShadow: [
                        BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))
                      ]
                    ),
                    child: ListTile(
                      onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => PlaylistDetailScreen(playlistId: p.id))),
                      leading: PlaylistArtworkView(playlist: p),
                      title: Text(p.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
                      subtitle: Text('${p.tracks.length} tracks', style: const TextStyle(color: Colors.white54)),
                      trailing: PopupMenuButton<String>(
                        icon: const Icon(Icons.more_vert, color: Colors.white70),
                        color: Colors.grey[900],
                        onSelected: (val) {
                          if (val == 'delete') {
                            ref.read(playlistProvider.notifier).deletePlaylist(p.id);
                          } else if (val == 'artwork') {
                            showPlaylistArtworkDialog(
                              context: context,
                              playlist: p,
                            );
                          } else if (val == 'rename') {
                            final ctrl = TextEditingController(text: p.name);
                            showGlassDialog(
                              context: context,
                              title: 'Rename Playlist',
                              content: TextField(
                                controller: ctrl,
                                style: const TextStyle(color: Colors.white),
                              ),
                              actions: [
                                TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                                TextButton(onPressed: () {
                                  if (ctrl.text.trim().isNotEmpty) {
                                    ref.read(playlistProvider.notifier).renamePlaylist(p.id, ctrl.text.trim());
                                  }
                                  Navigator.pop(context);
                                }, child: const Text('Save', style: TextStyle(color: Colors.white)))
                              ]
                            );
                          }
                        },
                        itemBuilder: (BuildContext context) => [
                          const PopupMenuItem(value: 'artwork', child: Text('Artwork', style: TextStyle(color: Colors.white))),
                          const PopupMenuItem(value: 'rename', child: Text('Rename', style: TextStyle(color: Colors.white))),
                          const PopupMenuItem(value: 'delete', child: Text('Delete', style: TextStyle(color: Colors.red))),
                        ],
                      ),
                    ),
                  );
                },
              ),
            
            const SizedBox(height: 32),
            if (lastPlayedTracks.isNotEmpty) ...[
              _LibraryHistoryLane(
                title: 'Last played',
                tracks: lastPlayedTracks,
                isLoading: isLastPlayedLoading,
              ),
              const SizedBox(height: 32),
            ],
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
                  return const Text('No saved tracks yet.', style: TextStyle(color: Colors.white54, fontSize: 16));
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
                    final videoId =
                        (track['video_id'] ?? track['id'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(16),
                        border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                        boxShadow: [BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))]
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
                                      'This downloaded track could not be loaded for playback.'),
                                  duration: Duration(seconds: 2),
                                ),
                              );
                              return;
                            }
                            Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                          },
                          child: Padding(
                            padding: const EdgeInsets.all(12.0),
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
                                      Text(name, maxLines: 1, overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
                                      const SizedBox(height: 4),
                                      Text(track['author'] ?? track['artist'] ?? '', maxLines: 1, style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
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
                                    final isDownloadedNow =
                                        hasLocalFile ||
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
                                                            color: _accentGrey,
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
                                                } catch (e) {
                                                  // Ignored mapping
                                                }
                                              },
                                            ),
                                          ],
                                        ),
                                        if (track['filesize'] != null && track['filesize'] > 0)
                                          Padding(
                                            padding: const EdgeInsets.only(right: 8.0),
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
              loading: () => const Center(child: CircularProgressIndicator(color: Colors.white)),
              error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
            ),
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}

class PlaylistDetailScreen extends ConsumerStatefulWidget {
  final String playlistId;
  const PlaylistDetailScreen({super.key, required this.playlistId});
  @override
  ConsumerState<PlaylistDetailScreen> createState() => _PlaylistDetailScreenState();
}

class _PlaylistDetailScreenState extends ConsumerState<PlaylistDetailScreen> {
  final _searchCtrl = TextEditingController();
  List<dynamic> _searchResults = [];
  bool _isSearching = false;
  final Set<String> _primedPlaylistIds = <String>{};
  int _searchRequestVersion = 0;

  void _primePlaylistTracks(Iterable<dynamic> tracks, {int limit = 8}) {
    final idsToWarm = <String>[];
    for (final track in tracks.take(limit)) {
      final videoId = (track['id'] ?? track['videoId'])?.toString();
      if (videoId == null || videoId.isEmpty) continue;
      if (_primedPlaylistIds.add(videoId)) {
        idsToWarm.add(videoId);
      }
    }
    if (idsToWarm.isEmpty) return;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(idsToWarm));
  }

  Future<void> _search(String q) async {
    if (q.isEmpty) return;
    final requestVersion = ++_searchRequestVersion;
    setState(() => _isSearching = true);
    try {
      final res = await http.post(
        buildProxyUri('/search'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({"query": q}),
      );
      if (requestVersion != _searchRequestVersion) return;
      if (res.statusCode == 200) {
        final results = jsonDecode(res.body)['results'] ?? [];
        setState(() => _searchResults = results);
        _primePlaylistTracks(results, limit: 8);
      } else {
        setState(() => _searchResults = []);
      }
    } catch (e) {
      // Ignored
    }
    if (requestVersion != _searchRequestVersion || !mounted) return;
    setState(() => _isSearching = false);
  }

  @override
  Widget build(BuildContext context) {
    final playlists = ref.watch(playlistProvider);
    final playlist = playlists.firstWhere((p) => p.id == widget.playlistId, orElse: () => Playlist(id: '', name: 'Deleted Playlist', tracks: []));
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primePlaylistTracks(playlist.tracks);
    });

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(backgroundColor: Colors.transparent, title: Text(playlist.name, style: const TextStyle(color: Colors.white))),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.03),
                borderRadius: BorderRadius.circular(_radiusLarge),
                border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
              ),
              child: Row(
                children: [
                  PlaylistArtworkView(
                    playlist: playlist,
                    size: 96,
                    radius: 22,
                    onTap: () => showPlaylistArtworkDialog(
                      context: context,
                      playlist: playlist,
                    ),
                  ),
                  const SizedBox(width: 18),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          playlist.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 24,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          '${playlist.tracks.length} tracks'
                          '${playlist.tracks.any(isTrackHidden) ? ' • ${playlist.tracks.where(isTrackHidden).length} skipped' : ''}',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.56),
                            fontSize: 13,
                          ),
                        ),
                        const SizedBox(height: 14),
                        TextButton.icon(
                          onPressed: () => showPlaylistArtworkDialog(
                            context: context,
                            playlist: playlist,
                          ),
                          icon: const Icon(Icons.photo_outlined, size: 18),
                          label: const Text('Edit artwork'),
                          style: TextButton.styleFrom(
                            foregroundColor: Colors.white,
                            backgroundColor: Colors.white.withValues(alpha: 0.05),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 14,
                              vertical: 12,
                            ),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(18),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _searchCtrl,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: "Let's find something for your playlist",
                hintStyle: const TextStyle(color: Colors.white54),
                filled: true,
                fillColor: _surfaceGrey,
                suffixIcon: IconButton(icon: const Icon(Icons.search, color: Colors.white), onPressed: () => _search(_searchCtrl.text)),
              ),
              onSubmitted: _search,
            ),
            const SizedBox(height: 16),
            if (_isSearching) const Center(child: CircularProgressIndicator(color: Colors.white)),
            if (_searchResults.isNotEmpty) ...[
              const Text('Search Results', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              ListView.builder(
                 shrinkWrap: true,
                 physics: const NeverScrollableScrollPhysics(),
                 itemCount: _searchResults.length,
                 itemBuilder: (context, i) {
                   final t = _searchResults[i];
                   final videoId = (t['id'] ?? t['videoId'])?.toString();
                   return ListTile(
                     leading: AppArtwork(
                       thumbnail: t['thumbnail'],
                       videoId: videoId,
                       width: 50,
                       height: 50,
                       radius: 14,
                     ),
                     title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                     subtitle: Text(t['channel'] ?? '', style: const TextStyle(color: Colors.white54)),
                     trailing: IconButton(
                       icon: const Icon(Icons.add_circle_outline, color: Colors.white),
                       onPressed: () {
                         ref.read(playlistProvider.notifier).addTrackToPlaylist(widget.playlistId, t);
                         ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added to ${playlist.name}'), duration: const Duration(seconds: 1)));
                       }
                     )
                   );
                 }
              ),
              const Divider(color: Colors.white24, height: 32),
            ],
            
            const Text('Playlist Tracks', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            if (playlist.tracks.isEmpty) const Text('No tracks added yet.', style: TextStyle(color: Colors.white54)),
            ListView.builder(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemCount: playlist.tracks.length,
              itemBuilder: (context, i) {
                 final t = playlist.tracks[i];
                 final videoId = (t['id'] ?? t['videoId'])?.toString();
                 return ListTile(
                    onTap: () {
                        unawaited(ref.read(playbackQueueProvider.notifier).startPlaylistSession(
                          playlistId: widget.playlistId,
                          playlistName: playlist.name,
                          tracks: playlist.tracks,
                          currentTrack: t,
                        ));
                        Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                    },
                    leading: AppArtwork(
                      thumbnail: t['thumbnail'],
                      videoId: videoId,
                      width: 50,
                      height: 50,
                      radius: 14,
                    ),
                    title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                    subtitle: Text(t['channel'] ?? t['author'] ?? '', style: const TextStyle(color: Colors.white54)),
                    trailing: PopupMenuButton<String>(
                      icon: const Icon(Icons.more_vert, color: Colors.white70),
                      color: Colors.grey[900],
                      onSelected: (val) {
                        if (val == 'remove') {
                          ref.read(playlistProvider.notifier).removeTrackFromPlaylist(widget.playlistId, t['id'] ?? t['videoId']);
                        }
                      },
                      itemBuilder: (_) => [const PopupMenuItem(value: 'remove', child: Text('Remove from Playlist', style: TextStyle(color: Colors.red)))]
                    )
                 );
              }
            )
          ]
        )
      )
    );
  }
}

