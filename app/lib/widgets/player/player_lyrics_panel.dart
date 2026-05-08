import 'package:flutter/material.dart';

import '../../logic/audio_provider.dart';

class PlayerLyricsPanel extends StatelessWidget {
  final PlayerState playerState;
  final TrackLyricsState lyricsState;
  final int activeIndex;
  final GlobalKey Function(int index) lyricKeyFor;
  final double radiusLarge;
  final Color accentColor;

  const PlayerLyricsPanel({
    super.key,
    required this.playerState,
    required this.lyricsState,
    required this.activeIndex,
    required this.lyricKeyFor,
    required this.radiusLarge,
    required this.accentColor,
  });

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 94, 18, 28),
        child: Container(
          decoration: BoxDecoration(
            color: const Color(0xFF101216).withValues(alpha: 0.94),
            borderRadius: BorderRadius.circular(radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.34),
                blurRadius: 30,
                offset: const Offset(0, 18),
              ),
            ],
          ),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 26, 24, 18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      Icons.lyrics_rounded,
                      color: Colors.white.withValues(alpha: 0.72),
                      size: 18,
                    ),
                    const SizedBox(width: 10),
                    Text(
                      'Lyrics',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.92),
                        fontSize: 16,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const Spacer(),
                    if (lyricsState.source != null &&
                        lyricsState.source!.isNotEmpty)
                      Text(
                        lyricsState.source!,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.34),
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 16),
                Text(
                  playerState.currentTrackName,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.95),
                    fontSize: 22,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  playerState.artist ?? 'Artist Unknown',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.52),
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                const SizedBox(height: 24),
                Expanded(
                  child: AnimatedSwitcher(
                    duration: const Duration(milliseconds: 240),
                    switchInCurve: Curves.easeOutCubic,
                    switchOutCurve: Curves.easeInCubic,
                    child: () {
                      if (lyricsState.isLoading) {
                        return Center(
                          key: const ValueKey('lyrics-loading'),
                          child: CircularProgressIndicator(color: accentColor),
                        );
                      }
                      if (!lyricsState.hasLyrics) {
                        return Center(
                          key: const ValueKey('lyrics-empty'),
                          child: Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 16),
                            child: Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(
                                  Icons.lyrics_outlined,
                                  color: Colors.white.withValues(alpha: 0.38),
                                  size: 42,
                                ),
                                const SizedBox(height: 18),
                                const Text(
                                  'Lyrics are not available for this track yet.',
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                    color: Colors.white70,
                                    fontSize: 18,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 10),
                                Text(
                                  lyricsState.error ??
                                      'Swipe right or tap Player to return.',
                                  textAlign: TextAlign.center,
                                  style: const TextStyle(
                                    color: Colors.white54,
                                    fontSize: 13,
                                    height: 1.35,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        );
                      }

                      return ListView.builder(
                        key: ValueKey('lyrics-${lyricsState.videoId}'),
                        physics: const BouncingScrollPhysics(
                          parent: AlwaysScrollableScrollPhysics(),
                        ),
                        padding: const EdgeInsets.only(bottom: 140),
                        itemCount: lyricsState.lines.length,
                        itemBuilder: (context, index) {
                          final line = lyricsState.lines[index];
                          final isActive = index == activeIndex;
                          return Padding(
                            key: lyricKeyFor(index),
                            padding: const EdgeInsets.symmetric(vertical: 8),
                            child: AnimatedContainer(
                              duration: const Duration(milliseconds: 260),
                              curve: Curves.easeOutCubic,
                              transform: Matrix4.translationValues(
                                isActive ? 0.0 : 8.0,
                                0.0,
                                0.0,
                              ),
                              child: AnimatedDefaultTextStyle(
                                duration: const Duration(milliseconds: 260),
                                curve: Curves.easeOutCubic,
                                style: TextStyle(
                                  color: isActive
                                      ? Colors.white
                                      : Colors.white.withValues(alpha: 0.34),
                                  fontSize: isActive ? 30 : 24,
                                  fontWeight: isActive
                                      ? FontWeight.w700
                                      : FontWeight.w500,
                                  height: 1.16,
                                ),
                                child: Text(line.text),
                              ),
                            ),
                          );
                        },
                      );
                    }(),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
