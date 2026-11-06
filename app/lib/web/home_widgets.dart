import 'dart:async';
import 'dart:ui' as ui;
import '../logic/track_metadata.dart';
import 'package:flutter/material.dart';
import 'artwork_image.dart';
import 'music_api.dart';
import 'design_system.dart';
import 'browse_widgets.dart';
import 'editorial_placeholder.dart';

class GuestHome extends StatelessWidget {
  const GuestHome({super.key, required this.onSignIn, required this.onSearch});
  final VoidCallback onSignIn, onSearch;
  @override
  Widget build(BuildContext context) =>
      ListView(padding: const EdgeInsets.fromLTRB(24, 48, 24, 40), children: [
        const Text('NEATIE / YOUR MUSIC', style: NeatieDesign.eyebrow),
        const SizedBox(height: 28),
        Text('Make yourself at home.', style: NeatieDesign.display(48)),
        const SizedBox(height: 18),
        const Text(
            'Search and listen freely. Sign in for a Home shaped by your music, saved likes and playlists.',
            style: TextStyle(color: NeatieDesign.muted, height: 1.7)),
        const SizedBox(height: 28),
        Wrap(spacing: 12, runSpacing: 12, children: [
          FilledButton(onPressed: onSignIn, child: const Text('Sign in')),
          TextButton.icon(
              onPressed: onSearch,
              icon: const Icon(Icons.search),
              label: const Text('Explore music')),
        ]),
      ]);
}

class HomeLikesSection extends StatelessWidget {
  const HomeLikesSection(
      {super.key,
      required this.api,
      required this.tracks,
      required this.onOpen,
      required this.onPlay,
      required this.onShuffle});
  final MusicApi api;
  final List<Map<String, dynamic>> tracks;
  final VoidCallback onOpen, onShuffle;
  final ValueChanged<int> onPlay;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.symmetric(vertical: 24),
      child: Column(children: [
        ArtworkSurface(
            artwork: tracks.isEmpty
                ? null
                : (size) => HomeArtwork(
                    api: api, item: tracks.first, kind: 'track', size: size),
            child: Material(
                color: Colors.transparent,
                child: InkWell(
                    onTap: onOpen,
                    child: Padding(
                        padding: const EdgeInsets.all(22),
                        child: Row(children: [
                          ExcludeSemantics(
                              child: SizedBox(
                                  width: 62,
                                  height: 66,
                                  child: Stack(
                                      alignment: Alignment.center,
                                      children: [
                                        for (var i = 2; i >= 0; i--)
                                          Transform.translate(
                                              offset: Offset(-i * 3.0, i * 6.0),
                                              child: Icon(
                                                  Icons.favorite_border_rounded,
                                                  size: 52,
                                                  color: Color.lerp(
                                                      const Color(0xffe69e87),
                                                      NeatieDesign.accent,
                                                      i / 3))),
                                      ]))),
                          const SizedBox(width: 20),
                          Expanded(
                              child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                Text('Your likes',
                                    style: NeatieDesign.display(32)),
                                const SizedBox(height: 6),
                                Text(
                                    '${tracks.length} songs, unmistakably yours.',
                                    style: const TextStyle(
                                        color: NeatieDesign.muted,
                                        fontSize: 12)),
                              ])),
                          IconButton.filled(
                              tooltip: 'Shuffle your likes',
                              onPressed: tracks.isEmpty ? null : onShuffle,
                              icon: const Icon(Icons.shuffle_rounded)),
                        ]))))),
        if (tracks.isNotEmpty) ...[
          const SizedBox(height: 12),
          LayoutBuilder(builder: (context, bounds) {
            final columns = bounds.maxWidth >= 900 ? 4 : 2;
            final width = (bounds.maxWidth - (columns - 1) * 10) / columns;
            return Wrap(spacing: 10, runSpacing: 10, children: [
              for (var i = 0; i < tracks.length.clamp(0, 4); i++)
                SizedBox(
                    width: width,
                    child: Material(
                        color: NeatieDesign.surface,
                        borderRadius: BorderRadius.circular(12),
                        child: InkWell(
                            borderRadius: BorderRadius.circular(12),
                            onTap: () => onPlay(i),
                            child: Padding(
                                padding: const EdgeInsets.all(10),
                                child: Row(children: [
                                  HomeArtwork(
                                      api: api,
                                      item: tracks[i],
                                      kind: 'track',
                                      size: 38),
                                  const SizedBox(width: 10),
                                  Expanded(
                                      child: Column(
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                        Text('${tracks[i]['title'] ?? ''}',
                                            maxLines: 1,
                                            overflow: TextOverflow.ellipsis,
                                            style: const TextStyle(
                                                fontSize: 12,
                                                fontWeight: FontWeight.w600)),
                                        const SizedBox(height: 4),
                                        Text(
                                            [
                                              '${tracks[i]['artist'] ?? tracks[i]['channel'] ?? ''}',
                                              catalogReleaseYear(tracks[i])
                                            ]
                                                .where((v) => v.isNotEmpty)
                                                .join(' · '),
                                            maxLines: 1,
                                            overflow: TextOverflow.ellipsis,
                                            style: const TextStyle(
                                                fontSize: 10,
                                                color: NeatieDesign.muted)),
                                      ])),
                                ]))))),
            ]);
          }),
        ],
      ]));
}

