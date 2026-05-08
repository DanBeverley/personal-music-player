import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../logic/audio_provider_queue.dart';
import '../../logic/download_provider.dart';
import '../app_artwork.dart';
import '../playlist/add_to_playlist_dialog.dart';
import 'player_queue_widgets.dart';
import 'player_sleep_timer_sheet.dart';

class PlayerNowPlayingPanel extends ConsumerWidget {
  final PlayerState playerState;
  final AudioPlayerNotifier audioNotifier;
  final PlaybackQueueState queueState;
  final double? dragValue;
  final ValueChanged<double> onSeekDragChanged;
  final ValueChanged<double> onSeekDragEnd;
  final VoidCallback onOpenQueueSheet;
  final VoidCallback onDismissPlayer;
  final Color accentColor;
  final Color surfaceColor;
  final double radiusLarge;
  final double radiusMedium;
  final String Function(int remainingSeconds) sleepTimerBadgeLabel;

  const PlayerNowPlayingPanel({
    super.key,
    required this.playerState,
    required this.audioNotifier,
    required this.queueState,
    required this.dragValue,
    required this.onSeekDragChanged,
    required this.onSeekDragEnd,
    required this.onOpenQueueSheet,
    required this.onDismissPlayer,
    required this.accentColor,
    required this.surfaceColor,
    required this.radiusLarge,
    required this.radiusMedium,
    required this.sleepTimerBadgeLabel,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final nextUpTrack = playerNextUpTrack(queueState);
    final queueNotifier = ref.read(playbackQueueProvider.notifier);
    final mediaQuery = MediaQuery.of(context);
    final bottomInset = mediaQuery.padding.bottom > mediaQuery.viewPadding.bottom
        ? mediaQuery.padding.bottom
        : mediaQuery.viewPadding.bottom;
    final downloadTask = playerState.videoId == null
        ? null
        : ref.watch(downloadTaskProvider(playerState.videoId!));

    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onVerticalDragEnd: (details) {
        final velocity = details.primaryVelocity ?? 0;
        if (velocity > 700) {
          onDismissPlayer();
        } else if (velocity < -700) {
          onOpenQueueSheet();
        }
      },
      child: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            final layout = _PlayerPanelLayout.from(
              constraints: constraints,
              mediaQuery: mediaQuery,
              hasQueuePreview: nextUpTrack != null,
            );
            return SingleChildScrollView(
              physics: const BouncingScrollPhysics(
                parent: AlwaysScrollableScrollPhysics(),
              ),
              padding: EdgeInsets.fromLTRB(
                24,
                18,
                24,
                math.max(
                  layout.compactLayout ? 161 : 201,
                  bottomInset + 109,
                ).toDouble(),
              ),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: constraints.maxHeight),
                child: Transform.translate(
                  offset: const Offset(0, -5),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const SizedBox(height: 8),
                      _PlayerArtworkHeader(
                        playerState: playerState,
                        layout: layout,
                        radiusLarge: radiusLarge,
                        radiusMedium: radiusMedium,
                      ),
                      _NextUpCard(
                        nextUpTrack: nextUpTrack,
                        queueAwareCompactLayout: layout.queueAwareCompactLayout,
                        radiusLarge: radiusLarge,
                        onTap: onOpenQueueSheet,
                      ),
                      SizedBox(height: layout.sliderGap),
                      _PlayerProgressSection(
                        playerState: playerState,
                        dragValue: dragValue,
                        onSeekDragChanged: onSeekDragChanged,
                        onSeekDragEnd: onSeekDragEnd,
                      ),
                      SizedBox(height: layout.toolRowGap),
                      _PlayerToolActionsRow(
                        playerState: playerState,
                        audioNotifier: audioNotifier,
                        queueNotifier: queueNotifier,
                        accentColor: accentColor,
                        surfaceColor: surfaceColor,
                        radiusLarge: radiusLarge,
                        sleepTimerBadgeLabel: sleepTimerBadgeLabel,
                        downloadTask: downloadTask,
                      ),
                      SizedBox(height: layout.compactLayout ? 12 : 18),
                      _PlayerTransportControls(
                        playerState: playerState,
                        audioNotifier: audioNotifier,
                        queueNotifier: queueNotifier,
                        layout: layout,
                        surfaceColor: surfaceColor,
                      ),
                      SizedBox(height: layout.compactLayout ? 10 : 14),
                    ],
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}

class _PlayerArtworkHeader extends StatelessWidget {
  final PlayerState playerState;
  final _PlayerPanelLayout layout;
  final double radiusLarge;
  final double radiusMedium;

  const _PlayerArtworkHeader({
    required this.playerState,
    required this.layout,
    required this.radiusLarge,
    required this.radiusMedium,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Center(
          child: Hero(
            tag: 'album_art_${playerState.currentTrackName}',
            child: Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(radiusLarge),
                border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
                color: Colors.white.withValues(alpha: 0.03),
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withValues(alpha: 0.44),
                    blurRadius: 48,
                    offset: const Offset(0, 26),
                  ),
                ],
              ),
              child: AppArtwork(
                thumbnail: playerState.thumbnail,
                videoId: playerState.videoId,
                width: layout.artworkSize,
                height: layout.artworkSize,
                radius: radiusMedium,
              ),
            ),
          ),
        ),
        SizedBox(height: layout.artworkGap),
        Text(
          playerState.currentTrackName,
          style: TextStyle(
            fontSize: layout.titleSize,
            fontWeight: FontWeight.w800,
            color: Colors.white,
            height: 1.2,
          ),
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        const SizedBox(height: 8),
        Text(
          playerState.artist ?? 'Artist Unknown',
          style: TextStyle(
            fontSize: layout.artistSize,
            color: Colors.white.withValues(alpha: 0.7),
          ),
        ),
        SizedBox(height: layout.hasQueuePreview ? layout.cardGap : 0),
      ],
    );
  }
}

