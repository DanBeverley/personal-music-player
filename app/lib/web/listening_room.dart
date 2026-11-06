import 'dart:async';
import 'dart:math' as math;
import '../logic/track_metadata.dart';
import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';
import 'vinyl_surface_stub.dart'
    if (dart.library.js_interop) 'vinyl_surface_web.dart' as native;
import 'artwork_image.dart';
import 'design_system.dart';
import 'queue_screen.dart';
import 'collection_search.dart';
import 'lyrics_panel.dart';
import 'song_discussion.dart';
import 'web_player.dart';
import 'playback_port.dart';

/// A drag previews locally; only its final position reaches the audio element.
/// Both player surfaces share this contract, including track-change fencing.
class PlaybackSeekSlider extends StatefulWidget {
  const PlaybackSeekSlider({super.key, required this.player,
    this.semanticFormatterCallback});
  final WebPlayer player;
  final String Function(double)? semanticFormatterCallback;
  @override
  State<PlaybackSeekSlider> createState() => _PlaybackSeekSliderState();
}

class _PlaybackSeekSliderState extends State<PlaybackSeekSlider> {
  double? _preview;
  Object? _dragIdentity;
  Object get _identity => (widget.player.listeningEpoch,
      extractTrackId(widget.player.current));

  @override
  void didUpdateWidget(covariant PlaybackSeekSlider oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.player != widget.player ||
        _dragIdentity != null && _dragIdentity != _identity) {
      _preview = null;
      _dragIdentity = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final player = widget.player;
    final maximum = player.duration > 0 ? player.duration : 1.0;
    return Slider(
      semanticFormatterCallback: widget.semanticFormatterCallback,
      value: (_preview ?? player.position).clamp(0, maximum),
      max: maximum,
      onChangeStart: (_) => _dragIdentity = _identity,
      onChanged: player.duration > 0
          ? (value) => setState(() => _preview = value) : null,
      onChangeEnd: (value) {
        final valid = _dragIdentity == _identity;
        setState(() { _preview = null; _dragIdentity = null; });
        if (valid) player.seek(value);
      },
    );
  }
}

/// The mini player's expanded presentation. Audio stays owned by WebPlayer.
class ListeningRoom extends StatefulWidget {
  const ListeningRoom(
      {super.key,
      required this.player,
      required this.onClose,
      this.initialLyrics = false,
      this.conversationStore,
      required this.onRepeat,
      required this.repeatOne,
      required this.onLike,
      this.onAddToPlaylist,
      required this.isLiked,
      this.likesListenable});
  final WebPlayer player;
  final VoidCallback onClose, onRepeat;
  final bool initialLyrics;
  final SessionStore? Function(String scope)? conversationStore;
  final bool Function() repeatOne;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final bool Function(Map<String, dynamic>) isLiked;
  final Listenable? likesListenable;
  @override
  State<ListeningRoom> createState() => _ListeningRoomState();
}

class _ListeningRoomState extends State<ListeningRoom> {
  static const _paper = Color(0xffdedfca);
  bool _covers = true;
  bool _searchOpen = false;
  bool _discussion = false;
  bool _discussionVisible = false;
  final _recordKey = GlobalKey();
  final _coverKey = GlobalKey();
  final _coverCenter = ValueNotifier<double?>(null);
  bool _alignmentPending = false;
  late bool _lyrics = widget.initialLyrics;
  late var _stamp = _identity;
  get _identity => (
        widget.player.queueRevision,
        widget.player.index,
        widget.player.playing,
        widget.player.current,
        widget.player.error,
        widget.player.selectionError,
        widget.player.pendingSelection,
        widget.player.loadingRelated,
        widget.player.relatedError
      );

  @override
  void initState() {
    super.initState();
    _stamp = _identity;
    widget.player.addListener(_changed);
    widget.likesListenable?.addListener(_likesChanged);
  }

  void _likesChanged() {
    if (mounted) setState(() {});
  }

  void _changed() {
    if (_stamp == _identity) return;
    _stamp = _identity;
    if (mounted) setState(() {});
  }

  @override
  void didUpdateWidget(covariant ListeningRoom old) {
    super.didUpdateWidget(old);
    if (old.likesListenable != widget.likesListenable) {
      old.likesListenable?.removeListener(_likesChanged);
      widget.likesListenable?.addListener(_likesChanged);
    }
    if (old.player != widget.player) {
      old.player.removeListener(_changed);
      widget.player.addListener(_changed);
      _stamp = _identity;
    }
  }

