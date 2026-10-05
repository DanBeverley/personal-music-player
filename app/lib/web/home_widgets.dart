import 'dart:async';
import 'package:flutter/material.dart';
import 'artwork_image.dart';
import 'music_api.dart';
import 'design_system.dart';
import 'editorial_placeholder.dart';

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
          placeholder: EditorialPlaceholder(
              label: '${item['title'] ?? item['name'] ?? ''}',
              artist: kind == 'artist' || kind == 'radio'),
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
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final compact = bounds.maxWidth < 580;
        final cover = HomeArtwork(
            api: api, item: track, kind: 'track', size: compact ? 112 : 210);
        final copy =
            Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text("Today's Pick",
              style: NeatieDesign.eyebrow.copyWith(color: NeatieDesign.accent)),
          const SizedBox(height: 14),
          Text('${track['title'] ?? ''}',
              maxLines: compact ? 3 : 2,
              overflow: TextOverflow.ellipsis,
              style: NeatieDesign.display(compact ? 30 : 46)),
          const SizedBox(height: 12),
          Text('${track['artist'] ?? track['channel'] ?? ''}',
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: NeatieDesign.muted, fontSize: 14)),
        ]);
        final play = Tooltip(
            message: "Play Today's Pick",
            child: FilledButton.icon(
                onPressed: onPlay,
                icon: const Icon(Icons.play_arrow_rounded),
                label: const Text('Press play')));
        return Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: EditorialSurface(
                radius: 26,
                child: DecoratedBox(
                    decoration: const BoxDecoration(
                        gradient: LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [Color(0xff202723), Color(0xff171c1b)])),
                    child: Padding(
                        padding: EdgeInsets.all(compact ? 22 : 32),
                        child: compact
                            ? Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                    Row(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          cover,
                                          const SizedBox(width: 18),
                                          Expanded(child: copy)
                                        ]),
                                    const SizedBox(height: 22),
                                    play,
                                  ])
                            : Row(children: [
                                cover,
                                const SizedBox(width: 36),
                                Expanded(
                                    child: Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                      copy,
                                      const SizedBox(height: 24),
                                      play
                                    ]))
                              ])))));
      });
}

class HomeAlbumHero extends StatefulWidget {
  const HomeAlbumHero(
      {super.key,
      required this.api,
      required this.albums,
      required this.active,
      required this.onOpen,
      required this.onPlay,
      this.action});
  final MusicApi api;
  final List<Map<String, dynamic>> albums;
  final bool active;
  final ValueChanged<Map<String, dynamic>> onOpen, onPlay;
  final Widget? action;
  @override
  State<HomeAlbumHero> createState() => _HomeAlbumHeroState();
}

