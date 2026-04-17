import 'package:flutter/material.dart';

import '../../logic/audio_provider.dart';
import '../app_artwork.dart';
import 'track_menu_button.dart';

class GenreTabChip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;

  const GenreTabChip({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(999),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          curve: Curves.easeOutCubic,
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 11),
          decoration: BoxDecoration(
            color: selected
                ? Colors.blueAccent.withValues(alpha: 0.14)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(999),
            border: Border.all(
              color: selected
                  ? Colors.blueAccent.withValues(alpha: 0.85)
                  : Colors.white.withValues(alpha: 0.52),
            ),
          ),
          child: Text(
            label,
            style: TextStyle(
              color: selected ? Colors.blueAccent.shade100 : Colors.white70,
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.2,
            ),
          ),
        ),
      ),
    );
  }
}

class GenreTrackTile extends StatelessWidget {
  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onOpenDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  const GenreTrackTile({
    super.key,
    required this.track,
    required this.onPlay,
    required this.onOpenDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  @override
  Widget build(BuildContext context) {
    final trackId = extractTrackId(track);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      child: Row(
        children: [
          AppArtwork(
            thumbnail: track['thumbnail'],
            videoId: trackId,
            width: 58,
            height: 58,
            radius: 16,
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  track['title']?.toString() ?? 'Unknown track',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 16,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  (track['channel'] ?? track['author'] ?? track['artist'])
                          ?.toString() ??
                      'Unknown artist',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.68),
                    fontSize: 13,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          TrackMenuButton(
            track: track,
            onOpenDetails: onOpenDetails,
            onAddToPlaylist: onAddToPlaylist,
            onStartStation: onStartStation,
            buttonSize: 42,
            iconSize: 18,
          ),
          InkWell(
            onTap: onPlay,
            borderRadius: BorderRadius.circular(999),
            child: Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.08),
                shape: BoxShape.circle,
              ),
              child: const Icon(
                Icons.play_arrow_rounded,
                color: Colors.white,
                size: 24,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
