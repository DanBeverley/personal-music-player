import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../logic/audio_provider_queue.dart';
import '../../logic/download_provider.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';

typedef TrackActionCallback = Future<void> Function(Map<String, dynamic> track);

enum _TrackMenuAction {
  addToPlaylist,
  startStation,
  download,
  removeFromFeed,
  details,
}

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

      return NeatieBottomSheetSurface(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const NeatieSheetHandle(),
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
                _TrackActionTile(
                  icon: Icons.block_rounded,
                  label: 'Remove from feed',
                  enabled: videoId != null && videoId.isNotEmpty,
                  onTap: videoId == null || videoId.isEmpty
                      ? null
                      : () => closeAndRun(
                            () async {
                              await recordProxyInteractionEvent(
                                'remove_from_feed',
                                trackId: videoId,
                                rawTrack: normalizedTrack,
                                metadata: const <String, dynamic>{
                                  'reason': 'remove_from_feed',
                                },
                              );
                              notifyRecommendationSignal('remove_from_feed');
                            },
                            message: 'Removed from future recommendations',
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
      );
    },
  );
}

class TrackMenuButton extends ConsumerStatefulWidget {
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
  ConsumerState<TrackMenuButton> createState() => _TrackMenuButtonState();
}

class _TrackMenuButtonState extends ConsumerState<TrackMenuButton> {
  bool _isOpen = false;

  @override
  Widget build(BuildContext context) {
    final normalizedTrack = Map<String, dynamic>.from(widget.track);
    final videoId = extractTrackId(normalizedTrack);
    final task = videoId == null ? null : ref.watch(downloadTaskProvider(videoId));
    final isDownloadActive = task?.phase == DownloadPhase.active;
    final isDownloaded = task?.phase == DownloadPhase.complete;

    Future<void> runAction(_TrackMenuAction action) async {
      switch (action) {
        case _TrackMenuAction.addToPlaylist:
          await widget.onAddToPlaylist(normalizedTrack);
          return;
        case _TrackMenuAction.startStation:
          await widget.onStartStation(normalizedTrack);
          return;
        case _TrackMenuAction.download:
          if (videoId == null || videoId.isEmpty || isDownloadActive) return;
          await ref
              .read(downloadCenterProvider.notifier)
              .downloadTrack(normalizedTrack);
          if (context.mounted) {
            _showTrackActionFeedback(
              context,
              isDownloaded ? 'Track already downloaded' : 'Download started',
            );
          }
          return;
        case _TrackMenuAction.removeFromFeed:
          if (videoId == null || videoId.isEmpty) return;
          await recordProxyInteractionEvent(
            'remove_from_feed',
            trackId: videoId,
            rawTrack: normalizedTrack,
            metadata: const <String, dynamic>{
              'reason': 'remove_from_feed',
            },
          );
          notifyRecommendationSignal('remove_from_feed');
          if (context.mounted) {
            _showTrackActionFeedback(
              context,
              'Removed from future recommendations',
            );
          }
          return;
        case _TrackMenuAction.details:
          widget.onOpenDetails();
          return;
      }
    }

    return PopupMenuButton<_TrackMenuAction>(
      tooltip: 'Track options',
      position: PopupMenuPosition.under,
      offset: const Offset(-8, 6),
      color: const Color(0xFF171717),
      elevation: 16,
      onOpened: () {
        if (mounted) setState(() => _isOpen = true);
      },
      onCanceled: () {
        if (mounted) setState(() => _isOpen = false);
      },
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: BorderSide(color: Colors.white.withValues(alpha: 0.10)),
      ),
      onSelected: (action) {
        if (mounted) setState(() => _isOpen = false);
        runAction(action);
      },
      itemBuilder: (context) => [
        const PopupMenuItem<_TrackMenuAction>(
          value: _TrackMenuAction.addToPlaylist,
          child: _TrackMenuPopupItem(
            icon: Icons.playlist_add_rounded,
            label: 'Add to playlist',
          ),
        ),
        const PopupMenuItem<_TrackMenuAction>(
          value: _TrackMenuAction.startStation,
          child: _TrackMenuPopupItem(
            icon: Icons.radio_rounded,
            label: 'Start radio',
          ),
        ),
        PopupMenuItem<_TrackMenuAction>(
          value: _TrackMenuAction.download,
          enabled: videoId != null && !isDownloadActive,
          child: _TrackMenuPopupItem(
            icon: isDownloaded
                ? Icons.check_circle_outline_rounded
                : Icons.download_rounded,
            label: isDownloadActive
                ? 'Downloading'
                : isDownloaded
                    ? 'Downloaded'
                    : 'Download',
            muted: videoId == null || isDownloadActive,
          ),
        ),
        PopupMenuItem<_TrackMenuAction>(
          value: _TrackMenuAction.removeFromFeed,
          enabled: videoId != null && videoId.isNotEmpty,
          child: _TrackMenuPopupItem(
            icon: Icons.block_rounded,
            label: 'Remove from feed',
            muted: videoId == null || videoId.isEmpty,
          ),
        ),
        PopupMenuItem<_TrackMenuAction>(
          value: _TrackMenuAction.details,
          enabled: videoId != null && videoId.isNotEmpty,
          child: _TrackMenuPopupItem(
            icon: Icons.info_outline_rounded,
            label: 'View details',
            muted: videoId == null || videoId.isEmpty,
          ),
        ),
      ],
      child: Container(
        width: widget.buttonSize,
        height: widget.buttonSize,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.05),
          shape: BoxShape.circle,
        ),
        child: AnimatedSwitcher(
          duration: const Duration(milliseconds: 180),
          switchInCurve: Curves.easeOutCubic,
          switchOutCurve: Curves.easeInCubic,
          transitionBuilder: (child, animation) => RotationTransition(
            turns: Tween<double>(begin: -0.08, end: 0).animate(animation),
            child: FadeTransition(opacity: animation, child: child),
          ),
          child: Icon(
            _isOpen ? Icons.more_horiz_rounded : Icons.more_vert_rounded,
            key: ValueKey<bool>(_isOpen),
            color: Colors.white70,
            size: widget.iconSize,
          ),
        ),
      ),
    );
  }
}

class _TrackMenuPopupItem extends StatelessWidget {
  final IconData icon;
  final String label;
  final bool muted;

  const _TrackMenuPopupItem({
    required this.icon,
    required this.label,
    this.muted = false,
  });

  @override
  Widget build(BuildContext context) {
    final foreground =
        muted ? Colors.white.withValues(alpha: 0.38) : Colors.white;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, color: foreground, size: 19),
        const SizedBox(width: 12),
        Text(
          label,
          style: TextStyle(
            color: foreground,
            fontSize: 13.5,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
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
