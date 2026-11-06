import 'dart:async';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import '../logic/track_metadata.dart';
import 'artwork_image.dart';
import 'design_system.dart';
import 'web_player.dart';

Future<void> playQueueSearchResult(
    WebPlayer player, Map<String, dynamic> track) async {
  final id = extractTrackId(track);
  var at = player.queue.indexWhere((item) => extractTrackId(item) == id);
  if (at < 0) {
    if (!player.addToQueue(track)) return;
    at = player.queue.length - 1;
  }
  await player.playIndex(at);
}

class QueueScreen extends StatefulWidget {
  const QueueScreen({
    super.key,
    required this.player,
    required this.onLike,
    this.onAddToPlaylist,
    this.isLiked,
    this.likesListenable,
    this.active = true,
  });
  final WebPlayer player;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final bool Function(Map<String, dynamic>)? isLiked;
  final Listenable? likesListenable;
  final bool active;
  @override
  State<QueueScreen> createState() => _QueueScreenState();
}

class _QueueScreenState extends State<QueueScreen> {
  final _scroll = ScrollController();
  int? _dragRevision;
  String _queueState = '';
  String get _stamp =>
      '${widget.player.queueRevision}:${widget.player.queue.length}:${widget.player.index}:${widget.player.title}:${widget.player.loadingRelated}:${widget.player.relatedError}:${widget.player.storageError}';
  void _playerChanged() {
    if (!widget.active || _stamp == _queueState) return;
    _queueState = _stamp;
    if (mounted) setState(() {});
  }

  @override
  void initState() {
    super.initState();
    widget.likesListenable?.addListener(_viewChanged);
    _queueState = _stamp;
    widget.player.addListener(_playerChanged);
  }

  void _viewChanged() {
    if (mounted) setState(() {});
  }

  @override
  void didUpdateWidget(covariant QueueScreen old) {
    super.didUpdateWidget(old);
    if (old.likesListenable != widget.likesListenable) {
      old.likesListenable?.removeListener(_viewChanged);
      widget.likesListenable?.addListener(_viewChanged);
    }
    if (old.player != widget.player) {
      old.player.removeListener(_playerChanged);
      widget.player.addListener(_playerChanged);
    }
    _queueState = _stamp;
  }

  @override
  void dispose() {
    _scroll.dispose();
    widget.likesListenable?.removeListener(_viewChanged);
    widget.player.removeListener(_playerChanged);
    super.dispose();
  }