class _HomeAlbumHeroState extends State<HomeAlbumHero> {
  final controller = PageController(keepPage: false);
  Timer? timer;
  int index = 0;
  bool reducedMotion = false;
  bool interacting = false;
  bool moving = false;
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
        moving ||
        widget.albums.length < 2) return;
    timer = Timer(const Duration(seconds: 7), () {
      if (mounted && controller.hasClients && widget.active) _go(index + 1);
    });
  }

  void _go(int target) {
    timer?.cancel();
    if (!controller.hasClients || moving) return;
    if (reducedMotion) {
      controller.jumpToPage(target);
      _schedule();
    } else {
      moving = true;
      unawaited(controller
          .animateToPage(target,
              duration: const Duration(milliseconds: 850),
              curve: Curves.easeInOutCubic)
          .whenComplete(() {
        if (!mounted) return;
        moving = false;
        _settleLoop();
      }));
    }
  }

  void _settleLoop() {
    if (!mounted || !controller.hasClients) return;
    // A duplicate first card makes last -> first one forward swipe. Reset only
    // after it settles, so the jump is visually identical and never traverses
    // the intervening cards backwards.
    if (controller.page == widget.albums.length.toDouble())
      controller.jumpToPage(0);
    _schedule();
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
      final size = compact ? 100.0 : 204.0;
      final extraScale = (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
          .clamp(0.0, double.infinity);
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SectionHeading(title: 'Featured Albums', action: widget.action),
        SizedBox(
            height: (compact ? 306 : 300) + extraScale * (compact ? 180 : 150),
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
                child: NotificationListener<ScrollNotification>(
                    onNotification: (notice) {
                      if (notice.depth != 0) return false;
                      if (notice is ScrollStartNotification) timer?.cancel();
                      if (notice is ScrollEndNotification && !moving) {
                        WidgetsBinding.instance
                            .addPostFrameCallback((_) => _settleLoop());
                      }
                      return false;
                    },
                    child: PageView.builder(
                        controller: controller,
                        itemCount: widget.albums.length +
                            (widget.albums.length > 1 ? 1 : 0),
                        onPageChanged: (value) {
                          setState(() => index = value % widget.albums.length);
                        },
                        itemBuilder: (context, page) {
                          final album =
                              widget.albums[page % widget.albums.length];
                          final title =
                              '${album['title'] ?? album['name'] ?? ''}';
                          return ClipRRect(
                              borderRadius: BorderRadius.circular(26),
                              child: Stack(fit: StackFit.expand, children: [
                                ArtworkImage(
                                    urls:
                                        widget.api.artworkUrls(album, 'album'),
                                    width: constraints.maxWidth,
                                    height: 320,
                                    placeholder: const ColoredBox(
                                        color: NeatieDesign.raised)),
                                const DecoratedBox(
                                    decoration: BoxDecoration(
                                        gradient: LinearGradient(
                                            begin: Alignment.centerLeft,
                                            end: Alignment.centerRight,
                                            colors: [
                                      Color(0xfa111916),
                                      Color(0xe6111715),
                                      Color(0x9c111916)
                                    ]))),
                                InkWell(
                                    onTap: () => widget.onOpen(album),
                                    child: Padding(
                                        padding:
                                            EdgeInsets.all(compact ? 20 : 32),
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
                                                Text('IN THE SPOTLIGHT',
                                                    style: NeatieDesign.eyebrow
                                                        .copyWith(
                                                            color: NeatieDesign
                                                                .accent)),
                                                const SizedBox(height: 12),
                                                Text(title,
                                                    maxLines: compact ? 2 : 3,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: NeatieDesign.display(
                                                        compact ? 27 : 42)),
                                                const SizedBox(height: 6),
                                                Text(
                                                    '${album['artist'] ?? album['artist_name'] ?? album['channel'] ?? ''}',
                                                    maxLines: 2,
                                                    overflow:
                                                        TextOverflow.ellipsis),
                                                const SizedBox(height: 14),
                                                Wrap(
                                                    spacing: 8,
                                                    runSpacing: 4,
                                                    children: [
                                                      FilledButton.icon(
                                                          onPressed: () =>
                                                              widget.onPlay(
                                                                  album),
                                                          icon: const Icon(
                                                              Icons.play_arrow),
                                                          label: const Text(
                                                              'Play')),
                                                      TextButton(
                                                          onPressed: () =>
                                                              widget.onOpen(
                                                                  album),
                                                          child: const Text(
                                                              'Open album')),
                                                    ]),
                                              ])),
                                        ]))),
                              ]));
                        })))),
        if (widget.albums.length > 1)
          SizedBox(
              height: 44,
              child: ListView(
                  key: const PageStorageKey('home-album-indicators'),
                  scrollDirection: Axis.horizontal,
                  children: [
                    for (var i = 0; i < widget.albums.length; i++)
                      IconButton(
                          tooltip: 'Show album ${i + 1}',
                          isSelected: i == index,
                          onPressed: () => _go(i),
                          icon: AnimatedContainer(
                              duration: reducedMotion
                                  ? Duration.zero
                                  : const Duration(milliseconds: 300),
                              curve: Curves.easeOutCubic,
                              width: i == index ? 24 : 6,
                              height: 6,
                              decoration: BoxDecoration(
                                  color: i == index
                                      ? NeatieDesign.accent
                                      : NeatieDesign.line,
                                  borderRadius: BorderRadius.circular(6)))),
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
      width: 154,
      child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: onOpen,
          child: Padding(
              padding: const EdgeInsets.all(8),
              child: Column(children: [
                Container(
                    padding: const EdgeInsets.all(5),
                    decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(color: NeatieDesign.line)),
                    child: HomeArtwork(
                        api: api, item: item, kind: 'artist', size: 116)),
                const SizedBox(height: 14),
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
      width: kind == 'radio' ? 224 : 204,
      child: Padding(
          padding: const EdgeInsets.only(right: 14, bottom: 4),
          child: EditorialSurface(
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
                                    size: kind == 'radio' ? 182 : 162)),
                            const SizedBox(height: 14),
                            Text(kind == 'radio' ? 'RADIO' : 'MIX',
                                style: NeatieDesign.eyebrow
                                    .copyWith(fontSize: 9, letterSpacing: 1.6)),
                            const SizedBox(height: 6),
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
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodySmall)),
                              IconButton(
                                  tooltip: 'Play $title',
                                  onPressed: onPlay,
                                  icon: Icon(kind == 'radio'
                                      ? Icons.play_circle_outline
                                      : Icons.play_circle_fill)),
                            ]),
                          ]))))));
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
      height: 264 +
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
                width: 184,
                child: Padding(
                    padding: const EdgeInsets.only(right: 14),
                    child: InkWell(
                        borderRadius: BorderRadius.circular(16),
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
                                      size: 148),
                                  const SizedBox(height: 12),
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
