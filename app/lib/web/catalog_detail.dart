import 'dart:async';

import 'package:flutter/material.dart';

import 'catalog_api.dart';
import 'artwork_image.dart';
import 'music_api.dart';
import 'web_player.dart';
import 'design_system.dart';
import 'editorial_placeholder.dart';
import 'home_widgets.dart' show ArtworkFlight;
import 'browse_widgets.dart';
import '../logic/track_metadata.dart';

class CatalogDetail extends StatefulWidget {
  const CatalogDetail({
    super.key,
    required this.api,
    required this.id,
    required this.kind,
    required this.title,
    required this.player,
    required this.onLike,
    required this.onOpen,
    required this.onBack,
    this.isLiked,
    this.onTrackOptions,
    this.initialItem,
    this.artworkOrigin,
    this.following = false,
    this.onFollow,
    this.active = true,
  });

  final CatalogApi api;
  final bool active;
  final Map<String, dynamic>? initialItem;
  final Rect? artworkOrigin;
  final String id;
  final String kind;
  final String title;
  final WebPlayer player;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final void Function(String kind, String id, String title) onOpen;
  final VoidCallback onBack;
  final bool Function(Map<String, dynamic>)? isLiked;
  final ValueChanged<Map<String, dynamic>>? onTrackOptions;
  final bool following;
  final ValueChanged<Map<String, dynamic>>? onFollow;

  @override
  State<CatalogDetail> createState() => _CatalogDetailState();
}

class _CatalogDetailState extends State<CatalogDetail> {
  Map<String, dynamic>? _detail;
  List<Map<String, dynamic>> _tracks = const [];
  List<Map<String, dynamic>> _albums = const [];
  final _songReleases = <String, Map<String, dynamic>>{};
  final _songCounts = <String, dynamic>{};
  String _releaseFilter = 'popular';
  Object? _error;
  var _loading = true;
  var _requestVersion = 0;
  var _checkingArtistTracks = false;