  void _edited(bool success) {
    if (!success && mounted)
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('The queue changed. Please try again.')),
      );
  }

  @override
  Widget build(BuildContext context) {
    final player = widget.player, revision = player.queueRevision;
    return ScrollConfiguration(
        behavior: ScrollConfiguration.of(context).copyWith(scrollbars: false),
        child: CustomScrollView(
          controller: _scroll,
          slivers: [
            if (player.queue.isEmpty)
              SliverToBoxAdapter(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: const Text(
                    'Choose a song to start a related-song queue.',
                  ),
                ),
              )
            else
              SliverPadding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                sliver: SliverReorderableList(
                  proxyDecorator: (child, _, __) => Material(
                    elevation: 4,
                    child: child,
                  ),
                  itemCount: player.queue.length,
                  onReorderStart: (_) => _dragRevision = player.queueRevision,
                  onReorder: (from, drop) {
                    final to = drop > from ? drop - 1 : drop;
                    if (from != to)
                      _edited(
                        player.moveQueueItem(
                          from,
                          to,
                          revision: _dragRevision ?? revision,
                        ),
                      );
                    _dragRevision = null;
                  },
                  itemBuilder: (context, at) => QueueEdgeCard(
                      key: ObjectKey(player.queue[at]),
                      scroll: _scroll,
                      child: _tile(at, revision, drag: true)),
                ),
              ),
            SliverToBoxAdapter(
              child: Column(
                children: [
                  if (player.relatedError != null)
                    Padding(
                      padding: const EdgeInsets.all(8),
                      child: Text(player.relatedError!),
                    ),
                  if (player.queue.isNotEmpty)
                    Tooltip(
                        message: 'Find more songs related to this queue',
                        child: TextButton.icon(
                          onPressed: player.loadingRelated
                              ? null
                              : () => unawaited(player.replenish()),
                          icon: player.loadingRelated
                              ? const NeatieLoading(
                                  label: 'Finding songs', size: 16)
                              : const Icon(Icons.auto_awesome_outlined,
                                  size: 16),
                          label: Text(
                            player.loadingRelated
                                ? 'Finding songs'
                                : 'Discover more',
                          ),
                        )),
                  if (player.storageError != null)
                    Padding(
                      padding: const EdgeInsets.all(8),
                      child: Text(player.storageError!),
                    ),
                ],
              ),
            ),
          ],
        ));
  }

  Widget _tile(int at, int revision, {required bool drag}) {
    final player = widget.player, track = widget.player.queue[at];
    final active = at == player.index;
    final title = '${track['title'] ?? 'Untitled song'}';
    final liked = widget.isLiked?.call(track) == true;
    return QueueRemoveGesture(
        key: ObjectKey(track),
        player: player,
        index: at,
        revision: revision,
        child: Material(
            type: MaterialType.transparency,
            borderRadius: BorderRadius.circular(12),
            clipBehavior: Clip.antiAlias,
            child: ListTile(
                selected: active,
                selectedTileColor: NeatieDesign.accent.withValues(alpha: .06),
                hoverColor: NeatieDesign.accent.withValues(alpha: .08),
                leading: ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: ArtworkImage(
                        urls: player.api.artworkUrls(track, 'track'),
                        width: 44,
                        height: 44)),
                title:
                    Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text('${track['artist'] ?? track['channel'] ?? ''}',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                onTap: () {
                  if (revision != player.queueRevision) return;
                  if (!active)
                    unawaited(player.playIndex(at));
                  else if (!player.playing && !player.busy)
                    unawaited(player.play());
                },
                trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                  IconButton(
                      tooltip: '${liked ? 'Unlike' : 'Like'} $title',
                      icon:
                          Icon(liked ? Icons.favorite : Icons.favorite_border),
                      onPressed: () => unawaited(widget.onLike(track))),
                  PopupMenuButton<String>(
                      tooltip: 'Queue options for $title',
                      icon: const Icon(Icons.more_horiz),
                      onSelected: (action) {
                        if (revision != player.queueRevision || !widget.active)
                          return;
                        if (action == 'playlist') {
                          unawaited(widget.onAddToPlaylist!(track));
                          return;
                        }
                        _edited(switch (action) {
                          'next' => player.playNext(track,
                              fromIndex: at, revision: revision),
                          'up' => player.moveQueueItem(at, at - 1,
                              revision: revision),
                          'down' => player.moveQueueItem(at, at + 1,
                              revision: revision),
                          _ => player.removeQueueItem(at, revision: revision),
                        });
                      },
                      itemBuilder: (_) => [
                            if (widget.onAddToPlaylist != null)
                              const PopupMenuItem(
                                  value: 'playlist',
                                  child: Text('Add to playlist')),
                            PopupMenuItem(
                                value: 'next',
                                enabled: !active,
                                child: const Text('Play next')),
                            PopupMenuItem(
                                value: 'up',
                                enabled: !active && at > 0,
                                child: const Text('Move up')),
                            PopupMenuItem(
                                value: 'down',
                                enabled:
                                    !active && at + 1 < player.queue.length,
                                child: const Text('Move down')),
                            PopupMenuItem(
                                value: 'remove',
                                enabled: !active,
                                child: const Text('Remove from queue')),
                          ]),
                  if (drag)
                    active
                        ? const SizedBox(width: 24)
                        : ReorderableDragStartListener(
                            index: at,
                            child: const Tooltip(
                                message: 'Drag to reorder',
                                child: Icon(Icons.drag_handle))),
                ]))));
  }
}

