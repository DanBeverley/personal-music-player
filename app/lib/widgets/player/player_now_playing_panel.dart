import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider.dart';
import '../../logic/audio_provider_queue.dart';
import '../../ui/app_theme_tokens.dart';
import '../app_artwork.dart';
import 'player_queue_widgets.dart';

class PlayerNowPlayingPanel extends ConsumerWidget {
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

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final queueNotifier = ref.read(playbackQueueProvider.notifier);
    final nextUpTrack = playerNextUpTrack(queueState);
    final mediaQuery = MediaQuery.of(context);
    final layout = _NowPlayingLayout.from(mediaQuery.size);
    final bottomInset =
        math.max(mediaQuery.padding.bottom, mediaQuery.viewPadding.bottom);

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
        child: SingleChildScrollView(
          physics: const BouncingScrollPhysics(
            parent: AlwaysScrollableScrollPhysics(),
          ),
          padding: EdgeInsets.fromLTRB(
            28,
            26,
            28,
            math.max(bottomInset + 108, 144),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const _PlayerHeaderLogo(),
              const SizedBox(height: 14),
              _ArtworkStage(
                playerState: playerState,
                artworkSize: layout.artworkSize,
                radius: radiusLarge,
              ),
              SizedBox(height: layout.infoGap),
              _TrackIdentity(playerState: playerState),
              const SizedBox(height: 24),
              _ProgressSection(
                playerState: playerState,
                queueState: queueState,
                nextUpTrack: nextUpTrack,
                dragValue: dragValue,
                onSeekDragChanged: onSeekDragChanged,
                onSeekDragEnd: onSeekDragEnd,
                onOpenQueueSheet: onOpenQueueSheet,
              ),
              const SizedBox(height: 16),
              _SecondaryActionsRow(
                playerState: playerState,
                audioNotifier: audioNotifier,
                queueNotifier: queueNotifier,
                onOpenQueueSheet: onOpenQueueSheet,
                accentColor: accentColor,
              ),
              const SizedBox(height: 14),
              _TransportRow(
                playerState: playerState,
                audioNotifier: audioNotifier,
                queueNotifier: queueNotifier,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _PlayerHeaderLogo extends StatelessWidget {
  const _PlayerHeaderLogo();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Image.asset(
        'assets/branding/neatie_3rd.png',
        width: 34,
        height: 26,
        fit: BoxFit.contain,
        errorBuilder: (_, __, ___) => const Icon(
          Icons.graphic_eq_rounded,
          color: Colors.white70,
          size: 22,
        ),
      ),
    );
  }
}

class _ArtworkStage extends StatelessWidget {
  const _ArtworkStage({
    required this.playerState,
    required this.artworkSize,
    required this.radius,
  });

  final PlayerState playerState;
  final double artworkSize;
  final double radius;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Hero(
        tag: 'album_art_${playerState.currentTrackName}',
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(radius),
            boxShadow: [
              BoxShadow(
                color: Colors.white.withValues(alpha: 0.08),
                blurRadius: 80,
                spreadRadius: -18,
              ),
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.55),
                blurRadius: 36,
                offset: const Offset(0, 22),
              ),
            ],
          ),
          child: AppArtwork(
            thumbnail: playerState.thumbnail,
            videoId: playerState.videoId,
            width: artworkSize,
            height: artworkSize,
            radius: radius,
          ),
        ),
      ),
    );
  }
}

class _TrackIdentity extends ConsumerStatefulWidget {
  const _TrackIdentity({required this.playerState});

  final PlayerState playerState;

  @override
  ConsumerState<_TrackIdentity> createState() => _TrackIdentityState();
}

class _TrackIdentityState extends ConsumerState<_TrackIdentity> {
  String? _optimisticTrackId;
  bool? _optimisticSaved;

  @override
  Widget build(BuildContext context) {
    final playerState = widget.playerState;
    final trackId = playerState.videoId?.trim();
    final libraryState = ref.watch(libraryProvider);
    final librarySaved = libraryState.maybeWhen(
      data: (tracks) => tracks.any(
        (track) =>
            extractTrackId(track) == trackId &&
            track['is_liked_locally'] == true,
      ),
      orElse: () => false,
    );
    final isSaved =
        _optimisticTrackId == trackId && _optimisticSaved != null
            ? _optimisticSaved!
            : librarySaved;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                playerState.currentTrackName,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 26,
                  fontWeight: FontWeight.w800,
                  height: 1.08,
                  letterSpacing: -0.5,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                playerState.artist ?? 'Artist Unknown',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: neatieMutedText,
                  fontSize: 16,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        ),
        IconButton(
          onPressed: trackId == null || trackId.isEmpty
              ? null
              : () {
                  final nextSaved = !isSaved;
                  setState(() {
                    _optimisticTrackId = trackId;
                    _optimisticSaved = nextSaved;
                  });
                  final updateFuture = nextSaved
                      ? upsertCloudLibraryTrack({
                          'id': trackId,
                          'videoId': trackId,
                          'title': playerState.currentTrackName,
                          'artist': playerState.artist,
                          'channel': playerState.artist,
                          'thumbnail': playerState.thumbnail,
                          'duration': playerState.duration,
                        })
                      : removeCloudLibraryTrack(trackId);
                  unawaited(
                    updateFuture.then((_) {
                      ref.invalidate(libraryProvider);
                      if (!mounted) return;
                      setState(() {
                        _optimisticSaved = null;
                      });
                    }).catchError((_) {
                      if (!mounted) return;
                      setState(() {
                        _optimisticSaved = null;
                      });
                    }),
                  );
                },
          icon: Icon(
            isSaved ? Icons.favorite_rounded : Icons.favorite_border_rounded,
          ),
          color: Colors.white,
          iconSize: 27,
          tooltip: isSaved ? 'Remove from liked songs' : 'Like',
        ),
      ],
    );
  }
}

