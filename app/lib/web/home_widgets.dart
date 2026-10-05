import 'dart:async';
import 'package:flutter/material.dart';
import 'artwork_image.dart';
import 'music_api.dart';

/// Presentation only: the backend owns recommendations, lanes and readiness.
class HomeArtwork extends StatelessWidget {
  const HomeArtwork(
      {super.key,
      required this.api,
      required this.item,
      required this.kind,
      required this.size});
  final MusicApi api;
  final Map<String, dynamic> item;
  final String kind;
  final double size;

  @override
  Widget build(BuildContext context) {
    final images = <String>{};
    if (kind == 'mix' || kind == 'radio') {
      for (final value in item['collage_images'] is List
          ? item['collage_images'] as List
          : const []) {
        final uri = api.artworkUri(value);
        if (uri != null) images.add(uri.toString());
      }
      // A mix may use its exact track covers. Artist radios retain portraits.
      if (kind == 'mix' && images.length < 2) {
        for (final track in MusicApi.tracks(item['tracks'] ?? item['items'])) {
          images.addAll(api.artworkUrls(track, 'track').take(1));
        }
      }
    }
    Widget cover;
    if (images.length >= 2) {
      final urls = images.take(4).toList();
      cover = Column(children: [
        for (var row = 0; row < (urls.length <= 2 ? 1 : 2); row++)
          Expanded(
              child: Row(children: [
            for (var column = 0; column < 2; column++)
              Expanded(
                  child: row * 2 + column < urls.length
                      ? ArtworkImage(
                          urls: [urls[row * 2 + column]],
                          width: size / 2,
                          height: size)
                      : const Center(child: Icon(Icons.music_note))),
          ])),
      ]);
    } else {
      cover = ArtworkImage(
          urls: [...images, ...api.artworkUrls(item, kind)],
          width: size,
          height: size,
          icon: kind == 'artist' || kind == 'radio'
              ? Icons.person_outline
              : Icons.music_note);
    }
    return SizedBox(
        width: size,
        height: size,
        child: ClipRRect(
            borderRadius:
                BorderRadius.circular(kind == 'artist' ? size / 2 : 14),
            child: ColoredBox(
                color: Theme.of(context).colorScheme.surfaceContainerHighest,
                child: cover)));
  }
}

class HomePickCard extends StatelessWidget {
  const HomePickCard(
      {super.key,
      required this.api,
      required this.track,
      required this.onPlay});
  final MusicApi api;
  final Map<String, dynamic> track;
  final VoidCallback? onPlay;
  @override
  Widget build(BuildContext context) => Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onPlay,
        child: Padding(
            padding: const EdgeInsets.all(14),
            child: Row(children: [
              HomeArtwork(api: api, item: track, kind: 'track', size: 78),
              const SizedBox(width: 14),
              Expanded(
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                    Text("Today's Pick",
                        style: Theme.of(context)
                            .textTheme
                            .labelMedium
                            ?.copyWith(
                                color: Theme.of(context).colorScheme.primary)),
                    const SizedBox(height: 5),
                    Text('${track['title'] ?? ''}',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 4),
                    Text('${track['artist'] ?? track['channel'] ?? ''}',
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                  ])),
              IconButton.filled(
                  tooltip: "Play Today's Pick",
                  onPressed: onPlay,
                  icon: const Icon(Icons.play_arrow)),
            ])),
      ));
}

class HomeAlbumHero extends StatefulWidget {
  const HomeAlbumHero(
      {super.key,
      required this.api,
      required this.albums,
      required this.active,
      required this.onOpen,
      required this.onPlay});
  final MusicApi api;
  final List<Map<String, dynamic>> albums;
  final bool active;
  final ValueChanged<Map<String, dynamic>> onOpen, onPlay;
  @override
  State<HomeAlbumHero> createState() => _HomeAlbumHeroState();
}

