import 'dart:async';
import 'package:flutter/material.dart';
import 'collection_search.dart';
import 'artwork_image.dart';
import 'design_system.dart';
import 'web_player.dart';

class QueueScreen extends StatefulWidget {
  const QueueScreen(
      {super.key, required this.player, required this.onLike, this.isLiked});
  final WebPlayer player;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final bool Function(Map<String, dynamic>)? isLiked;
  @override
  State<QueueScreen> createState() => _QueueScreenState();
}

class _QueueScreenState extends State<QueueScreen> {
  final _search = TextEditingController();
  int? _dragRevision;
  List<Map<String, dynamic>>? _filteredQueue;
  String _filteredQuery = '';
  List<int> _filteredIndices = [];
  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  void _edited(bool success) {
    if (!success && mounted)
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('The queue changed. Please try again.')));
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
      animation: widget.player,
      builder: (context, _) {
        final player = widget.player, revision = player.queueRevision;
        if (!identical(_filteredQueue, player.queue) ||
            _filteredQuery != _search.text) {
          _filteredQueue = player.queue;
          _filteredQuery = _search.text;
          _filteredIndices = matchingTrackIndices(player.queue, _search.text);
        }
        final visible = _filteredIndices;
        final filtering = _search.text.trim().isNotEmpty;
        return CustomScrollView(slivers: [
          SliverToBoxAdapter(
              child: Padding(
                  padding: const EdgeInsets.fromLTRB(24, 24, 24, 12),
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        EditorialHeading(
                            title: 'Up next.',
                            eyebrow: 'YOUR QUEUE',
                            subtitle: player.title),
                        const SizedBox(height: 16),
                        CollectionSearchField(
                            controller: _search,
                            label: 'Search this queue',
                            onChanged: (_) => setState(() {})),
                        const SizedBox(height: 8),
                        Text(filtering
                            ? '${visible.length} of ${player.queue.length} songs'
                            : '${player.queue.length} songs · drag to reorder'),
                      ]))),
          if (player.queue.isEmpty || visible.isEmpty)
            SliverToBoxAdapter(
                child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Text(player.queue.isEmpty
                        ? 'Choose a song to start a related-song queue.'
                        : 'No matching songs in this queue.')))
          else
            SliverPadding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                sliver: filtering
                    ? SliverList.builder(
                        itemCount: visible.length,
                        itemBuilder: (context, at) =>
                            _tile(visible[at], revision, drag: false))
                    : SliverReorderableList(
                        proxyDecorator: (child, _, __) => Material(
                            elevation: 4,
                            color: Theme.of(context).colorScheme.surface,
                            child: child),
                        itemCount: player.queue.length,
                        onReorderStart: (_) =>
                            _dragRevision = player.queueRevision,
                        onReorder: (from, drop) {
                          final to = drop > from ? drop - 1 : drop;
                          if (from != to)
                            _edited(player.moveQueueItem(from, to,
                                revision: _dragRevision ?? revision));
                          _dragRevision = null;
                        },
                        itemBuilder: (context, at) =>
                            _tile(at, revision, drag: true))),
          SliverToBoxAdapter(
              child: Column(children: [
            if (player.relatedError != null)
              Padding(
                  padding: const EdgeInsets.all(8),
                  child: Text(player.relatedError!)),
            if (player.queue.isNotEmpty &&
                (player.hasMore || player.relatedError != null))
              TextButton(
                  onPressed: player.loadingRelated
                      ? null
                      : () => unawaited(player.replenish()),
                  child: Text(player.loadingRelated
                      ? 'Finding related songs…'
                      : 'Load related songs')),
            if (player.storageError != null)
              Padding(
                  padding: const EdgeInsets.all(8),
                  child: Text(player.storageError!)),
          ])),
        ]);
      });

  Widget _tile(int at, int revision, {required bool drag}) {
    final player = widget.player, track = widget.player.queue[at];
    final active = at == player.index;
    final title = '${track['title'] ?? 'Untitled song'}';
    final liked = widget.isLiked?.call(track) == true;
    return ListTile(
        key: ObjectKey(track),
        selected: active,
        leading: ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: ArtworkImage(
                urls: player.api.artworkUrls(track, 'track'),
                width: 44,
                height: 44)),
        title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
            '${active ? 'Current song · ' : ''}${track['artist'] ?? track['channel'] ?? ''}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis),
        onTap: () {
          if (revision == player.queueRevision) unawaited(player.playIndex(at));
        },
        trailing: Row(mainAxisSize: MainAxisSize.min, children: [
          IconButton(
              tooltip: '${liked ? 'Unlike' : 'Like'} $title',
              icon: Icon(liked ? Icons.favorite : Icons.favorite_border),
              onPressed: () => unawaited(widget.onLike(track))),
          PopupMenuButton<String>(
              tooltip: 'Queue options for $title',
              icon: const Icon(Icons.more_horiz),
              onSelected: (action) => _edited(switch (action) {
                    'next' =>
                      player.playNext(track, fromIndex: at, revision: revision),
                    'up' =>
                      player.moveQueueItem(at, at - 1, revision: revision),
                    'down' =>
                      player.moveQueueItem(at, at + 1, revision: revision),
                    _ => player.removeQueueItem(at, revision: revision),
                  }),
              itemBuilder: (_) => [
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
                        enabled: !active && at + 1 < player.queue.length,
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
        ]));
  }
}