  @override
  void dispose() {
    widget.likesListenable?.removeListener(_likesChanged);
    widget.player.removeListener(_changed);
    _coverCenter.dispose();
    super.dispose();
  }

  void _alignCoverAfterLayout() {
    if (_alignmentPending) return;
    _alignmentPending = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _alignmentPending = false;
      if (!mounted) return;
      final record = _recordKey.currentContext?.findRenderObject();
      final cover = _coverKey.currentContext?.findRenderObject();
      if (record is! RenderBox ||
          cover is! RenderBox ||
          !record.hasSize ||
          !cover.hasSize) return;
      final center = record.localToGlobal(record.size.center(Offset.zero)).dy -
          cover.localToGlobal(Offset.zero).dy;
      if (_coverCenter.value == null ||
          (center - _coverCenter.value!).abs() > .5) {
        _coverCenter.value = center;
      }
    });
  }

  String _time(double seconds) =>
      '${seconds.toInt() ~/ 60}:${(seconds.toInt() % 60).toString().padLeft(2, '0')}';
  String _title(Map<String, dynamic>? track) => '${track?['title'] ?? ''}';
  String _artist(Map<String, dynamic>? track) =>
      '${track?['artist'] ?? track?['channel'] ?? ''}';

  Widget _button(String label, IconData icon, VoidCallback? action,
          {bool selected = false}) =>
      Material(
          type: MaterialType.transparency,
          shape: const CircleBorder(),
          clipBehavior: Clip.antiAlias,
          child: IconButton(
              tooltip: label,
              onPressed: action,
              iconSize: 20,
              color: selected ? _paper : NeatieDesign.muted,
              style: IconButton.styleFrom(
                  hoverColor: _paper.withValues(alpha: .08)),
              icon: Icon(icon)));

  Widget _current(bool narrow) {
    final player = widget.player;
    return LayoutBuilder(builder: (context, bounds) {
      final diameter = math.min(
          bounds.maxWidth - (narrow ? 24 : 64), narrow ? 278.0 : 410.0);
      return Column(
          key: const ValueKey('vinyl-current'),
          mainAxisSize: MainAxisSize.min,
          children: [
            VinylRecord(
                key: _recordKey,
                player: player,
                diameter: diameter,
                playing: player.playing),
            SizedBox(height: narrow ? 24 : 38),
            Text(_title(player.current),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style:
                    const TextStyle(fontSize: 15, fontWeight: FontWeight.w500)),
            const SizedBox(height: 6),
            Text(_artist(player.current),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style:
                    const TextStyle(fontSize: 12, color: NeatieDesign.muted)),
            const SizedBox(height: 20),
            SizedBox(
                width: math.min(diameter + 24, 410),
                child: AnimatedBuilder(
                    animation: Listenable.merge([player, player.progress]),
                    builder: (context, _) => Column(children: [
                          if (player.error != null ||
                              player.selectionError != null ||
                              player.preparingSelection)
                            Padding(
                                padding: const EdgeInsets.only(bottom: 12),
                                child: Text(
                                    player.selectionError ??
                                        player.error ??
                                        'Preparing ${player.pendingSelection!['title'] ?? 'next song'}',
                                    textAlign: TextAlign.center,
                                    style: const TextStyle(
                                        color: Colors.orangeAccent,
                                        fontSize: 12))),
                          SliderTheme(
                              data: SliderTheme.of(context).copyWith(
                                  trackHeight: 1,
                                  thumbShape: const RoundSliderThumbShape(
                                      enabledThumbRadius: 3),
                                  overlayShape: const RoundSliderOverlayShape(
                                      overlayRadius: 12),
                                  activeTrackColor: _paper,
                                  inactiveTrackColor: Colors.white24,
                                  thumbColor: _paper),
                              child: PlaybackSeekSlider(
                                  player: player,
                                  semanticFormatterCallback: _time,
                              )),
                          Padding(
                              padding:
                                  const EdgeInsets.symmetric(horizontal: 20),
                              child: Row(
                                  mainAxisAlignment:
                                      MainAxisAlignment.spaceBetween,
                                  children: [
                                    Text(_time(player.position),
                                        style: const TextStyle(
                                            fontSize: 10,
                                            color: NeatieDesign.muted)),
                                    Text(_time(player.duration),
                                        style: const TextStyle(
                                            fontSize: 10,
                                            color: NeatieDesign.muted))
                                  ])),
                          Row(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                _button(
                                    widget.repeatOne()
                                        ? 'Turn off repeat'
                                        : 'Repeat this song',
                                    Icons.repeat_one, () {
                                  widget.onRepeat();
                                  setState(() {});
                                }, selected: widget.repeatOne()),
                                _button(
                                    'Previous',
                                    Icons.skip_previous_rounded,
                                    player.index > 0
                                        ? () => unawaited(player.previous())
                                        : null),
                                Material(
                                    type: MaterialType.transparency,
                                    shape: const CircleBorder(),
                                    clipBehavior: Clip.antiAlias,
                                    child: IconButton(
                                        tooltip:
                                            player.canPause ? 'Pause' : 'Play',
                                        iconSize: 28,
                                        color: _paper,
                                        onPressed: () => player.canPause
                                            ? player.pause()
                                            : unawaited(player.play()),
                                        icon: player.busy
                                            ? const NeatieLoading(
                                                label: 'Loading song', size: 20)
                                            : Icon(player.playing
                                                ? Icons.pause_rounded
                                                : Icons.play_arrow_rounded))),
                                _button(
                                    'Next',
                                    Icons.skip_next_rounded,
                                    player.index + 1 < player.queue.length
                                        ? () => unawaited(player.next())
                                        : null),
                                _button(
                                    'Lyrics',
                                    Icons.lyrics_outlined,
                                    () => setState(() {
                                          _lyrics = !_lyrics;
                                          _discussion = false;
                                        }),
                                    selected: _lyrics),
                              ]),
                        ]))),
          ]);
    });
  }

  Widget _pane({required bool visible, required Widget child}) {
    final duration = MediaQuery.disableAnimationsOf(context)
        ? Duration.zero
        : const Duration(milliseconds: 280);
    return TooltipVisibility(
        visible: visible,
        child: IgnorePointer(
            ignoring: !visible,
            child: ExcludeFocus(
                excluding: !visible,
                child: ExcludeSemantics(
                    excluding: !visible,
                    child: AnimatedOpacity(
                        opacity: visible ? 1 : 0,
                        duration: duration,
                        child: AnimatedSlide(
                            offset:
                                visible ? Offset.zero : const Offset(0, .025),
                            duration: duration,
                            curve: Curves.easeOutCubic,
                            child: TickerMode(
                                enabled: visible, child: child)))))));
  }

  Widget _queue(bool narrow) => SizedBox(
      height: narrow ? 610 : 660,
      child: Stack(fit: StackFit.expand, children: [
        _pane(
            visible: !_lyrics && !_discussion,
            child: Column(children: [
              Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Wrap(
                      alignment: WrapAlignment.center,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        const Text('UP NEXT',
                            style: TextStyle(
                                fontSize: 10,
                                letterSpacing: 2,
                                color: NeatieDesign.muted)),
                        const SizedBox(width: 24),
                        _button('Cover queue view', Icons.album_outlined,
                            () => setState(() => _covers = true),
                            selected: _covers),
                        _button('List queue view', Icons.view_list_outlined,
                            () => setState(() => _covers = false),
                            selected: !_covers),
                        _button('Search queue', Icons.search,
                            () => setState(() => _searchOpen = !_searchOpen),
                            selected: _searchOpen),
                        _button(
                            'Ask about this song',
                            Icons.auto_awesome_outlined,
                            () => setState(() {
                                  _discussion = true;
                                  _discussionVisible = true;
                                  _lyrics = false;
                                })),
                      ])),
              ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 240),
                  child: SingleChildScrollView(
                      child: CatalogTrackSearch(
                          key: ValueKey(
                              'vinyl-search:${widget.player.api.userScopeId}'),
                          api: widget.player.api,
                          bare: true,
                          expanded: _searchOpen,
                          active: !_lyrics && !_discussion,
                          label: 'Find songs to add to your queue',
                          isAdded: (track) => widget.player.queue.any((item) =>
                              extractTrackId(item) == extractTrackId(track)),
                          onAdd: (track) async =>
                              widget.player.addToQueue(track),
                          onPlay: (track) =>
                              playQueueSearchResult(widget.player, track)))),
              const SizedBox(height: 12),
              Expanded(
                  child: Stack(fit: StackFit.expand, children: [
                _pane(
                    visible: _covers,
                    child: ValueListenableBuilder<double?>(
                        valueListenable: _coverCenter,
                        builder: (_, center, __) => QueueCoverBrowser(
                            key: _coverKey,
                            player: widget.player,
                            frontCoverCenter: narrow ? null : center))),
                _pane(
                    visible: !_covers,
                    child: QueueScreen(
                        player: widget.player,
                        active: !_covers && !_lyrics && !_discussion,
                        onLike: widget.onLike,
                        onAddToPlaylist: widget.onAddToPlaylist,
                        likesListenable: widget.likesListenable,
                        isLiked: widget.isLiked)),
              ])),
              if (_covers) ...[
                if (widget.player.relatedError != null)
                  Text(widget.player.relatedError!,
                      textAlign: TextAlign.center),
                TextButton.icon(
                    onPressed: widget.player.loadingRelated
                        ? null
                        : () => unawaited(widget.player.replenish()),
                    icon: widget.player.loadingRelated
                        ? const NeatieLoading(label: 'Finding songs', size: 16)
                        : const Icon(Icons.auto_awesome_outlined, size: 16),
                    label: Text(widget.player.loadingRelated
                        ? 'Finding songs'
                        : 'Discover more')),
              ],
            ])),
        _pane(
            visible: _lyrics,
            child: LyricsPanel(
                player: widget.player,
                minimalHeader: true,
                active: _lyrics,
                onAsk: () => setState(() {
                      _discussion = true;
                      _discussionVisible = true;
                      _lyrics = false;
                    }),
                onClose: () => setState(() => _lyrics = false))),
      ]));

  Widget _conversation() {
    final reduced = MediaQuery.disableAnimationsOf(context);
    return Offstage(
        offstage: !_discussionVisible,
        child: ClipRect(
            child: IgnorePointer(
                ignoring: !_discussion,
                child: ExcludeFocus(
                    excluding: !_discussion,
                    child: ExcludeSemantics(
                        excluding: !_discussion,
                        child: AnimatedSlide(
                            key: const ValueKey('song-conversation-slide'),
                            onEnd: () {
                              if (mounted &&
                                  !_discussion &&
                                  _discussionVisible) {
                                setState(() => _discussionVisible = false);
                              }
                            },
                            offset:
                                _discussion ? Offset.zero : const Offset(1, 0),
                            duration: reduced
                                ? Duration.zero
                                : Duration(
                                    milliseconds: _discussion ? 380 : 320),
                            curve: Curves.easeInOutCubic,
                            child: AnimatedOpacity(
                                opacity: _discussion ? 1 : 0,
                                duration: reduced
                                    ? Duration.zero
                                    : const Duration(milliseconds: 220),
                                child: TickerMode(
                                    enabled: _discussion,
                                    child: SongDiscussion(
                                        player: widget.player,
                                        storeForScope: widget.conversationStore,
                                        onClose: () => setState(
                                            () => _discussion = false))))))))));
  }

  @override
  Widget build(BuildContext context) {
    if (widget.player.current == null) return const SizedBox.shrink();
    return ScrollConfiguration(
        behavior: ScrollConfiguration.of(context).copyWith(scrollbars: false),
        child: Material(
            color: const Color(0xff111212),
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(20),
                side: const BorderSide(color: Color(0xff393b38))),
            clipBehavior: Clip.antiAlias,
            child: Column(children: [
              Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                  child: Row(children: [
                    _button('Close player', Icons.keyboard_arrow_down_rounded,
                        widget.onClose),
                    const Expanded(
                        child: Text('neatie',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                                fontFamily: 'NeatieDisplay', fontSize: 20))),
                    if (widget.onAddToPlaylist != null)
                      _button(
                          'Add current song to playlist',
                          Icons.playlist_add_rounded,
                          () => unawaited(
                              widget.onAddToPlaylist!(widget.player.current!))),
                    _button(
                        'Like current song',
                        widget.isLiked(widget.player.current!)
                            ? Icons.favorite
                            : Icons.favorite_border, () async {
                      unawaited(widget.onLike(widget.player.current!));
                    }),
                  ])),
              Expanded(child: LayoutBuilder(builder: (context, bounds) {
                final narrow = bounds.maxWidth < 760;
                if (narrow)
                  return Stack(fit: StackFit.expand, children: [
                    SingleChildScrollView(
                        child: Padding(
                            padding: const EdgeInsets.fromLTRB(18, 18, 18, 32),
                            child: Column(children: [
                              _current(true),
                              const SizedBox(height: 40),
                              _queue(true),
                            ]))),
                    Positioned.fill(
                        child: Padding(
                            padding: const EdgeInsets.fromLTRB(12, 12, 12, 16),
                            child: _conversation())),
                  ]);
                _alignCoverAfterLayout();
                return Padding(
                    padding: const EdgeInsets.fromLTRB(44, 24, 44, 32),
                    child: Row(
                        crossAxisAlignment: CrossAxisAlignment.center,
                        children: [
                          Expanded(
                              flex: 6,
                              child: SingleChildScrollView(
                                  child: _current(false))),
                          const SizedBox(width: 48),
                          Expanded(
                              flex: 4,
                              child: Stack(fit: StackFit.expand, children: [
                                Center(
                                    child: SingleChildScrollView(
                                        child: _queue(false))),
                                _conversation(),
                              ])),
                        ]));
              })),
            ])));
  }
}