/// A deliberate left swipe uses the same revision-checked queue edit as menus.
class QueueRemoveGesture extends StatefulWidget {
  const QueueRemoveGesture(
      {super.key,
      required this.player,
      required this.index,
      required this.revision,
      this.collapseSpace = true,
      required this.child});
  final WebPlayer player;
  final int index, revision;
  final bool collapseSpace;
  final Widget child;
  @override
  State<QueueRemoveGesture> createState() => _QueueRemoveGestureState();
}

class _QueueRemoveGestureState extends State<QueueRemoveGesture>
    with SingleTickerProviderStateMixin {
  late final _collapse = AnimationController(
      vsync: this, value: 1, duration: const Duration(milliseconds: 220));
  @override
  void dispose() {
    _collapse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final player = widget.player, index = widget.index;
        final revision = widget.revision;
        final track = index < player.queue.length ? player.queue[index] : null;
        double start = 0, distance = 0;
        return SizeTransition(
            sizeFactor: CurvedAnimation(
                parent: _collapse, curve: Curves.easeInOutCubic),
            child: Listener(
                onPointerDown: (event) {
                  start = event.position.dx;
                  distance = 0;
                },
                onPointerMove: (event) => distance = start - event.position.dx,
                onPointerUp: (event) => distance = start - event.position.dx,
                child: Dismissible(
                    key: ValueKey('queue-remove:$revision:$index'),
                    direction: index == player.index
                        ? DismissDirection.none
                        : DismissDirection.endToStart,
                    dismissThresholds: const {DismissDirection.endToStart: .65},
                    movementDuration: MediaQuery.disableAnimationsOf(context)
                        ? Duration.zero
                        : const Duration(milliseconds: 180),
                    confirmDismiss: (_) async {
                      if (distance < bounds.maxWidth * .5 || distance < 100)
                        return false;
                      if (revision != player.queueRevision ||
                          index == player.index) return false;
                      if (widget.collapseSpace &&
                          !MediaQuery.disableAnimationsOf(context)) {
                        try {
                          await _collapse.reverse().orCancel;
                        } on TickerCanceled {
                          return false;
                        }
                      }
                      if (!mounted) return false;
                      final retained = revision == player.queueRevision &&
                          index < player.queue.length &&
                          identical(player.queue[index], track);
                      if (!retained ||
                          !player.removeQueueItem(index, revision: revision))
                        _collapse.forward();
                      // Commit through the model after exit/collapse; stale edits undo visually.
                      return false;
                    },
                    background: Container(
                        alignment: Alignment.centerRight,
                        padding: const EdgeInsets.only(right: 24),
                        decoration: BoxDecoration(
                            color: Colors.redAccent.withValues(alpha: .12),
                            borderRadius: BorderRadius.circular(12)),
                        child: const Icon(Icons.remove_circle_outline,
                            color: Colors.redAccent)),
                    child: widget.child)));
      });
}

/// Filter each actual row as it crosses the viewport; no fixed glass overlay.
class QueueEdgeCard extends SingleChildRenderObjectWidget {
  const QueueEdgeCard({super.key, required this.scroll, required super.child});
  final ScrollController scroll;
  @override
  RenderObject createRenderObject(BuildContext context) =>
      _QueueEdgeRender(scroll);
  @override
  void updateRenderObject(
          BuildContext context, covariant _QueueEdgeRender renderObject) =>
      renderObject.scroll = scroll;
}

class _QueueEdgeRender extends RenderProxyBox {
  _QueueEdgeRender(this._scroll);
  ScrollController _scroll;
  double? _paintCenter, _paintOffset, _viewportHeight;
  bool _filtered = false;
  void _scrollChanged() {
    if (_paintCenter == null ||
        _paintOffset == null ||
        _viewportHeight == null ||
        !_scroll.hasClients) {
      markNeedsPaint();
      return;
    }
    final center = _paintCenter! - (_scroll.offset - _paintOffset!);
    if (_filtered || center < 56 || center > _viewportHeight! - 56)
      markNeedsPaint();
  }

  set scroll(ScrollController value) {
    if (identical(value, _scroll)) return;
    if (attached) _scroll.removeListener(_scrollChanged);
    _scroll = value;
    if (attached) _scroll.addListener(_scrollChanged);
    markNeedsPaint();
  }