class _NextUpCard extends StatelessWidget {
  final Map<String, dynamic>? nextUpTrack;
  final bool queueAwareCompactLayout;
  final double radiusLarge;
  final VoidCallback onTap;

  const _NextUpCard({
    required this.nextUpTrack,
    required this.queueAwareCompactLayout,
    required this.radiusLarge,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 280),
      switchInCurve: Curves.easeOutCubic,
      switchOutCurve: Curves.easeInCubic,
      transitionBuilder: (child, animation) {
        final curved = CurvedAnimation(
          parent: animation,
          curve: Curves.easeOutCubic,
          reverseCurve: Curves.easeInCubic,
        );
        return FadeTransition(
          opacity: curved,
          child: SlideTransition(
            position: Tween<Offset>(
              begin: const Offset(0, 0.08),
              end: Offset.zero,
            ).animate(curved),
            child: child,
          ),
        );
      },
      child: nextUpTrack == null
          ? const SizedBox.shrink(key: ValueKey('player-next-up-empty'))
          : GestureDetector(
              key: ValueKey(
                'player-next-up-${extractTrackId(nextUpTrack) ?? nextUpTrack!['title'] ?? 'unknown'}',
              ),
              onTap: onTap,
              child: Container(
                width: double.infinity,
                padding: EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: queueAwareCompactLayout ? 10 : 14,
                ),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.04),
                  borderRadius: BorderRadius.circular(radiusLarge),
                  border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
                ),
                child: Row(
                  children: [
                    Icon(
                      Icons.queue_music_rounded,
                      color: Colors.white.withValues(alpha: 0.72),
                      size: 18,
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: queueAwareCompactLayout
                          ? Text(
                              nextUpTrack!['title'] ?? 'More like this',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                              ),
                            )
                          : Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  'Up next',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.46),
                                    fontSize: 11,
                                    fontWeight: FontWeight.w700,
                                    letterSpacing: 0.5,
                                  ),
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  nextUpTrack!['title'] ?? 'More like this',
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Colors.white,
                                    fontSize: 14,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                              ],
                            ),
                    ),
                    Icon(
                      Icons.keyboard_arrow_up_rounded,
                      color: Colors.white.withValues(alpha: 0.56),
                    ),
                  ],
                ),
              ),
            ),
    );
  }
}