class VinylRecord extends StatefulWidget {
  const VinylRecord(
      {super.key,
      required this.player,
      required this.diameter,
      required this.playing});
  final WebPlayer player;
  final double diameter;
  final bool playing;
  @override
  State<VinylRecord> createState() => _VinylRecordState();
}

class _VinylRecordState extends State<VinylRecord>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  late final _rotation =
      AnimationController(vsync: this, duration: const Duration(seconds: 28));
  bool _visible = true;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  void _sync() {
    if (!kIsWeb &&
        widget.playing &&
        _visible &&
        !MediaQuery.disableAnimationsOf(context) &&
        TickerMode.of(context)) {
      if (!_rotation.isAnimating) _rotation.repeat();
    } else {
      _rotation.stop();
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  @override
  void didUpdateWidget(covariant VinylRecord old) {
    super.didUpdateWidget(old);
    _sync();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _visible = state == AppLifecycleState.resumed;
    _sync();
    if (kIsWeb && mounted) setState(() {});
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _rotation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (kIsWeb) {
      return ExcludeSemantics(
          child: native.vinylSurface(
              diameter: widget.diameter,
              retryKey: widget.player.diagnostics['operation'],
              urls: ArtworkImage.sources(
                  widget.player.api
                      .artworkUrls(widget.player.current!, 'track'),
                  widget.diameter * .22,
                  MediaQuery.devicePixelRatioOf(context)),
              playing: widget.playing &&
                  _visible &&
                  TickerMode.of(context) &&
                  !MediaQuery.disableAnimationsOf(context)));
    }
    return ExcludeSemantics(
        child: RotationTransition(
            turns: _rotation,
            child: RepaintBoundary(
                child: SizedBox(
                    width: widget.diameter,
                    height: widget.diameter,
                    child: CustomPaint(
                        painter: const _RecordPainter(),
                        child: Center(
                            child: ClipOval(
                                child: SizedBox(
                                    width: widget.diameter * .22,
                                    height: widget.diameter * .22,
                                    child: Stack(
                                        alignment: Alignment.center,
                                        children: [
                                          ArtworkImage(
                                              urls: widget.player.api
                                                  .artworkUrls(
                                                      widget.player.current!,
                                                      'track'),
                                              width: widget.diameter * .22,
                                              height: widget.diameter * .22),
                                          Container(
                                              width: 9,
                                              height: 9,
                                              decoration: const BoxDecoration(
                                                  color: Color(0xff151714),
                                                  shape: BoxShape.circle)),
                                        ])))))))));
  }
}

class _RecordPainter extends CustomPainter {
  const _RecordPainter();
  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero), r = size.width / 2;
    final rect = Offset.zero & size;
    canvas.drawCircle(
        center,
        r,
        Paint()
          ..shader = const RadialGradient(
              colors: [Color(0xffececda), Color(0xffd2d5bd), Color(0xffe5e6d2)],
              stops: [0, .6, 1]).createShader(rect));
    final groove = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = .55
      ..color = const Color(0x167a8066);
    for (double radius = r * .3; radius < r * .96; radius += 2.7)
      canvas.drawCircle(center, radius, groove);
    canvas.drawArc(
        rect.deflate(5),
        -.8,
        1.2,
        false,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..color = const Color(0x33ffffff));
    canvas.drawCircle(
        center,
        r * .27,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1
          ..color = const Color(0x22979d83));
  }

  @override
  bool shouldRepaint(covariant _RecordPainter oldDelegate) => false;
}