/// Presentation only: the backend owns recommendations, lanes and readiness.
class HomeArtwork extends StatelessWidget {
  const HomeArtwork(
      {super.key,
      required this.api,
      required this.item,
      required this.kind,
      required this.size,
      this.diagnosticKey,
      this.collage = true});
  final MusicApi api;
  final Map<String, dynamic> item;
  final String kind;
  final double size;
  final bool collage;
  final String? diagnosticKey;

  @override
  Widget build(BuildContext context) {
    final images = <String>{};
    final id = item['id'] ?? item['video_id'] ?? item['browse_id'];
    final cardKey = id == null ? null : '$kind:$id';
    if (!collage && kind == 'mix') {
      final tracks = MusicApi.tracks(item['tracks'] ?? item['items']);
      if (tracks.isNotEmpty)
        images.addAll(api.artworkUrls(tracks.first, 'track'));
    }
    if (collage && (kind == 'mix' || kind == 'radio')) {
      for (final value in item['collage_images'] is List
          ? item['collage_images'] as List
          : const []) {
        final uri = api.artworkUri(value);
        if (uri != null) images.add(uri.toString());
        if (images.length == 4) break;
      }
      // A mix may use its exact track covers. Artist radios retain portraits.
      if (kind == 'mix' && images.length < 2) {
        for (final track in MusicApi.tracks(item['tracks'] ?? item['items'])) {
          images.addAll(api.artworkUrls(track, 'track').take(1));
          if (images.length >= 4) break;
        }
      }
    }
    Widget cover;
    if (collage && images.length >= 2) {
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
                          cardKey: cardKey,
                          width: size / 2,
                          height: size / 2)
                      : const Center(child: Icon(Icons.music_note))),
          ])),
      ]);
    } else {
      cover = ArtworkImage(
          urls: [...images, ...api.artworkUrls(item, kind)],
          diagnosticKey: diagnosticKey,
          cardKey: cardKey,
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
            api: api, item: track, kind: 'track', size: compact ? 72 : 96);
        final copy =
            Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text("Today's Pick",
              style: NeatieDesign.eyebrow.copyWith(color: NeatieDesign.accent)),
          const SizedBox(height: 8),
          Text('${track['title'] ?? ''}',
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: NeatieDesign.display(compact ? 22 : 30)),
          const SizedBox(height: 6),
          Text(
              [
                '${track['artist'] ?? track['channel'] ?? ''}',
                catalogReleaseYear(track)
              ].where((v) => v.isNotEmpty).join(' · '),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: NeatieDesign.muted, fontSize: 14)),
        ]);
        final play = IconButton.filled(
            tooltip: "Play Today's Pick",
            onPressed: onPlay,
            icon: const Icon(Icons.play_arrow_rounded));
        return Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: EditorialSurface(
                radius: 20,
                child: DecoratedBox(
                    decoration: const BoxDecoration(
                        gradient: LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [Color(0xff202723), Color(0xff171c1b)])),
                    child: Padding(
                        padding: EdgeInsets.all(compact ? 18 : 24),
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
                                    const SizedBox(height: 14),
                                    play,
                                  ])
                            : Row(children: [
                                cover,
                                const SizedBox(width: 22),
                                Expanded(
                                    child: Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                      copy,
                                      const SizedBox(height: 12),
                                      play
                                    ]))
                              ])))));
      });
}