class _PlayerProgressSection extends StatelessWidget {
  final PlayerState playerState;
  final double? dragValue;
  final ValueChanged<double> onSeekDragChanged;
  final ValueChanged<double> onSeekDragEnd;

  const _PlayerProgressSection({
    required this.playerState,
    required this.dragValue,
    required this.onSeekDragChanged,
    required this.onSeekDragEnd,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        SliderTheme(
          data: SliderTheme.of(context).copyWith(
            activeTrackColor: Colors.white,
            inactiveTrackColor: Colors.white.withValues(alpha: 0.2),
            thumbColor: Colors.white,
            overlayColor: Colors.white.withValues(alpha: 0.1),
            trackHeight: 4.0,
            thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 6),
          ),
          child: Slider(
            value: playerState.duration > 0
                ? dragValue ??
                    (playerState.currentPosition / playerState.duration)
                        .clamp(0.0, 1.0)
                : 0.0,
            onChanged: playerState.duration > 0 ? onSeekDragChanged : null,
            onChangeEnd: playerState.duration > 0 ? onSeekDragEnd : null,
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8.0),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')}',
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.6),
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Text(
                '${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.6),
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _PlayerToolActionsRow extends ConsumerWidget {
  final PlayerState playerState;
  final AudioPlayerNotifier audioNotifier;
  final PlaybackQueueNotifier queueNotifier;
  final Color accentColor;
  final Color surfaceColor;
  final double radiusLarge;
  final String Function(int remainingSeconds) sleepTimerBadgeLabel;
  final DownloadTask? downloadTask;

  const _PlayerToolActionsRow({
    required this.playerState,
    required this.audioNotifier,
    required this.queueNotifier,
    required this.accentColor,
    required this.surfaceColor,
    required this.radiusLarge,
    required this.sleepTimerBadgeLabel,
    required this.downloadTask,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        IconButton(
          visualDensity: VisualDensity.compact,
          iconSize: 28,
          padding: EdgeInsets.zero,
          icon: Icon(
            Icons.shuffle_rounded,
            color: Colors.white.withValues(alpha: 0.5),
          ),
          onPressed: () {
            unawaited(queueNotifier.playShuffled());
          },
        ),
        Stack(
          clipBehavior: Clip.none,
          children: [
            IconButton(
              visualDensity: VisualDensity.compact,
              iconSize: 28,
              padding: EdgeInsets.zero,
              icon: Icon(
                Icons.timer_outlined,
                color: playerState.sleepTimerRemainingSeconds > 0
                    ? accentColor
                    : Colors.white.withValues(alpha: 0.5),
              ),
              onPressed: () => showPlayerSleepTimerSheet(
                context,
                audioNotifier,
                playerState,
                surfaceColor: surfaceColor,
                accentColor: accentColor,
                radiusLarge: radiusLarge,
              ),
            ),
            if (playerState.sleepTimerRemainingSeconds > 0)
              Positioned(
                right: -2,
                top: 2,
                child: Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 5,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.14),
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Text(
                    sleepTimerBadgeLabel(playerState.sleepTimerRemainingSeconds),
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 9,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ),
          ],
        ),
        IconButton(
          visualDensity: VisualDensity.compact,
          iconSize: 28,
          padding: EdgeInsets.zero,
          icon: Icon(
            Icons.loop,
            color: playerState.isLooping
                ? accentColor
                : Colors.white.withValues(alpha: 0.5),
          ),
          onPressed: () {
            audioNotifier.toggleLoop(0, playerState.duration * 1000);
          },
        ),
        IconButton(
          visualDensity: VisualDensity.compact,
          iconSize: 28,
          padding: EdgeInsets.zero,
          tooltip: 'Download',
          icon: downloadTask?.phase == DownloadPhase.active
              ? SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(
                    value:
                        downloadTask!.progress > 0 ? downloadTask!.progress : null,
                    strokeWidth: 2.3,
                    color: accentColor,
                  ),
                )
              : Icon(
                  downloadTask?.phase == DownloadPhase.complete
                      ? Icons.download_done_rounded
                      : Icons.download_rounded,
                  color: downloadTask?.phase == DownloadPhase.complete
                      ? accentColor
                      : Colors.white.withValues(alpha: 0.5),
                ),
          onPressed: playerState.videoId == null
              ? null
              : () {
                  ref.read(downloadCenterProvider.notifier).downloadTrack({
                    'id': playerState.videoId,
                    'videoId': playerState.videoId,
                    'title': playerState.currentTrackName,
                    'thumbnail': playerState.thumbnail,
                    'channel': playerState.artist,
                    'duration': playerState.duration,
                  });
                },
        ),
        IconButton(
          visualDensity: VisualDensity.compact,
          iconSize: 28,
          padding: EdgeInsets.zero,
          icon: Icon(
            Icons.playlist_add,
            color: Colors.white.withValues(alpha: 0.5),
          ),
          onPressed: () {
            if (playerState.videoId == null) return;
            showAddToPlaylistDialog(
              context: context,
              track: {
                'id': playerState.videoId,
                'videoId': playerState.videoId,
                'title': playerState.currentTrackName,
                'thumbnail': playerState.thumbnail,
                'channel': playerState.artist,
                'duration': playerState.duration,
              },
            );
          },
        ),
      ],
    );
  }
}

class _PlayerTransportControls extends StatelessWidget {
  final PlayerState playerState;
  final AudioPlayerNotifier audioNotifier;
  final PlaybackQueueNotifier queueNotifier;
  final _PlayerPanelLayout layout;
  final Color surfaceColor;

