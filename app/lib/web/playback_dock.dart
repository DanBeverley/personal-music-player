import 'dart:async';
import 'package:flutter/material.dart';
import 'browser_audio.dart';
import 'design_system.dart';
import 'home_widgets.dart';
import 'web_player.dart';
import 'listening_room.dart';

/// Both layouts control the same browser audio element and playback session.
class PlaybackDock extends StatelessWidget {
  const PlaybackDock({
    super.key,
    required this.player,
    required this.audio,
    required this.wide,
    required this.liked,
    required this.onLike,
    required this.onRepeat,
    this.onQueueLike,
    this.onAddToPlaylist,
    this.isLiked,
    this.likesListenable,
    this.onExpandedChanged,
  });
  final WebPlayer player;
  final BrowserAudio audio;
  final bool wide, liked;
  final VoidCallback onLike, onRepeat;
  final Future<void> Function(Map<String, dynamic>)? onQueueLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final bool Function(Map<String, dynamic>)? isLiked;
  final Listenable? likesListenable;
  final ValueChanged<bool>? onExpandedChanged;

  void _lyrics(BuildContext context) =>
      unawaited(_expand(context, showLyrics: true));

  String _time(double value) =>
      '${value.toInt() ~/ 60}:${(value.toInt() % 60).toString().padLeft(2, '0')}';
  Widget _play({double size = 44}) => IconButton.filled(
      tooltip: player.canPause ? 'Pause' : 'Play',
      onPressed: () =>
          player.canPause ? player.pause() : unawaited(player.play()),
      icon: Builder(
          builder: (context) => AnimatedSwitcher(
              duration: MediaQuery.disableAnimationsOf(context)
                  ? Duration.zero
                  : const Duration(milliseconds: 180),
              child: player.busy
                  ? const NeatieLoading(
                      key: ValueKey('loading'),
                      label: 'Loading song',
                      size: 22,
                      color: NeatieDesign.background)
                  : Icon(
                      player.playing
                          ? Icons.pause_rounded
                          : Icons.play_arrow_rounded,
                      key: ValueKey(player.playing)))),
      constraints: BoxConstraints.tightFor(width: size, height: size));
  Widget _transport() =>
      Row(mainAxisAlignment: MainAxisAlignment.center, children: [
        IconButton(
            tooltip: 'Back 10 seconds',
            onPressed: player.duration > 0
                ? () => player
                    .seek((player.position - 10).clamp(0, player.duration))
                : null,
            icon: const Icon(Icons.replay_10_rounded)),
        IconButton(
            tooltip: 'Previous',
            onPressed:
                player.index > 0 ? () => unawaited(player.previous()) : null,
            icon: const Icon(Icons.skip_previous_rounded)),
        _play(),
        IconButton(
            tooltip: 'Next',
            onPressed: player.index + 1 < player.queue.length
                ? () => unawaited(player.next())
                : null,
            icon: const Icon(Icons.skip_next_rounded)),
        IconButton(
            tooltip: 'Forward 10 seconds',
            onPressed: player.duration > 0
                ? () => player
                    .seek((player.position + 10).clamp(0, player.duration))
                : null,
            icon: const Icon(Icons.forward_10_rounded)),
      ]);
  Widget _progress() => AnimatedBuilder(
      animation: player.progress,
      builder: (_, __) => Row(children: [
            Text(_time(player.position),
                style:
                    const TextStyle(fontSize: 10, color: NeatieDesign.muted)),
            Expanded(
                child: PlaybackSeekSlider(player: player,
                    semanticFormatterCallback: _time)),
            Text(_time(player.duration),
                style:
                    const TextStyle(fontSize: 10, color: NeatieDesign.muted)),
          ]));
  Widget _identity({double cover = 52}) => Row(children: [
        HomeArtwork(
            api: player.api,
            item: player.current!,
            kind: 'track',
            size: cover,
            diagnosticKey: 'dock-${player.diagnostics['operation']}'),
        const SizedBox(width: 14),
        Expanded(
            child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
              Text('${player.current?['title'] ?? ''}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                      fontSize: 13, fontWeight: FontWeight.w700)),
              const SizedBox(height: 4),
              Text(
                  player.selectionError ??
                      (player.pendingSelection != null
                          ? 'Preparing ${player.pendingSelection!['title'] ?? 'next song'}'
                          : player.error != null
                              ? player.error!
                              : '${player.current?['artist'] ?? player.current?['channel'] ?? ''}'),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontSize: 11,
                      color:
                          player.error != null || player.selectionError != null
                              ? Colors.orangeAccent
                              : NeatieDesign.muted)),
            ])),
      ]);
  Future<void> _expand(BuildContext context, {bool showLyrics = false}) async {
    final reduced = MediaQuery.disableAnimationsOf(context);
    onExpandedChanged?.call(true);
    try {
      await showGeneralDialog<void>(
        context: context,
        barrierDismissible: true,
        barrierLabel: 'Close player',
        barrierColor: Colors.black.withValues(alpha: .78),
        transitionDuration:
            reduced ? Duration.zero : const Duration(milliseconds: 450),
        transitionBuilder: (context, animation, _, child) => FadeTransition(
            opacity: animation,
            child: ScaleTransition(
                alignment: Alignment.bottomCenter,
                scale: Tween(begin: .985, end: 1.0).animate(CurvedAnimation(
                    parent: animation, curve: Curves.easeOutCubic)),
                child: SlideTransition(
                    position:
                        Tween(begin: const Offset(0, .035), end: Offset.zero)
                            .animate(CurvedAnimation(
                                parent: animation, curve: Curves.easeOutCubic)),
                    child: child))),
        pageBuilder: (sheet, _, __) => SafeArea(
            child: Dialog(
                backgroundColor: Colors.transparent,
                elevation: 0,
                insetPadding: EdgeInsets.symmetric(
                    horizontal: MediaQuery.sizeOf(sheet).width < 600 ? 10 : 36,
                    vertical: 12),
                child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 1240),
                    child: SizedBox(
                        height: MediaQuery.sizeOf(sheet).height * .92,
                        child: ListeningRoom(
                          player: player,
                          onAddToPlaylist: onAddToPlaylist,
                          conversationStore: (scope) => BrowserSessionStore(
                              '${player.api.base}::song-conversations-v1',
                              scope: scope),
                          onClose: () => Navigator.pop(sheet),
                          initialLyrics: showLyrics,
                          onRepeat: onRepeat,
                          repeatOne: () => audio.repeatOne,
                          onLike: onQueueLike ??
                              (_) async {
                                onLike();
                              },
                          isLiked: isLiked ?? (_) => liked,
                          likesListenable: likesListenable,
                        ))))),
      );
    } finally {
      onExpandedChanged?.call(false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (player.current == null) return const SizedBox.shrink();
    return DecoratedBox(
      decoration: const BoxDecoration(
        color: NeatieDesign.surface,
        border: Border(top: BorderSide(color: NeatieDesign.line)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: EdgeInsets.symmetric(
              horizontal: wide ? 28 : 12,
              vertical: wide ? 10 : 8,
            ),
            child: wide
                ? Row(
                    children: [
                      Expanded(
                        flex: 4,
                        child: Semantics(
                          button: true,
                          excludeSemantics: true,
                          onTap: () => _expand(context),
                          label:
                              'Expand player: ${player.current?['title'] ?? ''}',
                          value: player.error,
                          liveRegion: player.error != null,
                          child: InkWell(
                            onTap: () => _expand(context),
                            borderRadius: BorderRadius.circular(12),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(vertical: 6),
                              child: _identity(),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 24),
                      Expanded(
                        flex: 5,
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [_transport(), _progress()],
                        ),
                      ),
                      const SizedBox(width: 24),
                      Expanded(
                        flex: 3,
                        child: FittedBox(
                            fit: BoxFit.scaleDown,
                            alignment: Alignment.centerRight,
                            child: Row(
                              mainAxisSize: MainAxisSize.min,
                              mainAxisAlignment: MainAxisAlignment.end,
                              children: [
                                if (Theme.of(context).platform != TargetPlatform.iOS &&
                                    Theme.of(context).platform != TargetPlatform.android)
                                  _DesktopVolume(audio: audio),
                                if (onAddToPlaylist != null)
                                  IconButton(
                                      tooltip: 'Add current song to playlist',
                                      onPressed: () => unawaited(
                                          onAddToPlaylist!(player.current!)),
                                      icon: const Icon(
                                          Icons.playlist_add_rounded)),
                                IconButton(
                                  tooltip: 'Like current song',
                                  onPressed: onLike,
                                  icon: Icon(
                                    liked
                                        ? Icons.favorite
                                        : Icons.favorite_border,
                                  ),
                                ),
                                IconButton(
                                  tooltip: audio.repeatOne
                                      ? 'Turn off repeat'
                                      : 'Repeat this song',
                                  onPressed: onRepeat,
                                  color: audio.repeatOne
                                      ? NeatieDesign.accent
                                      : null,
                                  icon: const Icon(Icons.repeat_one),
                                ),
                                IconButton(
                                  tooltip: 'Lyrics',
                                  onPressed: () => _lyrics(context),
                                  icon: const Icon(Icons.lyrics_outlined),
                                ),
                                IconButton(
                                  tooltip: 'Open queue',
                                  onPressed: () => unawaited(_expand(context)),
                                  icon: const Icon(Icons.queue_music),
                                ),
                              ],
                            )),
                      ),
                    ],
                  )
                : Row(
                    children: [
                      Expanded(
                        child: Semantics(
                          button: true,
                          onTap: () => _expand(context),
                          excludeSemantics: true,
                          label:
                              'Expand player: ${player.current?['title'] ?? ''}',
                          value: player.error,
                          liveRegion: player.error != null,
                          child: InkWell(
                            onTap: () => _expand(context),
                            borderRadius: BorderRadius.circular(12),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(vertical: 2),
                              child: _identity(cover: 44),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      _play(size: 40),
                      IconButton(
                        tooltip: 'Next',
                        onPressed: player.index + 1 < player.queue.length
                            ? () => unawaited(player.next())
                            : null,
                        icon: const Icon(Icons.skip_next_rounded),
                      ),
                    ],
                  ),
          ),
        ],
      ),
    );
  }
}

class _DesktopVolume extends StatefulWidget {
  const _DesktopVolume({required this.audio});
  final BrowserAudio audio;
  @override
  State<_DesktopVolume> createState() => _DesktopVolumeState();
}

class _DesktopVolumeState extends State<_DesktopVolume> {
  @override
  Widget build(BuildContext context) {
    final volume = widget.audio.element.volume.clamp(0.0, 1.0);
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(volume == 0 ? Icons.volume_off_outlined : Icons.volume_up_outlined,
          size: 18),
      SizedBox(
        width: 96,
        child: MergeSemantics(
          child: Semantics(
            label: 'Volume',
            child: Slider(
              value: volume,
              semanticFormatterCallback: (value) =>
                  'Volume ${(value * 100).round()} percent',
              onChanged: (value) =>
                  setState(() => widget.audio.element.volume = value),
            ),
          ),
        ),
      ),
    ]);
  }
}