class _ProgressSection extends StatelessWidget {
  const _ProgressSection({
    required this.playerState,
    required this.queueState,
    required this.nextUpTrack,
    required this.dragValue,
    required this.onSeekDragChanged,
    required this.onSeekDragEnd,
    required this.onOpenQueueSheet,
  });

  final PlayerState playerState;
  final PlaybackQueueState queueState;
  final Map<String, dynamic>? nextUpTrack;
  final double? dragValue;
  final ValueChanged<double> onSeekDragChanged;
  final ValueChanged<double> onSeekDragEnd;
  final VoidCallback onOpenQueueSheet;

  @override
  Widget build(BuildContext context) {
    final progress = playerState.duration > 0
        ? dragValue ??
            (playerState.currentPosition / playerState.duration).clamp(0.0, 1.0)
        : 0.0;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _PlaybackContextRow(
          playingFrom: _playingFromLabel(queueState),
          nextUpTrack: nextUpTrack,
          onOpenQueueSheet: onOpenQueueSheet,
        ),
        const SizedBox(height: 10),
        SliderTheme(
          data: SliderTheme.of(context).copyWith(
            activeTrackColor: Colors.white,
            inactiveTrackColor: Colors.white.withValues(alpha: 0.18),
            thumbColor: Colors.white,
            overlayColor: Colors.white.withValues(alpha: 0.08),
            trackHeight: 3,
            thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 5),
          ),
          child: Slider(
            value: progress,
            onChanged: playerState.duration > 0 ? onSeekDragChanged : null,
            onChangeEnd: playerState.duration > 0 ? onSeekDragEnd : null,
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              _DurationLabel(seconds: playerState.currentPosition),
              _DurationLabel(seconds: playerState.duration),
            ],
          ),
        ),
      ],
    );
  }
}

class _PlaybackContextRow extends StatelessWidget {
  const _PlaybackContextRow({
    required this.playingFrom,
    required this.nextUpTrack,
    required this.onOpenQueueSheet,
  });

  final String playingFrom;
  final Map<String, dynamic>? nextUpTrack;
  final VoidCallback onOpenQueueSheet;

  @override
  Widget build(BuildContext context) {
    final nextTitle =
        (nextUpTrack?['title'] ?? nextUpTrack?['name'] ?? '').toString().trim();

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: _ContextLabel(
            eyebrow: 'PLAYING FROM',
            value: playingFrom,
          ),
        ),
        if (nextTitle.isNotEmpty) ...[
          const SizedBox(width: 20),
          Expanded(
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: onOpenQueueSheet,
              child: _ContextLabel(
                eyebrow: 'UP NEXT',
                value: nextTitle,
                alignEnd: true,
              ),
            ),
          ),
        ],
      ],
    );
  }
}

class _ContextLabel extends StatelessWidget {
  const _ContextLabel({
    required this.eyebrow,
    required this.value,
    this.alignEnd = false,
  });

  final String eyebrow;
  final String value;
  final bool alignEnd;