class _HomeAlbumHeroState extends State<HomeAlbumHero> {
  final controller = PageController(keepPage: false);
  Timer? timer;
  int index = 0;
  bool reducedMotion = false;
  bool interacting = false;
  String _ids(List<Map<String, dynamic>> albums) => albums
      .map((a) => a['id'] ?? a['album_id'] ?? a['browseId'] ?? a['title'])
      .join('|');
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    reducedMotion = MediaQuery.disableAnimationsOf(context);
    _schedule();
  }

  @override
  void didUpdateWidget(covariant HomeAlbumHero old) {
    super.didUpdateWidget(old);
    if (_ids(old.albums) != _ids(widget.albums)) {
      index = 0;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && controller.hasClients) controller.jumpToPage(0);
      });
    }
    if (old.active != widget.active || _ids(old.albums) != _ids(widget.albums))
      _schedule();
  }

  void _schedule() {
    timer?.cancel();
    if (!mounted ||
        !widget.active ||
        reducedMotion ||
        interacting ||
        widget.albums.length < 2) return;
    timer = Timer(const Duration(seconds: 7), () {
      if (mounted && controller.hasClients && widget.active)
        _go((index + 1) % widget.albums.length);
    });
  }

  void _go(int target) {
    timer?.cancel();
    if (!controller.hasClients) return;
    if (reducedMotion)
      controller.jumpToPage(target);
    else
      unawaited(controller
          .animateToPage(target,
              duration: const Duration(milliseconds: 650),
              curve: Curves.easeInOutCubic)
          .whenComplete(_schedule));
  }

  @override
  void dispose() {
    timer?.cancel();
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.albums.isEmpty) return const SizedBox.shrink();
    return LayoutBuilder(builder: (context, constraints) {
      final compact = constraints.maxWidth < 520;
      final size = compact ? 104.0 : 160.0;
      final extraScale = (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
          .clamp(0.0, double.infinity);
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(
            padding: const EdgeInsets.only(top: 24, bottom: 10),
            child: Text('Featured Albums',
                style: Theme.of(context).textTheme.titleLarge)),
        SizedBox(
            height: (compact ? 280 : 252) + extraScale * (compact ? 160 : 120),
            child: Listener(
                onPointerDown: (_) {
                  interacting = true;
                  timer?.cancel();
                },
                onPointerUp: (_) {
                  interacting = false;
                  _schedule();
                },
                onPointerCancel: (_) {
                  interacting = false;
                  _schedule();
                },
                child: PageView.builder(
                    controller: controller,
                    itemCount: widget.albums.length,
                    onPageChanged: (value) {
                      setState(() => index = value);
                      _schedule();
                    },
                    itemBuilder: (context, page) {
                      final album = widget.albums[page];
                      final title = '${album['title'] ?? album['name'] ?? ''}';
                      return Card(
                        clipBehavior: Clip.antiAlias,
                        child: InkWell(
                            onTap: () => widget.onOpen(album),
                            child: Padding(
                                padding: EdgeInsets.all(compact ? 14 : 24),
                                child: Row(children: [
                                  HomeArtwork(
                                      api: widget.api,
                                      item: album,
                                      kind: 'album',
                                      size: size),
                                  const SizedBox(width: 18),
                                  Expanded(
                                      child: Column(
                                          mainAxisAlignment:
                                              MainAxisAlignment.center,
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                        Text(title,
                                            maxLines: compact ? 2 : 3,
                                            overflow: TextOverflow.ellipsis,
                                            style: compact
                                                ? Theme.of(context)
                                                    .textTheme
                                                    .titleLarge
                                                : Theme.of(context)
                                                    .textTheme
                                                    .headlineSmall),
                                        const SizedBox(height: 6),
                                        Text(
                                            '${album['artist'] ?? album['artist_name'] ?? album['channel'] ?? ''}',
                                            maxLines: 2,
                                            overflow: TextOverflow.ellipsis),
                                        const SizedBox(height: 14),
                                        Wrap(
                                            spacing: 8,
                                            runSpacing: 4,
                                            children: [
                                              FilledButton.icon(
                                                  onPressed: () =>
                                                      widget.onPlay(album),
                                                  icon: const Icon(
                                                      Icons.play_arrow),
                                                  label: const Text('Play')),
                                              TextButton(
                                                  onPressed: () =>
                                                      widget.onOpen(album),
                                                  child:
                                                      const Text('Open album')),
                                            ]),
                                      ])),
                                ]))),
                      );
                    }))),
        if (widget.albums.length > 1)
          SizedBox(
              height: 38,
              child: ListView(
                  key: const PageStorageKey('home-album-indicators'),
                  scrollDirection: Axis.horizontal,
                  children: [
                    for (var i = 0; i < widget.albums.length; i++)
                      IconButton(
                          tooltip: 'Show album ${i + 1}',
                          isSelected: i == index,
                          onPressed: () => _go(i),
                          icon: Icon(
                              i == index ? Icons.circle : Icons.circle_outlined,
                              size: 10)),
                  ])),
      ]);
    });
  }
}

