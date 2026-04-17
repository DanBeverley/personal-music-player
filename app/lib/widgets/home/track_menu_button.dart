import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../logic/audio_provider_queue.dart';
import '../../logic/download_provider.dart';
import '../app_artwork.dart';

typedef TrackActionCallback = Future<void> Function(Map<String, dynamic> track);

String _trackActionTitle(Map<String, dynamic> track) {
  final title =
      (track['title'] ?? track['name'] ?? track['song'])?.toString().trim() ??
          '';
  return title.isEmpty ? 'Unknown Track' : title;
}

String _trackActionArtist(Map<String, dynamic> track) {
  return (track['channel'] ?? track['author'] ?? track['artist'])
          ?.toString()
          .trim() ??
      'Unknown Artist';
}

void _showTrackActionFeedback(BuildContext context, String message) {
  final messenger = ScaffoldMessenger.maybeOf(context);
  if (messenger == null) return;
  messenger
    ..hideCurrentSnackBar()
    ..showSnackBar(
      SnackBar(
        content: Text(message),
        behavior: SnackBarBehavior.floating,
      ),
    );
}

Future<void> showTrackActionSheet({
  required BuildContext context,
  required WidgetRef ref,
  required Map<String, dynamic> track,
  required VoidCallback onOpenDetails,
  required TrackActionCallback onAddToPlaylist,
  required TrackActionCallback onStartStation,
}) async {
  final normalizedTrack = Map<String, dynamic>.from(track);
  final videoId = extractTrackId(normalizedTrack);
  final title = _trackActionTitle(normalizedTrack);
  final artist = _trackActionArtist(normalizedTrack);
  final task = videoId == null ? null : ref.read(downloadTaskProvider(videoId));
  final isDownloadActive = task?.phase == DownloadPhase.active;
  final isDownloaded = task?.phase == DownloadPhase.complete;

  await showModalBottomSheet<void>(
    context: context,
    backgroundColor: Colors.transparent,
    isScrollControlled: true,
    builder: (sheetContext) {
      Future<void> closeAndRun(
        Future<void> Function() action, {
        String? message,
      }) async {
        Navigator.of(sheetContext).pop();
        await action();
        if (context.mounted && message != null && message.isNotEmpty) {
          _showTrackActionFeedback(context, message);
        }
      }

      Future<void> closeAndOpen(
        Future<void> Function(Map<String, dynamic> track) action,
      ) async {
        Navigator.of(sheetContext).pop();
        await Future<void>.delayed(const Duration(milliseconds: 120));
        if (!context.mounted) return;
        await action(normalizedTrack);
      }

      return SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
          child: Container(
            decoration: BoxDecoration(
              color: const Color(0xFF111315),
              borderRadius: const BorderRadius.vertical(
                top: Radius.circular(30),
                bottom: Radius.circular(26),
              ),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.32),
                  blurRadius: 30,
                  offset: const Offset(0, 18),
                ),
              ],
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Center(
                  child: Container(
                    margin: const EdgeInsets.only(top: 10),
                    width: 48,
                    height: 4,
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.24),
                      borderRadius: BorderRadius.circular(999),
                    ),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(18, 18, 18, 14),
                  child: Row(
                    children: [
                      AppArtwork(
                        thumbnail: normalizedTrack['thumbnail'],
                        videoId: videoId,
                        width: 76,
                        height: 76,
                        radius: 22,
                      ),
                      const SizedBox(width: 16),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              title,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 21,
                                fontWeight: FontWeight.w800,
                                height: 1.1,
                              ),
                            ),
                            const SizedBox(height: 6),
                            Text(
                              artist,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.62),
                                fontSize: 15,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                Divider(
                  height: 1,
                  color: Colors.white.withValues(alpha: 0.08),
                ),
                _TrackActionTile(
                  icon: Icons.queue_play_next_rounded,
                  label: 'Play next',
                  onTap: () => closeAndRun(
                    () => ref
                        .read(playbackQueueProvider.notifier)
                        .enqueueTrack(normalizedTrack, playNext: true),
                    message: 'Queued to play next',
                  ),
                ),
                _TrackActionTile(
                  icon: Icons.playlist_play_rounded,
                  label: 'Play last',
                  onTap: () => closeAndRun(
                    () => ref
                        .read(playbackQueueProvider.notifier)
                        .enqueueTrack(normalizedTrack, playNext: false),
                    message: 'Added to the end of the queue',
                  ),
                ),
                _TrackActionTile(
                  icon: Icons.playlist_add_rounded,
                  label: 'Add to playlist',
                  onTap: () => closeAndOpen(onAddToPlaylist),
                ),
                _TrackActionTile(
                  icon: Icons.radio_rounded,
                  label: 'Start station',
                  onTap: () => closeAndOpen(onStartStation),
                ),
                _TrackActionTile(
                  icon: isDownloaded
                      ? Icons.check_circle_outline_rounded
                      : Icons.download_rounded,
                  label: isDownloadActive
                      ? 'Downloading'
                      : isDownloaded
                          ? 'Downloaded'
                          : 'Download',
                  enabled: videoId != null && !isDownloadActive,
                  onTap: videoId == null || isDownloadActive
                      ? null
                      : () => closeAndRun(
                            () => ref
                                .read(downloadCenterProvider.notifier)
                                .downloadTrack(normalizedTrack),
                            message: isDownloaded
                                ? 'Track already downloaded'
                                : 'Download started',
                          ),
                ),
                Divider(
                  height: 1,
                  color: Colors.white.withValues(alpha: 0.08),
                ),
                _TrackActionTile(
                  icon: Icons.info_outline_rounded,
                  label: 'View details',
                  enabled: videoId != null && videoId.isNotEmpty,
                  onTap: videoId == null || videoId.isEmpty
                      ? null
                      : () => closeAndRun(() async => onOpenDetails()),
                ),
                const SizedBox(height: 8),
              ],
            ),
          ),
        ),
      );
    },
  );
}

class TrackMenuButton extends ConsumerWidget {
  final Map<String, dynamic> track;
  final VoidCallback onOpenDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
  final double buttonSize;
  final double iconSize;

  const TrackMenuButton({
    super.key,
    required this.track,
    required this.onOpenDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.buttonSize = 42,
    this.iconSize = 20,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return IconButton(
      onPressed: () => showTrackActionSheet(
        context: context,
        ref: ref,
        track: track,
        onOpenDetails: onOpenDetails,
        onAddToPlaylist: onAddToPlaylist,
        onStartStation: onStartStation,
      ),
      icon: Container(
        width: buttonSize,
        height: buttonSize,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.05),
          shape: BoxShape.circle,
        ),
        child: Icon(
          Icons.more_horiz_rounded,
          color: Colors.white70,
          size: iconSize,
        ),
      ),
      padding: EdgeInsets.zero,
      constraints: const BoxConstraints(),
      splashRadius: buttonSize * 0.6,
      visualDensity: VisualDensity.compact,
    );
  }
}

class _TrackActionTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final bool enabled;

  const _TrackActionTile({
    required this.icon,
    required this.label,
    required this.onTap,
    this.enabled = true,
  });

  @override
  Widget build(BuildContext context) {
    final foreground =
        enabled ? Colors.white : Colors.white.withValues(alpha: 0.28);
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: enabled ? onTap : null,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 15),
          child: Row(
            children: [
              Icon(icon, color: foreground, size: 22),
              const SizedBox(width: 16),
              Expanded(
                child: Text(
                  label,
                  style: TextStyle(
                    color: foreground,
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
