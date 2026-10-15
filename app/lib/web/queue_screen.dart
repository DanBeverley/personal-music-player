import 'dart:async';
import 'package:flutter/material.dart';
import '../logic/track_metadata.dart';
import 'collection_search.dart';
import 'artwork_image.dart';
import 'design_system.dart';
import 'web_player.dart';

class QueueScreen extends StatefulWidget {
  const QueueScreen(
      {super.key,
      required this.player,
      required this.onLike,
      this.isLiked,
      this.active = true});
  final WebPlayer player;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final bool Function(Map<String, dynamic>)? isLiked;
  final bool active;
  @override
  State<QueueScreen> createState() => _QueueScreenState();
}

class _QueueScreenState extends State<QueueScreen> {
  int? _dragRevision;

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
                        CatalogTrackSearch(
                            key: ValueKey(
                                'queue-search:${player.api.userScopeId}'),
                            api: player.api,
                            active: widget.active,
                            label: 'Find songs to add to your queue',
                            isAdded: (track) => player.queue.any((item) =>
                                extractTrackId(item) == extractTrackId(track)),
                            onAdd: (track) async => player.addToQueue(track)),
                        const SizedBox(height: 8),
                        Text('${player.queue.length} songs · drag to reorder'),
                      ]))),
          if (player.queue.isEmpty)
            SliverToBoxAdapter(
                child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: const Text(
                        'Choose a song to start a related-song queue.')))
          else
            SliverPadding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                sliver: SliverReorderableList(
                    proxyDecorator: (child, _, __) => Material(
                        elevation: 4,
                        color: Theme.of(context).colorScheme.surface,
                        child: child),
                    itemCount: player.queue.length,
                    onReorderStart: (_) => _dragRevision = player.queueRevision,
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
            if (player.queue.isNotEmpty)
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