  @override
  void attach(PipelineOwner owner) {
    super.attach(owner);
    _scroll.addListener(_scrollChanged);
  }

  @override
  void detach() {
    _scroll.removeListener(_scrollChanged);
    super.detach();
  }

  @override
  void paint(PaintingContext context, Offset offset) {
    final viewport = RenderAbstractViewport.maybeOf(this);
    if (viewport is! RenderBox) return super.paint(context, offset);
    final center = MatrixUtils.transformPoint(
            getTransformTo(viewport), size.center(Offset.zero))
        .dy;
    final height = (viewport as RenderBox).size.height;
    _paintCenter = center;
    _paintOffset = _scroll.hasClients ? _scroll.offset : 0;
    _viewportHeight = height;
    final distance = center < height / 2 ? center : height - center;
    final amount = (1 - distance / 56).clamp(0.0, 1.0);
    _filtered = amount > 0;
    if (amount == 0) return super.paint(context, offset);
    context.pushOpacity(offset, (255 * (1 - amount * .6)).round(),
        (context, offset) {
      context.pushLayer(
          ImageFilterLayer(
              imageFilter:
                  ui.ImageFilter.blur(sigmaX: amount * 4, sigmaY: amount * 4)),
          super.paint,
          offset);
    });
  }
}

/// One freely scrolling cover presentation for the full and expanded queues.
class QueueCoverBrowser extends StatefulWidget {
  const QueueCoverBrowser(
      {super.key,
      required this.player,
      this.startAtCurrent = false,
      this.frontCoverCenter});
  final WebPlayer player;
  final bool startAtCurrent;
  final double? frontCoverCenter;
  @override
  State<QueueCoverBrowser> createState() => _QueueCoverBrowserState();
}

