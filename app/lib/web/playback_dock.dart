import 'dart:async';
import 'package:flutter/material.dart';
import 'browser_audio.dart';
import 'design_system.dart';
import 'home_widgets.dart';
import 'web_player.dart';
import 'lyrics_panel.dart';

/// Both layouts control the same browser audio element and playback session.
class PlaybackDock extends StatelessWidget {
  const PlaybackDock(
      {super.key,
      required this.player,
      required this.audio,
      required this.wide,
      required this.liked,
      required this.onLike,
      required this.onRepeat,
      required this.onQueue});
  final WebPlayer player;
  final BrowserAudio audio;
  final bool wide, liked;
  final VoidCallback onLike, onRepeat, onQueue;

  void _lyrics(BuildContext context) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      backgroundColor: NeatieDesign.surface,
      builder: (context) => SafeArea(
          child: SizedBox(
              height: MediaQuery.sizeOf(context).height * .8,
              child: LyricsPanel(player: player))));

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
            tooltip: 'Load related songs',
            onPressed: player.loadingRelated
                ? null
                : () => unawaited(player.replenish()),
            icon: player.loadingRelated
                ? const NeatieLoading(label: 'Finding related songs', size: 18)
                : const Icon(Icons.auto_awesome_rounded)),
      ]);
  Widget _progress() => Row(children: [
        Text(_time(player.position),
            style: const TextStyle(fontSize: 10, color: NeatieDesign.muted)),
        Expanded(
            child: Slider(
                value: player.position
                    .clamp(0, player.duration > 0 ? player.duration : 1),
                max: player.duration > 0 ? player.duration : 1,
                onChanged: player.duration > 0 ? player.seek : null)),
        Text(_time(player.duration),
            style: const TextStyle(fontSize: 10, color: NeatieDesign.muted)),
      ]);
  Widget _identity({double cover = 52}) => Row(children: [
        HomeArtwork(
            api: player.api, item: player.current!, kind: 'track', size: cover),
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
                  player.error != null
                      ? 'Playback interrupted · tap play to retry'
                      : '${player.current?['artist'] ?? player.current?['channel'] ?? ''}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontSize: 11,
                      color: player.error != null
                          ? Colors.orangeAccent
                          : NeatieDesign.muted)),
            ])),
      ]);
  void _expand(BuildContext context) {
    showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        backgroundColor: NeatieDesign.surface,
        showDragHandle: true,
        builder: (context) => AnimatedBuilder(
            animation: player,
            builder: (context, _) {
              if (player.current == null) return const SizedBox.shrink();
              return SafeArea(
                  child: SingleChildScrollView(
                      child: Padding(
                          padding: const EdgeInsets.fromLTRB(28, 8, 28, 28),
                          child:
                              Column(mainAxisSize: MainAxisSize.min, children: [
                            Text('NOW PLAYING', style: NeatieDesign.eyebrow),
                            const SizedBox(height: 24),
                            HomeArtwork(
                                api: player.api,
                                item: player.current!,
                                kind: 'track',
                                size: (MediaQuery.sizeOf(context).width - 80)
                                    .clamp(160.0, 320.0)),
                            const SizedBox(height: 24),
                            Text('${player.current?['title'] ?? ''}',
                                textAlign: TextAlign.center,
                                style: NeatieDesign.display(30)),
                            const SizedBox(height: 8),
                            Text(
                                '${player.current?['artist'] ?? player.current?['channel'] ?? ''}',
                                textAlign: TextAlign.center,
                                style:
                                    const TextStyle(color: NeatieDesign.muted)),
                            if (player.error != null)
                              Padding(
                                  padding: const EdgeInsets.only(top: 12),
                                  child: Text(player.error!,
                                      textAlign: TextAlign.center,
                                      style: const TextStyle(
                                          color: Colors.orangeAccent))),
                            const SizedBox(height: 20),
                            _progress(),
                            _transport(),
                            Row(
                                mainAxisAlignment:
                                    MainAxisAlignment.spaceEvenly,
                                children: [
                                  IconButton(
                                      tooltip: 'Like current song',
                                      onPressed: onLike,
                                      icon: Icon(liked
                                          ? Icons.favorite
                                          : Icons.favorite_border)),
                                  StatefulBuilder(
                                      builder: (context, update) => IconButton(
                                          tooltip: audio.repeatOne
                                              ? 'Turn off repeat'
                                              : 'Repeat this song',
                                          onPressed: () => update(onRepeat),
                                          color: audio.repeatOne
                                              ? NeatieDesign.accent
                                              : null,
                                          icon: const Icon(Icons.repeat_one))),
                                  IconButton(
                                      tooltip: 'Lyrics',
                                      onPressed: () => _lyrics(context),
                                      icon: const Icon(Icons.lyrics_outlined)),
                                  IconButton(
                                      tooltip: 'Open queue',
                                      onPressed: () {
                                        Navigator.pop(context);
                                        onQueue();
                                      },
                                      icon: const Icon(Icons.queue_music)),
                                ]),
                          ]))));
            }));
  }

  @override
  Widget build(BuildContext context) {
    if (player.current == null) return const SizedBox.shrink();
    return DecoratedBox(
        decoration: const BoxDecoration(
            color: NeatieDesign.surface,
            border: Border(top: BorderSide(color: NeatieDesign.line))),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Padding(
              padding: EdgeInsets.symmetric(
                  horizontal: wide ? 28 : 12, vertical: wide ? 10 : 8),
              child: wide
                  ? Row(children: [
                      Expanded(flex: 4, child: _identity()),
                      const SizedBox(width: 24),
                      Expanded(
                          flex: 5,
                          child: Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [_transport(), _progress()])),
                      const SizedBox(width: 24),
                      Expanded(
                          flex: 3,
                          child: Row(
                              mainAxisAlignment: MainAxisAlignment.end,
                              children: [
                                IconButton(
                                    tooltip: 'Like current song',
                                    onPressed: onLike,
                                    icon: Icon(liked
                                        ? Icons.favorite
                                        : Icons.favorite_border)),
                                IconButton(
                                    tooltip: audio.repeatOne
                                        ? 'Turn off repeat'
                                        : 'Repeat this song',
                                    onPressed: onRepeat,
                                    color: audio.repeatOne
                                        ? NeatieDesign.accent
                                        : null,
                                    icon: const Icon(Icons.repeat_one)),
                                IconButton(
                                    tooltip: 'Lyrics',
                                    onPressed: () => _lyrics(context),
                                    icon: const Icon(Icons.lyrics_outlined)),
                                IconButton(
                                    tooltip: 'Open queue',
                                    onPressed: onQueue,
                                    icon: const Icon(Icons.queue_music)),
                              ])),
                    ])
                  : Row(children: [
                      Expanded(
                          child: Semantics(
                              button: true,
                              excludeSemantics: true,
                              label:
                                  'Expand player: ${player.current?['title'] ?? ''}',
                              child: InkWell(
                                  onTap: () => _expand(context),
                                  borderRadius: BorderRadius.circular(12),
                                  child: Padding(
                                      padding: const EdgeInsets.symmetric(
                                          vertical: 2),
                                      child: _identity(cover: 44))))),
                      const SizedBox(width: 8),
                      _play(size: 40),
                      IconButton(
                          tooltip: 'Next',
                          onPressed: player.index + 1 < player.queue.length
                              ? () => unawaited(player.next())
                              : null,
                          icon: const Icon(Icons.skip_next_rounded)),
                    ])),
        ]));
  }
}