  @override
  Widget build(BuildContext context) {
    final alignment =
        alignEnd ? CrossAxisAlignment.end : CrossAxisAlignment.start;
    return SizedBox(
      height: 40,
      child: Column(
        mainAxisAlignment: MainAxisAlignment.start,
        crossAxisAlignment: alignment,
        children: [
          Text(
            eyebrow,
            maxLines: 1,
            style: const TextStyle(
              color: neatieMutedText,
              fontSize: 10,
              height: 1.2,
              fontWeight: FontWeight.w800,
              letterSpacing: 1.8,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            textAlign: alignEnd ? TextAlign.end : TextAlign.start,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 13,
              height: 1.2,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

String _playingFromLabel(PlaybackQueueState queueState) {
  final playlistName = queueState.playlistName?.trim();
  if (playlistName != null && playlistName.isNotEmpty) return playlistName;
  return switch (queueState.mode) {
    PlaybackQueueMode.radio => 'Neatie radio',
    PlaybackQueueMode.playlist => 'Neatie queue',
    PlaybackQueueMode.none => 'Now playing',
  };
}

class _DurationLabel extends StatelessWidget {
  const _DurationLabel({required this.seconds});

  final int seconds;

  @override
  Widget build(BuildContext context) {
    return Text(
      '${(seconds / 60).floor()}:${(seconds % 60).toString().padLeft(2, '0')}',
      style: const TextStyle(
        color: neatieMutedText,
        fontSize: 12,
        fontWeight: FontWeight.w600,
      ),
    );
  }
}

class _TransportRow extends StatelessWidget {
  const _TransportRow({
    required this.playerState,
    required this.audioNotifier,
    required this.queueNotifier,
  });

  final PlayerState playerState;
  final AudioPlayerNotifier audioNotifier;
  final PlaybackQueueNotifier queueNotifier;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      children: [
        _RoundTransportButton(
          icon: Icons.skip_previous_rounded,
          onPressed: () => unawaited(queueNotifier.playPrevious()),
        ),
        GestureDetector(
          onTap: () => unawaited(
            playerState.isPlaying ? audioNotifier.pause() : audioNotifier.play(),
          ),
          child: Container(
            width: 76,
            height: 76,
            decoration: BoxDecoration(
              color: Colors.white,
              shape: BoxShape.circle,
              boxShadow: [
                BoxShadow(
                  color: Colors.white.withValues(alpha: 0.16),
                  blurRadius: 34,
                ),
              ],
            ),
            child: playerState.isDownloading
                ? const Padding(
                    padding: EdgeInsets.all(22),
                    child: CircularProgressIndicator(
                      color: Colors.black,
                      strokeWidth: 3,
                    ),
                  )
                : Icon(
                    playerState.isPlaying
                        ? Icons.pause_rounded
                        : Icons.play_arrow_rounded,
                    color: Colors.black,
                    size: 42,
                  ),
          ),
        ),
        _RoundTransportButton(
          icon: Icons.skip_next_rounded,
          onPressed: () => unawaited(queueNotifier.playNext()),
        ),
      ],
    );
  }
}

class _SecondaryActionsRow extends StatelessWidget {
  const _SecondaryActionsRow({
    required this.playerState,
    required this.audioNotifier,
    required this.queueNotifier,
    required this.onOpenQueueSheet,
    required this.accentColor,
  });

  final PlayerState playerState;
  final AudioPlayerNotifier audioNotifier;
  final PlaybackQueueNotifier queueNotifier;
  final VoidCallback onOpenQueueSheet;
  final Color accentColor;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        _SecondaryIconButton(
          icon: Icons.shuffle_rounded,
          onPressed: () => unawaited(queueNotifier.playShuffled()),
        ),
        _SecondaryIconButton(
          icon: Icons.replay_10_rounded,
          onPressed: () =>
              unawaited(audioNotifier.seek(playerState.currentPosition - 10)),
        ),
        _SecondaryIconButton(
          icon: Icons.forward_10_rounded,
          onPressed: () =>
              unawaited(audioNotifier.seek(playerState.currentPosition + 10)),
        ),
        _SecondaryIconButton(
          icon: Icons.repeat_rounded,
          color: playerState.isLooping ? accentColor : null,
          onPressed: () =>
              audioNotifier.toggleLoop(0, playerState.duration * 1000),
        ),
        _SecondaryIconButton(
          icon: Icons.queue_music_rounded,
          onPressed: onOpenQueueSheet,
        ),
      ],
    );
  }
}

class _RoundTransportButton extends StatelessWidget {
  const _RoundTransportButton({
    required this.icon,
    required this.onPressed,
  });

  final IconData icon;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      onPressed: onPressed,
      icon: Icon(icon),
      color: Colors.white,
      iconSize: 38,
    );
  }
}

class _SecondaryIconButton extends StatelessWidget {
  const _SecondaryIconButton({
    required this.icon,
    required this.onPressed,
    this.color,
  });

  final IconData icon;
  final VoidCallback onPressed;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      visualDensity: VisualDensity.compact,
      onPressed: onPressed,
      icon: Icon(icon),
      color: color ?? Colors.white.withValues(alpha: 0.78),
      iconSize: 25,
    );
  }
}

class _NowPlayingLayout {
  const _NowPlayingLayout({
    required this.artworkSize,
    required this.infoGap,
  });

  final double artworkSize;
  final double infoGap;

  factory _NowPlayingLayout.from(Size size) {
    final compact = size.height < 760;
    final artwork = math.min(size.width - 72, compact ? 322.0 : 386.0);
    return _NowPlayingLayout(
      artworkSize: artwork.clamp(236.0, 386.0).toDouble(),
      infoGap: compact ? 28 : 38,
    );
  }
}