class _QueueCoverBrowserState extends State<QueueCoverBrowser>
    with SingleTickerProviderStateMixin {
  final _sleeves = <int, Widget>{};
  double _sleeveSize = 0;
  late int selected;
  static const step = 80.0;
  late ScrollController controller;
  late List<Map<String, dynamic>> _queue;
  String? _playingId;
  double? _pendingOffset;
  double get _center => widget.player.queue.length * 50.0;
  late (int, int) stamp;
  late final _arrival = AnimationController(
      vsync: this, value: 1, duration: const Duration(milliseconds: 320));
  int get initial => widget.player.queue.isEmpty
      ? 0
      : (widget.player.index + (widget.startAtCurrent ? 0 : 1)) %
          widget.player.queue.length;
  @override
  void initState() {
    super.initState();
    selected = initial;
    _queue = List.of(widget.player.queue);
    _playingId = extractTrackId(widget.player.current);
    controller =
        ScrollController(initialScrollOffset: (_center + selected) * step);
    controller.addListener(_scrolled);
    stamp = (widget.player.queueRevision, widget.player.index);
    widget.player.addListener(_changed);
  }

  void _changed() {
    final next = (widget.player.queueRevision, widget.player.index);
    if (stamp == next) return;
    _sleeves.clear();
    final previous =
        _queue.isNotEmpty ? _queue[selected.clamp(0, _queue.length - 1)] : null;
    final playingId = extractTrackId(widget.player.current);
    final fraction = controller.hasClients
        ? controller.offset / step - (controller.offset / step).round()
        : 0.0;
    final retained =
        widget.player.queue.indexWhere((item) => identical(item, previous));
    final fallback = previous == null
        ? -1
        : widget.player.queue.indexWhere(
            (item) => extractTrackId(item) == extractTrackId(previous));
    final changedTrack = playingId != _playingId;
    final sameCount = _queue.length == widget.player.queue.length;
    final removedFront = previous != null && retained < 0 && fallback < 0;
    selected = changedTrack
        ? initial
        : retained >= 0
            ? retained
            : fallback >= 0
                ? fallback
                : selected.clamp(
                    0, (widget.player.queue.length - 1).clamp(0, 1 << 30));
    _playingId = playingId;
    _queue = List.of(widget.player.queue);
    stamp = next;
    if (removedFront && !MediaQuery.disableAnimationsOf(context))
      _arrival.forward(from: 0);
    if (changedTrack &&
        sameCount &&
        controller.hasClients &&
        !MediaQuery.disableAnimationsOf(context)) {
      final position = controller.offset / step;
      var distance = (selected - position) % _queue.length;
      if (distance > _queue.length / 2) distance -= _queue.length;
      final target = controller.offset + distance * step;
      selected = position.round() % _queue.length;
      setState(() {});
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && controller.hasClients && stamp == next)
          unawaited(controller.animateTo(target,
              duration: const Duration(milliseconds: 360),
              curve: Curves.easeInOutCubic));
      });
      return;
    }
    final target = (_center + selected + (changedTrack ? 0 : fraction)) * step;
    _pendingOffset = target;
    if (mounted) setState(() {});
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && controller.hasClients && _pendingOffset == target) {
        controller.jumpTo(target);
        setState(() => _pendingOffset = null);
      }
    });
  }

  void _scrolled() {
    if (widget.player.queue.isEmpty) return;
    final count = widget.player.queue.length;
    final at = (controller.offset / step).round() % count;
    if (mounted && selected != at) setState(() => selected = at);
    if (_pendingOffset == null &&
        (controller.offset < count * step * 2 ||
            controller.offset > count * step * 98)) {
      final target = (_center + (controller.offset / step) % count) * step;
      _pendingOffset = target;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && controller.hasClients && _pendingOffset == target) {
          controller.jumpTo(target);
          setState(() => _pendingOffset = null);
        }
      });
    }
  }

  @override
  void didUpdateWidget(covariant QueueCoverBrowser old) {
    super.didUpdateWidget(old);
    if (old.player != widget.player) {
      old.player.removeListener(_changed);
      widget.player.addListener(_changed);
      _changed();
    }
  }

  @override
  void dispose() {
    widget.player.removeListener(_changed);
    controller.dispose();
    _arrival.dispose();
    super.dispose();
  }

  void _select(int at, int revision) {
    final player = widget.player;
    if (revision != player.queueRevision || at < 0 || at >= player.queue.length)
      return;
    if (at != player.index)
      unawaited(player.playIndex(at));
    else if (!player.playing && !player.busy) unawaited(player.play());
  }

  @override
  Widget build(BuildContext context) {
    final player = widget.player;
    if (player.queue.isEmpty)
      return const Center(child: Text('Your queue is empty.'));
    final reversed = Theme.of(context).platform == TargetPlatform.iOS ||
        Theme.of(context).platform == TargetPlatform.android;
    selected = selected.clamp(0, player.queue.length - 1);
    final track = player.queue[selected], revision = player.queueRevision;
    return LayoutBuilder(builder: (context, bounds) {
      final size = (bounds.maxWidth - 40).clamp(100.0, 192.0);
      if (size != _sleeveSize) {
        _sleeves.clear();
        _sleeveSize = size;
      }
      Widget sleeve(int at) => _sleeves.putIfAbsent(
          at,
          () => RepaintBoundary(
              child: QueueRemoveGesture(
                  key: ValueKey(at),
                  player: player,
                  index: at % player.queue.length,
                  collapseSpace: false,
                  revision: revision,
                  child: _QueueSleeve(
                      urls: player.api.artworkUrls(
                          player.queue[at % player.queue.length], 'track'),
                      size: size,
                      label:
                          'Play queued ${player.queue[at % player.queue.length]['title'] ?? ''}',
                      onTap: () =>
                          _select(at % player.queue.length, revision)))));
      return Column(children: [
        Expanded(child: LayoutBuilder(builder: (context, viewport) {
          final frontTop = (widget.frontCoverCenter != null
                  ? widget.frontCoverCenter! - size / 2
                  : (viewport.maxHeight - size) * .65)
              .clamp(
                  0.0, (viewport.maxHeight - size).clamp(0.0, double.infinity));
          final backHeight = frontTop.clamp(0.0, 125.0);
          return Stack(children: [
            Positioned(
                left: (bounds.maxWidth - size) / 2,
                top: frontTop - backHeight,
                width: size,
                height: size + backHeight,
                child: ScrollConfiguration(
                    behavior: ScrollConfiguration.of(context)
                        .copyWith(scrollbars: false),
                    child: ClipRect(
                        child: SingleChildScrollView(
                            key: const ValueKey('queue-cover-scroll'),
                            controller: controller,
                            reverse: reversed,
                            physics: const BouncingScrollPhysics(
                                parent: AlwaysScrollableScrollPhysics()),
                            child: AnimatedBuilder(
                                animation:
                                    Listenable.merge([controller, _arrival]),
                                builder: (context, _) {
                                  final offset = _pendingOffset ??
                                      (controller.hasClients
                                          ? controller.offset
                                          : (_center + selected) * step);
                                  final position = offset / step;
                                  final front = position.floor();
                                  _sleeves.removeWhere(
                                      (at, _) => at < front || at > front + 5);
                                  return SizedBox(
                                      height: size +
                                          backHeight +
                                          (player.queue.length * 101 - 1) *
                                              step,
                                      child: Stack(
                                          clipBehavior: Clip.none,
                                          children: [
                                            // Back sleeves paint first, exposing a narrow strip above each.
                                            for (var at = front + (player.queue.length - 1).clamp(0, 5);
                                                at >= front;
                                                at--)
                                              Positioned(
                                                  top: (reversed
                                                          ? (player.queue.length * 101 - 1) *
                                                                  step -
                                                              offset
                                                          : offset) +
                                                      backHeight -
                                                      (at < position
                                                          ? (at - position) *
                                                              size *
                                                              1.5
                                                          : (at - position + 1 - Curves.easeOutCubic.transform(_arrival.value)) *
                                                              25),
                                                  left: 0,
                                                  width: size,
                                                  height: size,
                                                  child: Transform.scale(
                                                      alignment:
                                                          Alignment.topCenter,
                                                      scale:
                                                          (1 - ((at - position).abs() + 1 - Curves.easeOutCubic.transform(_arrival.value)) * .065)
                                                              .clamp(.65, 1.0),
                                                      child: Container(
                                                          color: const Color(0xff111212),
                                                          foregroundDecoration: BoxDecoration(color: Colors.black.withValues(alpha: ((at - position) * .12).clamp(0.0, .55))),
                                                          child: sleeve(at))))
                                          ]));
                                })))))
          ]);
        })),
        const SizedBox(height: 20),
        Text('${track['title'] ?? ''}',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500)),
        const SizedBox(height: 8),
        Text('${track['artist'] ?? track['channel'] ?? ''}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 12, color: NeatieDesign.muted)),
        const SizedBox(height: 14),
        Text('${selected + 1} / ${player.queue.length}',
            style: const TextStyle(fontSize: 10, color: NeatieDesign.muted)),
      ]);
    });
  }
}

class _QueueSleeve extends StatefulWidget {
  const _QueueSleeve(
      {required this.urls,
      required this.size,
      required this.label,
      required this.onTap});
  final List<String> urls;
  final double size;
  final String label;
  final VoidCallback onTap;
  @override
  State<_QueueSleeve> createState() => _QueueSleeveState();
}

class _QueueSleeveState extends State<_QueueSleeve> {
  bool hovered = false;
  @override
  Widget build(BuildContext context) => AnimatedScale(
      scale: hovered && !MediaQuery.disableAnimationsOf(context) ? 1.035 : 1,
      duration: MediaQuery.disableAnimationsOf(context)
          ? Duration.zero
          : const Duration(milliseconds: 200),
      child: Semantics(
          button: true,
          label: widget.label,
          onTap: widget.onTap,
          excludeSemantics: true,
          child: Material(
              type: MaterialType.transparency,
              borderRadius: BorderRadius.circular(6),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                  onTap: widget.onTap,
                  onHover: (value) => setState(() => hovered = value),
                  onFocusChange: (value) => setState(() => hovered = value),
                  child: ArtworkImage(
                      urls: widget.urls,
                      width: widget.size,
                      height: widget.size)))));
}