class HomeArtistCard extends StatelessWidget {
  const HomeArtistCard(
      {super.key, required this.api, required this.item, required this.onOpen});
  final MusicApi api;
  final Map<String, dynamic> item;
  final VoidCallback? onOpen;
  @override
  Widget build(BuildContext context) => SizedBox(
      width: 136,
      child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: onOpen,
          child: Padding(
              padding: const EdgeInsets.all(8),
              child: Column(children: [
                HomeArtwork(api: api, item: item, kind: 'artist', size: 108),
                const SizedBox(height: 10),
                Text(
                    '${item['name'] ?? item['artist_name'] ?? item['title'] ?? ''}',
                    textAlign: TextAlign.center,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.titleSmall),
              ]))));
}

class HomeCollectionCard extends StatelessWidget {
  const HomeCollectionCard(
      {super.key,
      required this.api,
      required this.item,
      required this.kind,
      required this.title,
      required this.subtitle,
      required this.onOpen,
      required this.onPlay});
  final MusicApi api;
  final Map<String, dynamic> item;
  final String kind, title, subtitle;
  final VoidCallback? onOpen, onPlay;
  @override
  Widget build(BuildContext context) => SizedBox(
      width: kind == 'radio' ? 196 : 176,
      child: Card(
          clipBehavior: Clip.antiAlias,
          child: InkWell(
              onTap: onOpen,
              child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Center(
                            child: HomeArtwork(
                                api: api,
                                item: item,
                                kind: kind,
                                size: kind == 'radio' ? 154 : 134)),
                        const SizedBox(height: 8),
                        Text(title,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(context).textTheme.titleSmall),
                        const Spacer(),
                        Row(children: [
                          Expanded(
                              child: Text(subtitle,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style:
                                      Theme.of(context).textTheme.bodySmall)),
                          IconButton(
                              tooltip: 'Play $title',
                              onPressed: onPlay,
                              icon: Icon(kind == 'radio'
                                  ? Icons.play_circle_outline
                                  : Icons.play_circle_fill)),
                        ]),
                      ])))));
}

class HomeTrackStrip extends StatelessWidget {
  const HomeTrackStrip(
      {super.key,
      required this.api,
      required this.tracks,
      required this.onPlay,
      required this.onLike,
      required this.onMenu,
      required this.isLiked});
  final MusicApi api;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<int> onPlay;
  final ValueChanged<Map<String, dynamic>> onLike, onMenu;
  final bool Function(Map<String, dynamic>) isLiked;
  @override
  Widget build(BuildContext context) => SizedBox(
      height: 280 +
          (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
                  .clamp(0.0, double.infinity) *
              70,
      child: ListView.builder(
          scrollDirection: Axis.horizontal,
          itemCount: tracks.length,
          itemBuilder: (context, index) {
            final track = tracks[index];
            final title = '${track['title'] ?? ''}';
            return SizedBox(
                width: 164,
                child: Card(
                    clipBehavior: Clip.antiAlias,
                    child: InkWell(
                        onTap: track['playable'] == false
                            ? null
                            : () => onPlay(index),
                        child: Padding(
                            padding: const EdgeInsets.all(10),
                            child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  HomeArtwork(
                                      api: api,
                                      item: track,
                                      kind: 'track',
                                      size: 136),
                                  const SizedBox(height: 8),
                                  Text(title,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: Theme.of(context)
                                          .textTheme
                                          .titleSmall),
                                  Text(
                                      '${track['artist'] ?? track['channel'] ?? ''}',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodySmall),
                                  const Spacer(),
                                  Row(
                                      mainAxisAlignment: MainAxisAlignment.end,
                                      children: [
                                        IconButton(
                                            visualDensity:
                                                VisualDensity.compact,
                                            tooltip:
                                                '${isLiked(track) ? 'Unlike' : 'Like'} $title',
                                            onPressed: () => onLike(track),
                                            icon: Icon(isLiked(track)
                                                ? Icons.favorite
                                                : Icons.favorite_border)),
                                        IconButton(
                                            visualDensity:
                                                VisualDensity.compact,
                                            tooltip: 'Options for $title',
                                            onPressed: () => onMenu(track),
                                            icon: const Icon(Icons.more_horiz)),
                                      ]),
                                ])))));
          }));
}
