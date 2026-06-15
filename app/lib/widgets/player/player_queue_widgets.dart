import 'package:flutter/material.dart';

import '../../logic/audio_provider_queue.dart';
import '../../logic/track_metadata.dart';
import '../../main_dialogs.dart';
import '../app_artwork.dart';

bool playerIsTrackInQueue(PlaybackQueueState queueState, String? trackId) {
  if (trackId == null || trackId.isEmpty) return false;
  return queueState.queue.any((track) => extractTrackId(track) == trackId);
}

int playerResolvedQueueIndex(PlaybackQueueState queueState) {
  if (queueState.queue.isEmpty) return -1;
  final activeTrackId = queueState.currentTrackId;
  if (activeTrackId != null && activeTrackId.isNotEmpty) {
    final matchingIndex = queueState.queue.indexWhere(
      (track) => extractTrackId(track) == activeTrackId,
    );
    if (matchingIndex >= 0) {
      return matchingIndex;
    }
  }
  return queueState.currentIndex.clamp(0, queueState.queue.length - 1).toInt();
}

Map<String, dynamic>? playerCurrentQueueTrack(PlaybackQueueState queueState) {
  final resolvedIndex = playerResolvedQueueIndex(queueState);
  if (resolvedIndex < 0 || resolvedIndex >= queueState.queue.length) {
    return null;
  }
  return queueState.queue[resolvedIndex];
}

Map<String, dynamic>? playerNextUpTrack(PlaybackQueueState queueState) {
  final resolvedIndex = playerResolvedQueueIndex(queueState);
  for (var i = resolvedIndex + 1; i < queueState.queue.length; i++) {
    final track = queueState.queue[i];
    if (isTrackHidden(track)) {
      continue;
    }
    return track;
  }
  return queueState.recommendations.isNotEmpty
      ? queueState.recommendations.first
      : null;
}

class PlayerPanelTab extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;
  final bool pointLeft;
  final Color surfaceColor;

  const PlayerPanelTab({
    super.key,
    required this.icon,
    required this.onTap,
    required this.pointLeft,
    required this.surfaceColor,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: ClipPath(
        clipper: LyricsTabClipper(pointLeft: pointLeft),
        child: Container(
          width: 28,
          height: 74,
          decoration: BoxDecoration(
            color: surfaceColor.withValues(alpha: 0.96),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.3),
                blurRadius: 18,
                offset: const Offset(0, 8),
              ),
            ],
          ),
          child: Center(
            child: Icon(
              icon,
              color: Colors.white.withValues(alpha: 0.82),
              size: 14,
            ),
          ),
        ),
      ),
    );
  }
}

class PlayerQueueTile extends StatelessWidget {
  final Map<String, dynamic> track;
  final bool isActive;
  final VoidCallback onTap;
  final VoidCallback? onRemove;
  final VoidCallback? onAddToPlaylist;
  final String? trailingLabel;
  final bool isDimmed;
  final bool isRemoving;
  final Widget? trailing;
  final double radiusLarge;
  final double radiusMedium;

  const PlayerQueueTile({
    super.key,
    required this.track,
    required this.isActive,
    required this.onTap,
    required this.radiusLarge,
    required this.radiusMedium,
    this.onRemove,
    this.onAddToPlaylist,
    this.trailingLabel,
    this.isDimmed = false,
    this.isRemoving = false,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    final videoId = extractTrackId(track);
    final tile = Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: isActive
            ? Colors.white.withValues(alpha: 0.08)
            : Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(radiusLarge),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
      ),
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 220),
        opacity: isDimmed ? 0.34 : 1,
        child: Material(
          type: MaterialType.transparency,
          child: ListTile(
            contentPadding:
                const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
            onTap: onTap,
            leading: AppArtwork(
              thumbnail: track['thumbnail'],
              videoId: videoId,
              width: 58,
              height: 58,
              radius: radiusMedium,
            ),
            title: Text(
              track['title'] ?? 'Unknown Track',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: isDimmed
                    ? Colors.white.withValues(alpha: 0.48)
                    : Colors.white,
                fontWeight: isActive ? FontWeight.w700 : FontWeight.w600,
              ),
            ),
            subtitle: Text(
              track['channel'] ?? track['author'] ?? '',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: Colors.white.withValues(alpha: isDimmed ? 0.3 : 0.54),
                fontSize: 12,
              ),
            ),
            trailing: trailing ??
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (trailingLabel != null) ...[
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 6,
                        ),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.08),
                          borderRadius: BorderRadius.circular(999),
                        ),
                        child: Text(
                          trailingLabel!,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.8),
                            fontSize: 11,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      const SizedBox(width: 6),
                    ],
                    if (onAddToPlaylist != null)
                      IconButton(
                        onPressed: onAddToPlaylist,
                        icon: Icon(
                          Icons.playlist_add_rounded,
                          color: Colors.white.withValues(alpha: 0.72),
                        ),
                      ),
                    if (onRemove != null)
                      IconButton(
                        onPressed: onRemove,
                        icon: Icon(
                          Icons.remove_circle_outline_rounded,
                          color: Colors.white.withValues(alpha: 0.62),
                        ),
                      ),
                  ],
                ),
          ),
        ),
      ),
    );

    return TweenAnimationBuilder<double>(
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeInOutCubic,
      tween: Tween<double>(begin: 1, end: isRemoving ? 0 : 1),
      builder: (context, value, child) {
        return ClipRect(
          child: Align(
            heightFactor: value.clamp(0, 1),
            child: Opacity(
              opacity: value.clamp(0, 1),
              child: Transform.translate(
                offset: Offset((1 - value) * -30, 0),
                child: child,
              ),
            ),
          ),
        );
      },
      child: tile,
    );
  }
}