  bool get _isArtist => widget.kind.toLowerCase() == 'artist';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _requestVersion++;
    super.dispose();
  }

  Future<void> _load() async {
    final requestVersion = ++_requestVersion;
    setState(() {
      _loading = true;
      _error = null;
      _detail = null;
      _tracks = const [];
      _albums = const [];
      _songReleases.clear();
      _songCounts.clear();
    });
    try {
      final detail = _isArtist
          ? await widget.api.artist(widget.id)
          : await widget.api.album(widget.id);
      if (!mounted || requestVersion != _requestVersion) return;
      setState(() {
        _detail = detail;
        _tracks =
            MusicApi.tracks(_isArtist ? detail['top_songs'] : detail['tracks']);
        final albums = detail['albums'];
        _albums = albums is List
            ? albums.whereType<Map>().map(Map<String, dynamic>.from).toList()
            : const [];
        _loading = false;
        _checkingArtistTracks = _isArtist &&
            _tracks.isEmpty &&
            _text(detail['name']).isNotEmpty &&
            detail['name'] != 'Unknown Artist';
      });
      if (_checkingArtistTracks) {
        try {
          final works = await widget.api.artistWorks(widget.id, 'songs');
          if (mounted && requestVersion == _requestVersion) {
            setState(() => _tracks = MusicApi.tracks(works).take(12).toList());
          }
        } catch (_) {
          // The overview stays available and Show all can retry.
        } finally {
          if (mounted && requestVersion == _requestVersion) {
            setState(() => _checkingArtistTracks = false);
          }
        }
      }
      if (_isArtist && mounted && requestVersion == _requestVersion) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && requestVersion == _requestVersion && widget.active) {
            unawaited(_loadSongReleases(requestVersion));
          }
        });
      }
    } catch (error) {
      if (!mounted || requestVersion != _requestVersion) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    }
  }

  String _text(dynamic value) => value?.toString().trim() ?? '';

  Future<void> _loadSongReleases(int version) async {
    final scope = widget.api.api.userScopeId;
    final ids = _tracks
        .map((track) => _text(track['album_id']))
        .where((id) => id.isNotEmpty)
        .toSet()
        .take(5)
        .toList();
    bool current() => mounted && widget.active && version == _requestVersion &&
        scope == widget.api.api.userScopeId;
    Future<void> fetch(String id) async {
      try {
        final release = await widget.api.album(id);
        if (current()) setState(() => _songReleases[id] = release);
      } catch (_) {
        // An unavailable release does not block the overview or its songs.
      }
    }

    for (var index = 0; index < ids.length && current(); index += 2) {
      await Future.wait(ids.skip(index).take(2).map(fetch));
    }
    if (!current()) return;
    final missing = _tracks.take(12)
        .where((track) => _songCount(track).isEmpty)
        .map(extractPlaybackSourceId).whereType<String>().toSet().toList();
    for (var index = 0; index < missing.length && current(); index += 5) {
      final batch = missing.skip(index).take(5).toSet();
      try {
        final counts = await widget.api.artistWorks(widget.id, 'song_counts',
            collectionId: batch.join(','));
        if (current()) setState(() {
          for (final count in counts) {
            final id = _text(count['id']);
            if (batch.contains(id)) _songCounts[id] = count['views'];
          }
        });
      } catch (_) {
        // Provider counts are supplemental.
      }
    }
  }

  String _songCount(Map<String, dynamic> track) {
    final release = _songReleases[_text(track['album_id'])];
    final source = extractPlaybackSourceId(track);
    final match = source == null || release == null
        ? null
        : (release['tracks'] as List? ?? const []).whereType<Map>()
            .where((item) => extractPlaybackSourceId(item) == source)
            .firstOrNull;
    final count = [match?['views'], track['views'], _songCounts[source]]
        .map(_text)
        .where((value) => RegExp(r'^[1-9]').hasMatch(value)).firstOrNull ?? '';
    if (count.isEmpty || RegExp(r'plays|views', caseSensitive: false).hasMatch(count))
      return count;
    final number = int.tryParse(count);
    return '${number == null ? count : MaterialLocalizations.of(context).formatDecimal(number)} views';
  }

  String _songMetadata(Map<String, dynamic> track) {
    final trackDate = catalogReleaseDate(track);
    final releaseDate =
        catalogReleaseDate(_songReleases[_text(track['album_id'])] ?? {});
    final date = trackDate.isEmpty ||
            (trackDate.length == 4 && releaseDate.endsWith(trackDate))
        ? releaseDate
        : trackDate;
    return [date, _songCount(track)].where((value) => value.isNotEmpty).join(' · ');
  }

  List<Map<String, dynamic>> _items(String key) =>
      (_detail?[key] as List? ?? const [])
          .whereType<Map<String, dynamic>>()
          .toList();

  List<String> get _artwork => [
        ...widget.api.api.artworkUrls(_detail ?? {}, widget.kind),
        ...widget.api.api.artworkUrls(widget.initialItem ?? {}, widget.kind),
      ].toSet().toList();

  String get _title {
    final detail = _detail;
    final value = _isArtist
        ? (detail?['name'] ?? detail?['title'])
        : (detail?['title'] ?? detail?['name']);
    final resolved = _text(value);
    return resolved.isEmpty || resolved == 'Unknown Artist'
        ? widget.title
        : resolved;
  }

  String get _artist {
    final detail = _detail;
    final value = _isArtist
        ? (detail?['artist'] ?? detail?['author'])
        : (detail?['artist'] ?? detail?['author'] ?? detail?['channel']);
    return _text(value);
  }

  String get _biography {
    final full = _text(_detail?['full_description']);
    return full.isNotEmpty ? full : _text(_detail?['description']);
  }

  Future<void> _playAlbum(int selected) async {
    final tracks = _tracks;
    if (tracks.isEmpty) return;
    await widget.player.startQueue(tracks, selected, _title);
  }

  Future<void> _playArtistTrack(Map<String, dynamic> track) =>
      widget.player.startArtistQueue(track, _tracks, _title,
          loadSongs: () => widget.api.artistWorks(widget.id, 'songs'));

  Future<void> _showArtistWorks(String title, List<String> sections,
      {String collectionId = ''}) {
    final scope = widget.api.api.userScopeId;
    final future = Future.wait(sections.map((section) => widget.api
        .artistWorks(widget.id, section, collectionId: collectionId)));
    final tracks =
        const {'songs', 'radio', 'featuring'}.contains(sections.first);
    return showDialog<void>(
      context: context,
      builder: (context) => Dialog(
        child: SizedBox(
          width: 760,
          height: MediaQuery.sizeOf(context).height * .8,
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(children: [
              Row(children: [
                Expanded(child: Text(title, style: NeatieDesign.display(28))),
                IconButton(
                    tooltip: 'Close',
                    onPressed: () => Navigator.pop(context),
                    icon: const Icon(Icons.close)),
              ]),
              Expanded(
                  child: FutureBuilder<List<List<Map<String, dynamic>>>>(
                future: future,
                builder: (_, snapshot) {
                  if (snapshot.hasError) {
                    return const Center(
                        child: Text('Could not load these works. Try again.'));
                  }
                  if (!snapshot.hasData)
                    return const Center(child: NeatieLoading());
                  final items = {
                    for (final item in snapshot.data!.expand((group) => group))
                      _text(item['id'] ?? item['videoId']): item,
                  }.values.toList();
                  if (items.isEmpty)
                    return const Center(
                        child: Text('No additional works available.'));
                  return ListView.builder(
                    itemCount: items.length,
                    itemBuilder: (_, index) {
                      final item = items[index];
                      return ListTile(
                        leading: _Cover(
                            urls: widget.api.api
                                .artworkUrls(item, tracks ? 'track' : 'album'),
                            size: 42),
                        title: Text(_text(item['title'] ?? item['name'])),
                        subtitle: Text([
                          _text(item['artist'] ?? item['channel']),
                          tracks ? _songMetadata(item) : catalogReleaseDate(item),
                        ].where((text) => text.isNotEmpty).join(' · ')),
                        trailing: Icon(
                            tracks ? Icons.play_arrow : Icons.chevron_right),
                        onTap: () {
                          if (!mounted || scope != widget.api.api.userScopeId)
                            return;
                          Navigator.pop(context);
                          if (tracks) {
                            if (sections.first == 'songs') {
                              unawaited(widget.player
                                  .startArtistQueue(item, items, _title));
                            } else {
                              unawaited(widget.player
                                  .startQueue(items, index, title));
                            }
                          } else {
                            widget.onOpen('album', _text(item['id']),
                                _text(item['title']));
                          }
                        },
                      );
                    },
                  );
                },
              )),
            ]),
          ),
        ),
      ),
    );
  }

  Widget _releaseStrip(List<Map<String, dynamic>> releases) => SizedBox(
        height: 258,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          itemCount: releases.length,
          separatorBuilder: (_, __) => const SizedBox(width: 20),
          itemBuilder: (_, index) => _albumCard(releases[index]),
        ),
      );

  String _duration(Map<String, dynamic> track) {
    final seconds = trackDurationSeconds(track);
    return seconds > 0
        ? '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}'
        : '';
  }

  String get _metadata {
    final released = _isArtist
        ? ''
        : catalogReleaseDate(_detail ?? widget.initialItem ?? {});
    final seconds =
        _tracks.fold<int>(0, (sum, track) => sum + trackDurationSeconds(track));
    return [
      if (released.isNotEmpty) 'Released $released',
      if (_tracks.isNotEmpty)
        '${_tracks.length} ${_isArtist ? 'top songs' : 'songs'}',
      if (_isArtist && _albums.isNotEmpty) '${_albums.length} albums',
      if (!_isArtist && seconds > 0) '${seconds ~/ 60} min'
    ].join('  ·  ');
  }

  @override
  Widget build(BuildContext context) {
    final body = _loading || (_error != null && widget.initialItem != null)
        ? ListView(
            padding: const EdgeInsets.all(20),
            children: [
              if (widget.initialItem != null)
                _Header(
                  isArtist: _isArtist,
                  title: widget.title,
                  artist:
                      '${widget.initialItem?['artist'] ?? widget.initialItem?['channel'] ?? ''}',
                  thumbnail: widget.api.api.artworkUrls(
                    widget.initialItem!,
                    _isArtist ? 'artist' : 'album',
                  ),
                  origin: widget.artworkOrigin,
                ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.all(28),
                  child: _Failure(onRetry: _load),
                )
              else
                const Padding(
                  padding: EdgeInsets.all(28),
                  child: Center(
                    child: NeatieLoading(
                      label: 'Opening your music',
                      showLabel: true,
                    ),
                  ),
                ),
            ],
          )
        : _error != null
            ? _Failure(onRetry: _load)
            : _detail == null
                ? const _EmptyDetail(message: 'No detail data is available.')
                : _content();
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Back',
          icon: const Icon(Icons.arrow_back),
          onPressed: widget.onBack,
        ),
        title: Text(_isArtist ? 'Artist' : 'Album',
            style: const TextStyle(fontSize: 12, color: NeatieDesign.muted)),
      ),
      body: body,
    );
  }

  Widget _content() {
    final tracks = _tracks;
    final singles = _items('singles');
    final featuring = _items('featuring');
    final appearances = _items('appears_on');
    final sections = _detail?['work_sections'] as Map? ?? const {};
    final releases = switch (_releaseFilter) {
      'albums' => _albums,
      'singles' => singles,
      _ => [..._albums, ...singles].take(12).toList(),
    };
    final releaseSections = [
      if (_releaseFilter != 'singles' &&
          (_albums.isNotEmpty || sections.containsKey('albums')))
        'albums',
      if (_releaseFilter != 'albums' &&
          (singles.isNotEmpty || sections.containsKey('singles')))
        'singles',
    ];
    return ArtworkWarmup(
        enabled: widget.active && !widget.player.busy,
        items: [
          for (final track in tracks.take(24))
            (urls: widget.api.api.artworkUrls(track, 'track'), size: 48.0)
        ],
        child: CustomScrollView(
          cacheExtent: 700,
          slivers: [
            SliverPadding(
                padding: EdgeInsets.fromLTRB(
                    MediaQuery.sizeOf(context).width < 700 ? 20 : 36,
                    8,
                    MediaQuery.sizeOf(context).width < 700 ? 20 : 36,
                    48),
                sliver: SliverMainAxisGroup(slivers: [
                  SliverToBoxAdapter(
                      child: _Header(
                    origin: widget.artworkOrigin,
                    isArtist: _isArtist,
                    title: _title,
                    artist: _artist,
                    thumbnail: _artwork,
                    metadata: _metadata,
                    actions: [
                      FilledButton.icon(
                          onPressed: tracks.isNotEmpty
                              ? () => unawaited(_isArtist
                                  ? _playArtistTrack(tracks.first)
                                  : _playAlbum(0))
                              : null,
                          icon: const Icon(Icons.play_arrow_rounded, size: 23),
                          label: Text(
                              _isArtist ? 'Start listening' : 'Play album')),
                      if (_isArtist && widget.onFollow != null)
                        OutlinedButton(
                          onPressed: () => widget.onFollow!({
                            'id': widget.id,
                            'name': _title,
                            'thumbnail': _artwork.firstOrNull,
                          }),
                          child:
                              Text(widget.following ? 'Following' : 'Follow'),
                        ),
                    ],
                  )),
                  SliverLayoutBuilder(builder: (context, bounds) {
                    final sequence = SliverMainAxisGroup(slivers: [
                      SliverToBoxAdapter(
                          child: BrowseSectionHeading(
                              title: _isArtist ? 'Top songs' : 'Tracks',
                              caption: _isArtist
                                  ? 'A place to begin.'
                                  : 'The album, in sequence.',
                              action: _isArtist &&
                                      (sections.containsKey('songs') ||
                                          tracks.isEmpty)
                                  ? TextButton(
                                      onPressed: () => _showArtistWorks(
                                          'Songs by $_title', ['songs']),
                                      child: const Text('Show all'))
                                  : null)),
                      const SliverToBoxAdapter(child: Divider(height: 1)),
                      if (tracks.isEmpty)
                        SliverToBoxAdapter(
                            child: _EmptyDetail(
                                message: _isArtist
                                    ? (_checkingArtistTracks
                                        ? 'Checking for credited songs…'
                                        : 'No playable top songs are available for this artist.')
                                    : 'No playable tracks are available for this album.'))
                      else
                        SliverList.builder(
                            itemCount: tracks.length,
                            itemBuilder: (_, index) =>
                                _trackTile(tracks[index], index)),
                    ]);
                    final notes = SliverToBoxAdapter(child: _linerNotes());
                    return bounds.crossAxisExtent < 1000
                        ? SliverMainAxisGroup(slivers: [sequence, notes])
                        : SliverCrossAxisGroup(slivers: [
                            SliverCrossAxisExpanded(flex: 7, sliver: sequence),
                            const SliverConstrainedCrossAxis(
                                maxExtent: 44,
                                sliver: SliverToBoxAdapter(
                                    child: SizedBox(width: 44))),
                            SliverCrossAxisExpanded(flex: 3, sliver: notes),
                          ]);
                  }),
                  if (_isArtist &&
                      (_albums.isNotEmpty ||
                          singles.isNotEmpty ||
                          sections.containsKey('albums') ||
                          sections.containsKey('singles'))) ...[
                    SliverToBoxAdapter(
                        child: BrowseSectionHeading(
                            title: 'Discography',
                            action: releaseSections.isEmpty
                                ? null
                                : TextButton(
                                    onPressed: () => _showArtistWorks(
                                        'Discography', releaseSections),
                                    child: const Text('Show all'),
                                  ))),
                    SliverToBoxAdapter(
                      child: Wrap(spacing: 8, children: [
                        for (final option in const {
                          'popular': 'Popular releases',
                          'albums': 'Albums',
                          'singles': 'Singles and EPs',
                        }.entries)
                          ChoiceChip(
                              label: Text(option.value),
                              selected: _releaseFilter == option.key,
                              onSelected: (_) =>
                                  setState(() => _releaseFilter = option.key)),
                      ]),
                    ),
                    if (releases.isNotEmpty)
                      SliverToBoxAdapter(child: _releaseStrip(releases)),
                  ],
                  if (_isArtist &&
                      (featuring.isNotEmpty ||
                          _detail?['radio_id'] != null)) ...[
                    SliverToBoxAdapter(
                        child:
                            BrowseSectionHeading(title: 'Featuring $_title')),
                    SliverToBoxAdapter(
                        child: Wrap(spacing: 12, runSpacing: 8, children: [
                      if (_detail?['radio_id'] != null)
                        ActionChip(
                            avatar: const Icon(Icons.radio_outlined, size: 18),
                            label: Text('$_title Radio'),
                            onPressed: () =>
                                _showArtistWorks('$_title Radio', ['radio'])),
                      for (final item in featuring)
                        ActionChip(
                            avatar: const Icon(Icons.playlist_play, size: 18),
                            label: Text(_text(item['title'])),
                            onPressed: () => _showArtistWorks(
                                _text(item['title']), ['featuring'],
                                collectionId: _text(item['id']))),
                    ])),
                  ],
                  if (_isArtist && appearances.isNotEmpty) ...[
                    const SliverToBoxAdapter(
                        child: BrowseSectionHeading(title: 'Appears on')),
                    SliverToBoxAdapter(child: _releaseStrip(appearances)),
                  ],
                ]))
          ],
        ));
  }

  Widget _linerNotes() {
    final related =
        (_detail?['related_artists'] as List? ?? const []).whereType<Map>();
    return Padding(
        padding: const EdgeInsets.only(top: 36),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (!_isArtist) ...[
            Text('Liner notes', style: NeatieDesign.display(28)),
            const SizedBox(height: 18),
            const Divider(height: 1),
            if (catalogReleaseDate(_detail ?? {}).isNotEmpty)
              BrowseFact(
                  label: 'Release date', value: catalogReleaseDate(_detail!)),
            if (_artist.isNotEmpty) BrowseFact(label: 'Artist', value: _artist),
            if (_tracks.isNotEmpty)
              BrowseFact(
                  label: 'Running time',
                  value: formatCollectionDuration(_tracks)),
            if (_text(_detail?['label']).isNotEmpty)
              BrowseFact(label: 'Label', value: _text(_detail?['label'])),
          ],
          if (_isArtist && _biography.isNotEmpty)
            ExpansionTile(
                shape: const Border(),
                collapsedShape: const Border(),
                tilePadding: EdgeInsets.zero,
                title: Text('About', style: NeatieDesign.display(28)),
                subtitle: const Text('The story behind the sound.',
                    style: TextStyle(fontSize: 12, color: NeatieDesign.muted)),
                childrenPadding: const EdgeInsets.only(top: 16, bottom: 24),
                expandedAlignment: Alignment.centerLeft,
                children: [
                  SelectableText(_biography,
                      style: const TextStyle(
                          fontSize: 13, height: 1.9, color: NeatieDesign.muted))
                ]),
          if (_isArtist && related.isNotEmpty) ...[
            const BrowseSectionHeading(title: 'In good company'),
            for (final raw in related.take(6))
              Builder(builder: (context) {
                final item = Map<String, dynamic>.from(raw),
                    name = '${raw['name'] ?? raw['title'] ?? ''}';
                final id = '${raw['id'] ?? raw['browseId'] ?? ''}';
                return ListTile(
                    contentPadding: EdgeInsets.zero,
                    leading: ClipOval(
                        child: _Cover(
                            urls: widget.api.api.artworkUrls(item, 'artist'),
                            size: 42)),
                    title: Text(name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 12)),
                    trailing: const Icon(Icons.arrow_outward_rounded, size: 17),
                    onTap: id.isEmpty
                        ? null
                        : () => widget.onOpen('artist', id, name));
              }),
          ],
        ]));
  }

  Widget _albumCard(Map<String, dynamic> album) {
    final id = _text(album['id'] ?? album['browseId'] ?? album['album_id']);
    final title = _text(album['title'] ?? album['name']);
    final artist = _text(
      album['artist'] ?? album['author'] ?? album['channel'],
    );
    return SizedBox(
        width: 184,
        child: Material(
            color: Colors.transparent,
            child: InkWell(
                borderRadius: BorderRadius.circular(14),
                onTap:
                    id.isEmpty ? null : () => widget.onOpen('album', id, title),
                child: Padding(
                    padding: const EdgeInsets.all(6),
                    child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          _Cover(
                              urls: widget.api.api.artworkUrls(album, 'album'),
                              size: 172),
                          const SizedBox(height: 14),
                          Text(title.isEmpty ? 'Untitled album' : title,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                  fontSize: 13,
                                  fontWeight: FontWeight.w600,
                                  color: NeatieDesign.ink)),
                          const SizedBox(height: 4),
                          Text(
                              catalogReleaseDate(album).isNotEmpty
                                  ? catalogReleaseDate(album)
                                  : artist,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                  fontSize: 11, color: NeatieDesign.muted)),
                        ])))));
  }

  Widget _trackTile(Map<String, dynamic> track, int index) {
    final title = _text(track['title'] ?? track['name']);
    final artist = _text(
      track['channel'] ?? track['artist'] ?? track['author'],
    );
    final metadata = _isArtist ? _songMetadata(track) : '';
    return BrowseTrackRow(
      number: index + 1,
      title: title.isEmpty ? 'Untitled track' : title,
      artist: artist,
      albumTitle: metadata,
      album: metadata.isEmpty ? null : Text(metadata, maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 11, color: NeatieDesign.muted)),
      duration: _duration(track),
      artwork: _Cover(
        urls: widget.api.api.artworkUrls(track, 'track'),
        size: 48,
      ),
      actions: [
        IconButton(
          tooltip:
              '${widget.isLiked?.call(track) == true ? 'Unlike' : 'Like'} $title',
          icon: Icon(
            widget.isLiked?.call(track) == true
                ? Icons.favorite
                : Icons.favorite_border,
          ),
          onPressed: () => unawaited(widget.onLike(track)),
        ),
        if (widget.onTrackOptions != null)
          IconButton(
            tooltip: 'Options for $title',
            icon: const Icon(Icons.more_horiz),
            onPressed: () => widget.onTrackOptions!(track),
          ),
      ],
      onTap: () =>
          unawaited(_isArtist ? _playArtistTrack(track) : _playAlbum(index)),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({
    required this.title,
    required this.artist,
    required this.thumbnail,
    required this.isArtist,
    this.origin,
    this.metadata = '',
    this.actions = const [],
  });

  final String title;
  final String artist;
  final List<String> thumbnail;
  final bool isArtist;
  final Rect? origin;
  final String metadata;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => BrowseHero(
        title: title,
        eyebrow: isArtist ? 'ARTIST' : 'ALBUM',
        artist: isArtist,
        metadata: metadata,
        actions: actions,
        subtitle: artist.isEmpty || artist == title
            ? null
            : Text(artist,
                style: const TextStyle(fontSize: 16, color: BrowsePalette.ink)),
        artwork: (size) => ArtworkFlight(
          origin: origin,
          urls: thumbnail,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: ArtworkImage(
              urls: thumbnail,
              width: size,
              height: size,
              placeholder: EditorialPlaceholder(label: title, artist: isArtist),
            ),
          ),
        ),
      );
}

class _Cover extends StatelessWidget {
  const _Cover({required this.urls, required this.size});

  final List<String> urls;
  final double size;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: ArtworkImage(
          urls: urls, width: size, height: size, icon: Icons.album_outlined),
    );
  }
}

class _Failure extends StatelessWidget {
  const _Failure({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('This detail could not be loaded.'),
            const SizedBox(height: 12),
            FilledButton(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      );
}

class _EmptyDetail extends StatelessWidget {
  const _EmptyDetail({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 24),
        child: Text(message),
      );
}