class HomeAlbumHero extends StatefulWidget {
  const HomeAlbumHero({
    super.key,
    required this.api,
    required this.albums,
    required this.active,
    required this.onOpen,
    required this.onPlay,
    this.onArtworkOpen,
  });
  final MusicApi api;
  final List<Map<String, dynamic>> albums;
  final bool active;
  final ValueChanged<Map<String, dynamic>> onOpen, onPlay;
  final void Function(Map<String, dynamic>, Rect)? onArtworkOpen;
  @override
  State<HomeAlbumHero> createState() => _HomeAlbumHeroState();
}

class _HomeAlbumHeroState extends State<HomeAlbumHero> {
  final controller = PageController(keepPage: false);
  Timer? timer;
  int index = 0;
  bool reducedMotion = false;
  bool interacting = false, hovering = false;
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
        hovering ||
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
      _settleLoop();
    } else {
      moving = true;
      unawaited(
        controller
            .animateToPage(
          target,
          duration: const Duration(milliseconds: 450),
          curve: Curves.easeInOutCubic,
        )
            .whenComplete(() {
          if (!mounted) return;
          moving = false;
          _settleLoop();
        }),
      );
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
    return LayoutBuilder(
      builder: (context, constraints) {
        final compact = constraints.maxWidth < 520;
        final size = compact ? 112.0 : 240.0;
        final extraScale = (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
            .clamp(0.0, double.infinity);
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              height:
                  (compact ? 264 : 320) + extraScale * (compact ? 260 : 150),
              child: MouseRegion(
                onEnter: (_) {
                  hovering = true;
                  timer?.cancel();
                },
                onExit: (_) {
                  hovering = false;
                  _schedule();
                },
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
                        WidgetsBinding.instance.addPostFrameCallback(
                          (_) => _settleLoop(),
                        );
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
                        final coverKey = GlobalKey();
                        void openAlbum() {
                          final box = coverKey.currentContext
                              ?.findRenderObject() as RenderBox?;
                          if (box != null &&
                              box.hasSize &&
                              widget.onArtworkOpen != null) {
                            widget.onArtworkOpen!(
                              album,
                              box.localToGlobal(Offset.zero) & box.size,
                            );
                          } else {
                            widget.onOpen(album);
                          }
                        }

                        return ClipRRect(
                          borderRadius: BorderRadius.circular(26),
                          child: Stack(
                            fit: StackFit.expand,
                            children: [
                              const ColoredBox(color: Color(0xff1d2924)),
                              IgnorePointer(
                                  child: RepaintBoundary(
                                      child: ImageFiltered(
                                imageFilter:
                                    ui.ImageFilter.blur(sigmaX: 8, sigmaY: 8),
                                child: Transform.flip(
                                    flipX: true,
                                    child: ArtworkImage(
                                        urls: widget.api
                                            .artworkUrls(album, 'album'),
                                        width: constraints.maxWidth,
                                        height: compact ? 264 : 320)),
                              ))),
                              const DecoratedBox(
                                decoration: BoxDecoration(
                                  gradient: LinearGradient(
                                    begin: Alignment.centerLeft,
                                    end: Alignment.centerRight,
                                    colors: [
                                      Color(0xe6111916),
                                      Color(0xb3111715),
                                      Color(0x85111916),
                                    ],
                                  ),
                                ),
                              ),
                              InkWell(
                                onTap: openAlbum,
                                child: Padding(
                                  padding: EdgeInsets.all(compact ? 20 : 32),
                                  child: Row(
                                    children: [
                                      HomeArtwork(
                                        key: coverKey,
                                        api: widget.api,
                                        item: album,
                                        kind: 'album',
                                        size: size,
                                      ),
                                      const SizedBox(width: 18),
                                      Expanded(
                                        child: Column(
                                          mainAxisAlignment:
                                              MainAxisAlignment.center,
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                            Text(
                                              title,
                                              maxLines: compact ? 2 : 3,
                                              overflow: TextOverflow.ellipsis,
                                              style: NeatieDesign.display(
                                                compact ? 24 : 42,
                                              ),
                                            ),
                                            const SizedBox(height: 6),
                                            Text(
                                              [
                                                '${album['artist'] ?? album['artist_name'] ?? album['channel'] ?? ''}',
                                                catalogReleaseYear(album)
                                              ]
                                                  .where((v) => v.isNotEmpty)
                                                  .join(' · '),
                                              maxLines: 2,
                                              overflow: TextOverflow.ellipsis,
                                            ),
                                            const SizedBox(height: 14),
                                            Row(
                                              crossAxisAlignment:
                                                  CrossAxisAlignment.center,
                                              children: [
                                                IconButton.filled(
                                                  tooltip:
                                                      'Play featured album',
                                                  onPressed: () =>
                                                      widget.onPlay(album),
                                                  icon: const Icon(
                                                    Icons.play_arrow,
                                                  ),
                                                ),
                                                const SizedBox(width: 8),
                                                Flexible(
                                                    child: TextButton(
                                                  onPressed: openAlbum,
                                                  child: const Text(
                                                    'Open album',
                                                  ),
                                                )),
                                              ],
                                            ),
                                          ],
                                        ),
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                            ],
                          ),
                        );
                      },
                    ),
                  ),
                ),
              ),
            ),
            if (widget.albums.length > 1)
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  for (var dot = 0; dot < widget.albums.length; dot++)
                    Semantics(
                        button: true,
                        selected: dot == index,
                        label:
                            'Featured album ${dot + 1} of ${widget.albums.length}',
                        child: InkResponse(
                            onTap: () => _go(dot),
                            radius: 18,
                            child: SizedBox(
                                width: 28,
                                height: 36,
                                child: Center(
                                    child: AnimatedContainer(
                                        duration: reducedMotion
                                            ? Duration.zero
                                            : const Duration(milliseconds: 220),
                                        width: dot == index ? 18 : 5,
                                        height: 5,
                                        decoration: BoxDecoration(
                                            color: dot == index
                                                ? NeatieDesign.accent
                                                : NeatieDesign.muted
                                                    .withValues(alpha: .35),
                                            borderRadius:
                                                BorderRadius.circular(4))))))),
                ],
              ),
          ],
        );
      },
    );
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
  const HomeCollectionCard({
    super.key,
    required this.api,
    required this.item,
    required this.kind,
    required this.title,
    required this.subtitle,
    required this.onOpen,
    required this.onPlay,
    this.width,
    this.onArtworkOpen,
  });
  final MusicApi api;
  final Map<String, dynamic> item;
  final String kind, title, subtitle;
  final VoidCallback? onOpen, onPlay;
  final double? width;
  final ValueChanged<Rect>? onArtworkOpen;
  @override
  Widget build(BuildContext context) => SizedBox(
        width: width ?? (kind == 'radio' ? 202 : 186),
        child: Padding(
          padding: const EdgeInsets.only(right: 14, bottom: 4),
          child: EditorialSurface(
            outlined: false,
            color: Colors.transparent,
            child: InkWell(
              onTap: onOpen,
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Center(
                      child: ArtworkOpen(
                        onOpen: (rect) {
                          if (onArtworkOpen != null)
                            onArtworkOpen!(rect);
                          else
                            onOpen?.call();
                        },
                        child: HomeArtwork(
                          api: api,
                          item: item,
                          kind: kind,
                          collage: kind != 'mix' || width == null,
                          size: width == null
                              ? (kind == 'radio' ? 160 : 144)
                              : width! - 38,
                        ),
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            subtitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(context).textTheme.bodySmall,
                          ),
                        ),
                        IconButton(
                          tooltip: 'Play $title',
                          onPressed: onPlay,
                          icon: Icon(
                            kind == 'radio'
                                ? Icons.play_circle_outline
                                : Icons.play_circle_fill,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
}

class HomeTrackStrip extends StatelessWidget {
  const HomeTrackStrip({
    super.key,
    required this.api,
    required this.tracks,
    required this.onPlay,
    required this.onMenu,
    this.sliver = false,
  });
  final MusicApi api;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<int> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenu;
  final bool sliver;
  @override
  Widget build(BuildContext context) {
    Widget buildTrack(BuildContext context, int index) {
      final track = tracks[index];
      final title = '${track['title'] ?? ''}';
      final seconds = trackDurationSeconds(track);
      return ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
        leading: HomeArtwork(api: api, item: track, kind: 'track', size: 44),
        title: Text(
          title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: Theme.of(context).textTheme.titleSmall,
        ),
        subtitle: Text(
          [
            '${track['artist'] ?? track['channel'] ?? ''}',
            catalogReleaseYear(track),
          ].where((v) => v.isNotEmpty).join(' · '),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        onTap: track['playable'] == false ? null : () => onPlay(index),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (seconds > 0)
              Text(
                '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            IconButton(
              tooltip: 'Options for $title',
              icon: const Icon(Icons.more_horiz, size: 19),
              onPressed: () => onMenu(track),
            ),
          ],
        ),
      );
    }
    if (sliver) {
      return SliverList.builder(itemCount: tracks.length, itemBuilder: buildTrack);
    }
    return Column(children: [
      for (var index = 0; index < tracks.length; index++)
        buildTrack(context, index),
    ]);
  }
}

/// Captures the real cover bounds; keyboard activation uses the same path.
class ArtworkOpen extends StatelessWidget {
  const ArtworkOpen({super.key, required this.child, required this.onOpen});
  final Widget child;
  final ValueChanged<Rect> onOpen;
  @override
  Widget build(BuildContext context) => Builder(
        builder: (coverContext) => InkWell(
          onTap: () {
            final box = coverContext.findRenderObject() as RenderBox?;
            if (box != null && box.hasSize)
              onOpen(box.localToGlobal(Offset.zero) & box.size);
          },
          borderRadius: BorderRadius.circular(14),
          child: child,
        ),
      );
}

/// A short, repaint-isolated cover flight, independent of metadata loading.
class ArtworkFlight extends StatefulWidget {
  const ArtworkFlight({
    super.key,
    required this.child,
    required this.urls,
    this.origin,
  });
  final Widget child;
  final List<String> urls;
  final Rect? origin;
  @override
  State<ArtworkFlight> createState() => _ArtworkFlightState();
}

class _ArtworkFlightState extends State<ArtworkFlight> {
  OverlayEntry? entry;
  bool started = false, flying = false;
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (started) return;
    started = true;
    if (widget.origin == null || MediaQuery.disableAnimationsOf(context))
      return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !TickerMode.of(context)) return;
      final box = context.findRenderObject() as RenderBox?;
      final overlay = Overlay.maybeOf(context, rootOverlay: true);
      final overlayBox = overlay?.context.findRenderObject() as RenderBox?;
      if (box == null || !box.hasSize || overlay == null || overlayBox == null)
        return;
      final shift = overlayBox.localToGlobal(Offset.zero);
      final target = (box.localToGlobal(Offset.zero) - shift) & box.size;
      final origin = widget.origin!.shift(-shift);
      setState(() => flying = true);
      entry = OverlayEntry(
        builder: (_) => IgnorePointer(
          child: TweenAnimationBuilder<Rect?>(
            tween: RectTween(begin: origin, end: target),
            duration: const Duration(milliseconds: 420),
            curve: Curves.easeInOutCubic,
            onEnd: _finish,
            builder: (_, rect, __) => Stack(
              children: [
                Positioned.fromRect(
                  rect: rect!,
                  child: RepaintBoundary(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(14),
                      child: ArtworkImage(
                        urls: widget.urls,
                        width: rect.width,
                        height: rect.height,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
      overlay.insert(entry!);
    });
  }

  void _finish() {
    entry?.remove();
    entry = null;
    if (mounted) setState(() => flying = false);
  }

  @override
  void dispose() {
    entry?.remove();
    entry = null;
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      Opacity(opacity: flying ? 0 : 1, child: widget.child);
}
