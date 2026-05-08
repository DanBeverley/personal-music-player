import 'dart:async';
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../main_player.dart';
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
      Navigator.of(context)
          .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
    }

    return GestureDetector(
      onTap: openPlayer,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(10),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 18, sigmaY: 18),
          child: Container(
            height: 64,
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(10),
              color: const Color(0xFF1B1D20).withValues(alpha: 0.96),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
                width: 1,
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.34),
                  blurRadius: 28,
                  offset: const Offset(0, 14),
                ),
              ],
            ),
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
                    width: 40,
                    height: 40,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: const Color(0xFF767D85).withValues(alpha: 0.45),
                        width: 1.4,
                      ),
                    ),
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        ClipRRect(
                          borderRadius: BorderRadius.circular(8),
                          child: AppArtwork(
                            thumbnail: playerState.thumbnail,
                            videoId: videoId,
                            width: 36,
                            height: 36,
                            radius: 8,
                            heroTag: 'album_art_${playerState.currentTrackName}',
                          ),
                        ),
                        Container(
                          width: 36,
                          height: 36,
                          decoration: BoxDecoration(
                            color: Colors.black.withValues(alpha: 0.46),
                            borderRadius: BorderRadius.circular(8),
                          ),
                        ),
                        playerState.isDownloading
                            ? const SizedBox(
                                width: 22,
                                height: 22,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.4,
                                  color: Colors.white,
                                ),
                              )
                            : Icon(
                                playerState.isPlaying
                                    ? Icons.pause_rounded
                                    : Icons.play_arrow_rounded,
                                color: Colors.white,
                                size: 18,
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
                      const SizedBox(height: 5),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(999),
                        child: LinearProgressIndicator(
                          value: progress,
                          backgroundColor: Colors.white.withValues(alpha: 0.08),
                          color: const Color(0xFF7A8088),
                          minHeight: 2.5,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 4),
                _MiniPlayerAction(
                  icon: Icons.queue_music_rounded,
                  onTap: openPlayer,
                ),
              ],
            ),
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
        width: 28,
        height: 28,
        decoration: BoxDecoration(
          color: const Color(0xFF2A2D31),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Icon(
          icon,
          color: Colors.white.withValues(alpha: 0.72),
          size: 15,
        ),
      ),
    );
  }
}