  const _PlayerTransportControls({
    required this.playerState,
    required this.audioNotifier,
    required this.queueNotifier,
    required this.layout,
    required this.surfaceColor,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        _TransportIconButton(
          width: layout.transportButtonWidth,
          iconSize: layout.transportIconSize,
          icon: Icons.skip_previous_rounded,
          onPressed: () {
            unawaited(queueNotifier.playPrevious());
          },
        ),
        _TransportIconButton(
          width: layout.transportButtonWidth,
          iconSize: layout.transportIconSize,
          icon: Icons.replay_10_rounded,
          onPressed: () {
            unawaited(audioNotifier.seek(playerState.currentPosition - 10));
          },
        ),
        SizedBox(
          width: layout.primaryTransportBox,
          child: Center(
            child: GestureDetector(
              onTap: () {
                unawaited(
                  playerState.isPlaying
                      ? audioNotifier.pause()
                      : audioNotifier.play(),
                );
              },
              child: Container(
                width: layout.primaryTransportSize,
                height: layout.primaryTransportSize,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: surfaceColor,
                  boxShadow: [
                    BoxShadow(
                      color: Colors.black.withValues(alpha: 0.26),
                      blurRadius: 24,
                      offset: const Offset(0, 10),
                    ),
                  ],
                ),
                alignment: Alignment.center,
                child: playerState.isDownloading
                    ? const SizedBox(
                        width: 38,
                        height: 38,
                        child: CircularProgressIndicator(
                          color: Colors.white,
                          strokeWidth: 3,
                        ),
                      )
                    : Transform.translate(
                        offset: Offset(playerState.isPlaying ? 0 : 1, 0),
                        child: Icon(
                          playerState.isPlaying
                              ? Icons.pause_rounded
                              : Icons.play_arrow_rounded,
                          color: Colors.white,
                          size: 42,
                        ),
                      ),
              ),
            ),
          ),
        ),
        _TransportIconButton(
          width: layout.transportButtonWidth,
          iconSize: layout.transportIconSize,
          icon: Icons.forward_10_rounded,
          onPressed: () {
            unawaited(audioNotifier.seek(playerState.currentPosition + 10));
          },
        ),
        _TransportIconButton(
          width: layout.transportButtonWidth,
          iconSize: layout.transportIconSize,
          icon: Icons.skip_next_rounded,
          onPressed: () {
            unawaited(queueNotifier.playNext());
          },
        ),
      ],
    );
  }
}

