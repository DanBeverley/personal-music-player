import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../navigation/player_navigation.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';

class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final videoId = playerState.videoId;

    final progress = playerState.duration > 0
        ? (playerState.currentPosition / playerState.duration).clamp(0.0, 1.0)
        : 0.0;

    void openPlayer() {
      unawaited(openFullPlayer(context));
    }

    return GestureDetector(
      onTap: openPlayer,
      child: NeatieSurface(
        radius: neatieRadiusMedium,
        color: neatieGlass,
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 6),
        child: SizedBox(
          height: 52,
          child: Row(
            children: [
              GestureDetector(
                onTap: () {
                  unawaited(
                    playerState.isPlaying
                        ? audioNotifier.pause()
                        : audioNotifier.play(),
                  );
                },
                child: Container(
                  width: 36,
                  height: 36,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(neatieRadiusSmall),
                    border: Border.all(
                      color: neatieStroke,
                      width: 1.4,
                    ),
                  ),
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(neatieRadiusSmall),
                        child: AppArtwork(
                          thumbnail: playerState.thumbnail,
                          videoId: videoId,
                          width: 32,
                          height: 32,
                          radius: neatieRadiusSmall,
                          heroTag: 'album_art_${playerState.currentTrackName}',
                        ),
                      ),
                      Container(
                        width: 32,
                        height: 32,
                        decoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.46),
                          borderRadius:
                              BorderRadius.circular(neatieRadiusSmall),
                        ),
                      ),
                      playerState.isDownloading
                          ? const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(
                        strokeWidth: 2.2,
                                color: Colors.white,
                              ),
                            )
                          : Icon(
                              playerState.isPlaying
                                  ? Icons.pause_rounded
                                  : Icons.play_arrow_rounded,
                              color: Colors.white,
                              size: 17,
                            ),
                    ],
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(
                      playerState.currentTrackName,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.white,
                        fontWeight: FontWeight.w700,
                        fontSize: 12.5,
                        letterSpacing: -0.2,
                      ),
                    ),
                    const SizedBox(height: 1),
                    Text(
                      playerState.artist ?? 'Unknown Artist',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.64),
                        fontSize: 10.5,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 4),
                    ClipRRect(
                      borderRadius: BorderRadius.circular(999),
                      child: LinearProgressIndicator(
                        value: progress,
                        backgroundColor: Colors.white.withValues(alpha: 0.10),
                        color: neatieActive,
                        minHeight: 2.5,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 4),
              _MiniPlayerAction(
                icon: Icons.cast_rounded,
                onTap: openPlayer,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MiniPlayerAction extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;

  const _MiniPlayerAction({
    required this.icon,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Container(
        width: 26,
        height: 26,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(neatieRadiusSmall),
        ),
        child: Icon(
          icon,
          color: neatieMutedText,
          size: 14,
        ),
      ),
    );
  }
}