class _TransportIconButton extends StatelessWidget {
  final double width;
  final double iconSize;
  final IconData icon;
  final VoidCallback onPressed;

  const _TransportIconButton({
    required this.width,
    required this.iconSize,
    required this.icon,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: width,
      child: IconButton(
        visualDensity: VisualDensity.compact,
        iconSize: iconSize,
        padding: EdgeInsets.zero,
        onPressed: onPressed,
        icon: Icon(
          icon,
          color: Colors.white.withValues(alpha: 0.82),
        ),
      ),
    );
  }
}

class _PlayerPanelLayout {
  final bool compactLayout;
  final bool queueAwareCompactLayout;
  final bool hasQueuePreview;
  final double artworkSize;
  final double titleSize;
  final double artistSize;
  final double artworkGap;
  final double cardGap;
  final double sliderGap;
  final double toolRowGap;
  final double transportIconSize;
  final double transportButtonWidth;
  final double primaryTransportSize;
  final double primaryTransportBox;

  const _PlayerPanelLayout({
    required this.compactLayout,
    required this.queueAwareCompactLayout,
    required this.hasQueuePreview,
    required this.artworkSize,
    required this.titleSize,
    required this.artistSize,
    required this.artworkGap,
    required this.cardGap,
    required this.sliderGap,
    required this.toolRowGap,
    required this.transportIconSize,
    required this.transportButtonWidth,
    required this.primaryTransportSize,
    required this.primaryTransportBox,
  });

  factory _PlayerPanelLayout.from({
    required BoxConstraints constraints,
    required MediaQueryData mediaQuery,
    required bool hasQueuePreview,
  }) {
    final compactLayout = constraints.maxHeight < 760;
    final extraCompactLayout = constraints.maxHeight < 690;
    final queueAwareCompactLayout = compactLayout && hasQueuePreview;
    final artworkSize = math.min(
      mediaQuery.size.width *
          (queueAwareCompactLayout
              ? 0.52
              : compactLayout
                  ? 0.62
                  : 0.76),
      queueAwareCompactLayout
          ? 244.0
          : compactLayout
              ? 296.0
              : 360.0,
    ).clamp(
      queueAwareCompactLayout
          ? 164.0
          : extraCompactLayout
              ? 182.0
              : 214.0,
      360.0,
    ).toDouble();

    return _PlayerPanelLayout(
      compactLayout: compactLayout,
      queueAwareCompactLayout: queueAwareCompactLayout,
      hasQueuePreview: hasQueuePreview,
      artworkSize: artworkSize,
      titleSize: queueAwareCompactLayout
          ? 22.0
          : compactLayout
              ? 24.0
              : 28.0,
      artistSize: queueAwareCompactLayout
          ? 15.0
          : compactLayout
              ? 16.0
              : 18.0,
      artworkGap: queueAwareCompactLayout
          ? 16.0
          : compactLayout
              ? 24.0
              : 40.0,
      cardGap: queueAwareCompactLayout ? 10.0 : compactLayout ? 14.0 : 18.0,
      sliderGap:
          queueAwareCompactLayout ? 12.0 : compactLayout ? 18.0 : 26.0,
      toolRowGap:
          queueAwareCompactLayout ? 10.0 : compactLayout ? 12.0 : 20.0,
      transportIconSize: queueAwareCompactLayout
          ? 28.0
          : compactLayout
              ? 30.0
              : 34.0,
      transportButtonWidth: queueAwareCompactLayout
          ? 42.0
          : compactLayout
              ? 46.0
              : 50.0,
      primaryTransportSize: queueAwareCompactLayout
          ? 68.0
          : compactLayout
              ? 74.0
              : 84.0,
      primaryTransportBox: queueAwareCompactLayout
          ? 76.0
          : compactLayout
              ? 82.0
              : 92.0,
    );
  }
}
