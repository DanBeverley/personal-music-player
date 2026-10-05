import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import '../logic/track_metadata.dart';
import 'catalog_api.dart';
import 'catalog_detail.dart';
import 'artwork_image.dart';
import 'home_widgets.dart';
import 'music_api.dart';
import 'web_player.dart';

class CatalogScreen extends StatefulWidget {
  final MusicApi api;
  final WebPlayer player;
  final bool home, active;
  final bool homeSignalsReady;
  final int homeSignalRevision;
  final int foregroundRevision;
  final bool foreground;
  final List<Map<String, dynamic>> likedTracks;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Map<String, dynamic> Function()? homeSignals;
  const CatalogScreen(
      {super.key,
      required this.api,
      required this.player,
      required this.home,
      required this.active,
      required this.onLike,
      this.homeSignals,
      this.homeSignalsReady = true,
      this.homeSignalRevision = 0,
      this.foregroundRevision = 0,
      this.foreground = true,
      this.likedTracks = const []});
  @override
  State<CatalogScreen> createState() => _CatalogScreenState();
}

class _CatalogScreenState extends State<CatalogScreen> {
  late final catalog = CatalogApi(widget.api);
  final query = TextEditingController();
  Map<String, dynamic> data = {}, pagination = {};
  final List<({String type, Map<String, dynamic> item})> extras = [];
  final Set<String> seen = {};
  final Map<String, List<Map<String, dynamic>>> pagedItems = {};
  final Map<String, Map<String, dynamic>> pagedCursors = {};
  int pollAttempts = 0;
  bool busy = false, submitted = false, automatic = true;
  String? error;
  String submittedQuery = '', filter = 'all';
  int generation = 0;
  int revision = 0;
  Timer? poll, homePoll;
  Timer? homePrepare;
  final String launchToken = 'web:${DateTime.now().microsecondsSinceEpoch}';
  String lastPreparedSignals = '';
  DateTime? lastPreparedAt;
  bool prepareInFlight = false, prepareAgain = false, launchedHome = false;
  bool homeStatusInFlight = false, homeAwaitingPreparation = false;
  DateTime? lastHomeRevalidatedAt;
  String homeLane = 'all';
  bool get homeActive => widget.active && widget.foreground;
  ({String kind, String id, String title})? detail;
  static const surfaces = {
    'tracks': 'tracks',
    'results': 'tracks',
    'artists': 'artists',
    'albums': 'albums',
    'artist_tracks': 'tracks',
    'similar_tracks': 'tracks',
    'artist_albums': 'albums',
    'related_albums': 'albums',
    'similar_artists': 'artists'
  };
  static const labels = {
    'tracks': 'Songs',
    'artists': 'Artists',
    'albums': 'Albums'
  };

  @override
  void initState() {
    super.initState();
    if (widget.home && homeActive && widget.homeSignalsReady) unawaited(load());
  }

  @override
  void didUpdateWidget(covariant CatalogScreen old) {
    super.didUpdateWidget(old);
    if (widget.home &&
        homeActive &&
        (!old.active ||
            !old.foreground ||
            widget.foregroundRevision != old.foregroundRevision)) {
      if (maps(data['rows']).isEmpty && widget.homeSignalsReady && !busy)
        unawaited(load());
      else
        _revalidateHome();
    }
    if (widget.home &&
        homeActive &&
        !old.homeSignalsReady &&
        widget.homeSignalsReady &&
        maps(data['rows']).isEmpty &&
        !busy) {
      unawaited(load());
    }
    if (!homeActive) homePoll?.cancel();
    if (widget.home &&
        widget.homeSignalsReady &&
        (widget.homeSignalRevision != old.homeSignalRevision ||
            !old.homeSignalsReady)) {
      _scheduleHomePrepare();
    }
    if (!widget.home && old.active && !widget.active) poll?.cancel();
    if (!widget.home &&
        widget.active &&
        !old.active &&
        submitted &&
        detail == null) {
      _scheduleEnrichment(generation, pollAttempts);
    }
  }

  void _revalidateHome() {
    if (!widget.home ||
        !homeActive ||
        busy ||
        homeStatusInFlight ||
        !widget.homeSignalsReady) return;
    if (lastHomeRevalidatedAt != null &&
        widget.api.clock().difference(lastHomeRevalidatedAt!) <
            const Duration(seconds: 30)) {
      if (homeAwaitingPreparation) _scheduleHomeStatus(generation);
      return;
    }
    lastHomeRevalidatedAt = widget.api.clock();
    // Reconcile availability, never promote or replace a populated feed here.
    unawaited(_pollHomeStatus(generation, wait: false));
  }

  List<Map<String, dynamic>> maps(dynamic value) => value is List
      ? value.whereType<Map>().map((v) => Map<String, dynamic>.from(v)).toList()
      : [];
  String identity(String type, Map<String, dynamic> item) =>
      '$type:${item['id'] ?? item['browseId'] ?? item['videoId'] ?? '${item['title']}:${item['name']}'}';

  String _homeType(Map<String, dynamic> row) {
    final declared = '${row['item_type'] ?? ''}'.trim().toLowerCase();
    if (const {'track', 'album', 'artist', 'mix', 'radio'}.contains(declared))
      return declared;
    final kind = '${row['kind'] ?? row['id'] ?? ''}'.trim().toLowerCase();
    if (kind == 'featured_new_albums' || kind == 'recommended_albums')
      return 'album';
    if (kind == 'recommended_artists') return 'artist';
    if (kind == 'popular_radio') return 'radio';
    if (kind == 'made_for_you') return 'mix';
    return 'track';
  }

  List<Map<String, dynamic>> _collectionTracks(Map<String, dynamic> item) {
    final tracks = MusicApi.tracks(
        item['tracks'] ?? item['items'] ?? item['recommendations']);
    if (tracks.isEmpty && item['preview_track'] is Map)
      tracks.addAll(MusicApi.tracks([item['preview_track']]));
    return tracks.where((track) => track['playable'] != false).toList();
  }

  List<String> _artwork(Map<String, dynamic> item, String type) {
    final candidates = [
      item['thumbnail'],
      item['thumbnail_url'],
      item['artwork_url'],
      if (type == 'artists' || type == 'radio' || type == 'mix')
        item['artist_thumbnail'],
      if (type == 'radio' || type == 'mix') ...[
        if (item['collage_images'] is List) ...item['collage_images'] as List,
        ..._collectionTracks(item).map((track) => track['thumbnail']),
      ],
    ];
    return <String>{
      ...widget.api.artworkUrls(item, type),
      if (type == 'radio' || type == 'mix')
        for (final value in candidates)
          if (value is String && widget.api.artworkUri(value) != null)
            widget.api.artworkUri(value)!.toString(),
      if (type == 'radio' || type == 'mix')
        for (final track in _collectionTracks(item))
          ...widget.api.artworkUrls(track, 'track'),
    }.toList();
  }

  Widget _homeCollectionCard(String kind, Map<String, dynamic> item) {
    final title = (item['radio_title'] ??
            item['title'] ??
            item['name'] ??
            item['artist_name'] ??
            'Recommended mix')
        .toString();
    final artist =
        (item['artist_name'] ?? item['artist'] ?? item['channel'] ?? '')
            .toString();
    final collection = _collectionTracks(item);
    if (kind == 'artist')
      return HomeArtistCard(
          api: widget.api,
          item: item,
          onOpen: _homeDetailId(item, kind).isEmpty
              ? null
              : () => _openHomeItem(kind, item));
    return HomeCollectionCard(
        api: widget.api,
        item: item,
        kind: kind,
        title: title,
        subtitle: kind == 'radio'
            ? (artist.isEmpty ? 'Artist radio' : artist)
            : kind == 'album'
                ? (artist.isEmpty ? 'Album' : artist)
                : collection.isEmpty
                    ? 'Mix'
                    : '${collection.length} songs',
        onOpen: collection.isNotEmpty || _homeDetailId(item, kind).isNotEmpty
            ? () => _openHomeItem(kind, item)
            : null,
        onPlay: collection.isEmpty
            ? null
            : () => unawaited(widget.player.startQueue(collection, 0, title)));
  }

  void _openHomeItem(String kind, Map<String, dynamic> item) {
    final title =
        '${item['radio_title'] ?? item['title'] ?? item['name'] ?? item['artist_name'] ?? ''}';
    if (kind == 'mix' || kind == 'radio') {
      final tracks = _collectionTracks(item);
      if (tracks.isNotEmpty) _openHomeCollection(kind, item, tracks);
    } else {
      final id = _homeDetailId(item, kind);
      if (id.isNotEmpty) open(kind, id, title);
    }
  }

  void _playHomeAlbum(Map<String, dynamic> album) {
    final tracks = _collectionTracks(album);
    if (tracks.isEmpty) {
      _openHomeItem('album', album);
      return;
    }
    // Embedded previews start inside the gesture; otherwise open persisted detail.
    unawaited(widget.player.startQueue(
        tracks, 0, '${album['title'] ?? album['name'] ?? 'Album'}'));
  }

  String _homeDetailId(Map<String, dynamic> item, String kind) {
    var value = (kind == 'album'
                ? item['browseId'] ??
                    item['browse_id'] ??
                    item['album_id'] ??
                    item['id']
                : item['id'] ??
                    item['browseId'] ??
                    item['browse_id'] ??
                    item['artist_id'])
            ?.toString()
            .trim() ??
        '';
    if (kind == 'album' && value.startsWith('ytmusic:album:')) {
      value = value.substring('ytmusic:album:'.length);
    }
    return value;
  }

  void _openHomeCollection(String kind, Map<String, dynamic> item,
      List<Map<String, dynamic>> tracks) {
    final title = (item['radio_title'] ??
            item['title'] ??
            item['name'] ??
            item['artist_name'] ??
            'Recommended')
        .toString();
    if (kind == 'mix' || kind == 'radio') {
      _collectionDetailTracks = tracks;
      _collectionDetailItem = item;
      _collectionDetailKind = kind;
      setState(() => detail =
          (kind: 'collection', id: identity(kind, item), title: title));
    }
  }

  List<Map<String, dynamic>> _collectionDetailTracks = const [];
  Map<String, dynamic> _collectionDetailItem = const {};
  String _collectionDetailKind = 'mix';

  bool _isLiked(Map<String, dynamic> track) => widget.likedTracks
      .any((item) => extractTrackId(item) == extractTrackId(track));

  void _trackOptions(Map<String, dynamic> track) {
    final title = '${track['title'] ?? ''}',
        artist = '${track['artist'] ?? track['channel'] ?? ''}';
    final albumId = '${track['album_id'] ?? track['album_browse_id'] ?? ''}';
    final artistId = '${track['artist_id'] ?? track['artist_browse_id'] ?? ''}';
    unawaited(showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (sheet) => SafeArea(
            child: SingleChildScrollView(
                child: Padding(
                    padding: const EdgeInsets.all(20),
                    child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(children: [
                            HomeArtwork(
                                api: widget.api,
                                item: track,
                                kind: 'track',
                                size: 72),
                            const SizedBox(width: 16),
                            Expanded(
                                child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                  Text(title,
                                      style: Theme.of(context)
                                          .textTheme
                                          .titleLarge),
                                  Text(artist),
                                  if ('${track['album'] ?? ''}'.isNotEmpty)
                                    Text('${track['album']}'),
                                  if (trackDurationSeconds(track) > 0)
                                    Text(
                                        '${trackDurationSeconds(track) ~/ 60}:${(trackDurationSeconds(track) % 60).toString().padLeft(2, '0')}'),
                                ])),
                          ]),
                          ListTile(
                              leading: const Icon(Icons.play_arrow),
                              title: const Text('Play song and discover'),
                              enabled: track['playable'] != false,
                              onTap: () {
                                Navigator.pop(sheet);
                                unawaited(widget.player.start(track));
                              }),
                          ListTile(
                              leading: Icon(_isLiked(track)
                                  ? Icons.favorite
                                  : Icons.favorite_border),
                              title: Text(_isLiked(track)
                                  ? 'Remove from likes'
                                  : 'Add to likes'),
                              onTap: () {
                                Navigator.pop(sheet);
                                unawaited(widget.onLike(track));
                              }),
                          if (albumId.isNotEmpty)
                            ListTile(
                                leading: const Icon(Icons.album_outlined),
                                title: const Text('Open album'),
                                onTap: () {
                                  Navigator.pop(sheet);
                                  open('album', albumId,
                                      '${track['album'] ?? 'Album'}');
                                }),
                          if (artistId.isNotEmpty)
                            ListTile(
                                leading: const Icon(Icons.person_outline),
                                title: const Text('Open artist'),
                                onTap: () {
                                  Navigator.pop(sheet);
                                  open('artist', artistId, artist);
                                }),
                        ]))))));
  }

  Widget _homeTrackTile(Map<String, dynamic> track,
          List<Map<String, dynamic>> tracks, int index, String title) =>
      ListTile(
          leading: HomeArtwork(
              api: widget.api, item: track, kind: 'track', size: 44),
          title: Text('${track['title'] ?? ''}',
              maxLines: 1, overflow: TextOverflow.ellipsis),
          subtitle: Text('${track['artist'] ?? track['channel'] ?? ''}',
              maxLines: 1, overflow: TextOverflow.ellipsis),
          selected:
              extractTrackId(widget.player.current) == extractTrackId(track),
          onTap: track['playable'] == false
              ? null
              : () => unawaited(widget.player.startQueue(tracks, index, title)),
          trailing: Row(mainAxisSize: MainAxisSize.min, children: [
            IconButton(
                tooltip:
                    '${_isLiked(track) ? 'Unlike' : 'Like'} ${track['title']}',
                icon: Icon(
                    _isLiked(track) ? Icons.favorite : Icons.favorite_border),
                onPressed: () => unawaited(widget.onLike(track))),
            IconButton(
                tooltip: 'Options for ${track['title']}',
                icon: const Icon(Icons.more_horiz),
                onPressed: () => _trackOptions(track)),
          ]));

  Widget _trackStrip(String title, List<Map<String, dynamic>> tracks) =>
      Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(
            padding: const EdgeInsets.only(top: 24, bottom: 10),
            child: Text(title, style: Theme.of(context).textTheme.titleLarge)),
        HomeTrackStrip(
            key: PageStorageKey('home-tracks:$title'),
            api: widget.api,
            tracks: tracks,
            isLiked: _isLiked,
            onLike: (track) => unawaited(widget.onLike(track)),
            onMenu: _trackOptions,
            onPlay: (index) =>
                unawaited(widget.player.startQueue(tracks, index, title))),
      ]);

  Map<String, dynamic> get _homeDiagnostics =>
      Map<String, dynamic>.from(data['diagnostics'] as Map? ?? {});
  List<Map<String, dynamic>> _laneItems(String lane, String type) {
    final lanes = _homeDiagnostics['home_tab_lanes'];
    final raw = lanes is Map ? lanes[lane] : null;
    return type == 'tracks'
        ? MusicApi.tracks(raw is Map ? raw[type] : raw)
        : maps(raw is Map ? raw[type] : null);
  }

  List<String> get _availableLanes {
    final diagnostics = _homeDiagnostics['home_tab_diagnostics'];
    final requestedMinimum =
        diagnostics is Map ? diagnostics['min_count'] : null;
    final minimum = diagnostics is Map && diagnostics['accepted'] == true
        ? (requestedMinimum is num && requestedMinimum > 0
            ? requestedMinimum.toInt()
            : 8)
        : 6;
    return [
      'all',
      for (final lane in ['chill', 'workout', 'focus', 'mood'])
        if (_laneItems(lane, 'tracks').length >= minimum) lane
    ];
  }

  Future<void> load(
      {bool refresh = false,
      bool autoCorrect = true,
      bool manualSearch = false}) async {
    if (!widget.home && query.text.trim().isEmpty) return;
    if (!widget.home && manualSearch) widget.api.clearSearchContext();
    final version = ++generation;
    poll?.cancel();
    homePoll?.cancel();
    homeAwaitingPreparation = false;
    pagedItems.clear();
    pagedCursors.clear();
    pollAttempts = 0;
    widget.player.preloadSearch([]);
    submittedQuery = query.text.trim();
    automatic = autoCorrect;
    setState(() {
      busy = true;
      submitted = true;
      error = null;
      detail = null;
      if (!widget.home || data.isEmpty) data = {};
      extras.clear();
      seen.clear();
      pagination = {};
    });
    try {
      final promoteOnLaunch = widget.home && !refresh && !launchedHome;
      final result = widget.home
          ? await catalog.home(
              refresh: refresh,
              launchToken: promoteOnLaunch ? launchToken : '',
              signals: widget.homeSignals?.call() ?? const {})
          : await catalog.search(submittedQuery, autoCorrect: automatic);
      if (!mounted || version != generation) return;
      if (widget.home) {
        launchedHome = true;
        lastHomeRevalidatedAt = widget.api.clock();
      }
      if (!widget.home && manualSearch)
        widget.api.setManualSearchContext(submittedQuery);
      setState(() {
        data = result;
        revision = (((result['diagnostics']
                            as Map?)?['search_snapshot_revision'] ??
                        (result['diagnostics'] as Map?)?['snapshot_revision'])
                    as num? ??
                0)
            .toInt();
        pagination =
            Map<String, dynamic>.from(result['pagination'] as Map? ?? {});
        for (final entry in surfaces.entries) {
          if (entry.key == 'results' && result['tracks'] is List) continue;
          for (final item in maps(result[entry.key])) {
            seen.add(identity(entry.value, item));
          }
        }
      });
      if (!widget.home) {
        widget.player
            .preloadSearch(MusicApi.tracks(data['tracks'] ?? data['results']));
        _scheduleEnrichment(version, 0);
      } else if (_homePreparing(result)) {
        homeAwaitingPreparation = true;
        _scheduleHomeStatus(version);
      } else {
        homeAwaitingPreparation = false;
        homePoll?.cancel();
      }
    } catch (_) {
      if (mounted && version == generation)
        setState(() {
          error =
              'Could not load ${widget.home ? 'Home' : 'search'}. Please retry.';
          if (!widget.home) data = {};
        });
    } finally {
      if (mounted && version == generation) setState(() => busy = false);
    }
  }

  void _scheduleHomePrepare() {
    homePrepare?.cancel();
    homePrepare = Timer(
        const Duration(milliseconds: 750), () => unawaited(_prepareHome()));
  }

  Future<void> _prepareHome() async {
    if (!mounted || !widget.home || !widget.homeSignalsReady) return;
    if (prepareInFlight) {
      prepareAgain = true;
      return;
    }
    final signals = widget.homeSignals?.call() ?? const <String, dynamic>{};
    final signature = jsonEncode(signals);
    if (signature == lastPreparedSignals &&
        lastPreparedAt != null &&
        DateTime.now().difference(lastPreparedAt!) <
            const Duration(seconds: 45)) return;
    final version = generation;
    prepareInFlight = true;
    try {
      final response = await catalog.home(prepare: true, signals: signals);
      if (!mounted || version != generation) return;
      lastPreparedSignals = signature;
      lastPreparedAt = DateTime.now();
      // Preparation only updates availability. It never replaces visible rows.
      final current = data['diagnostics'] as Map? ?? const {};
      final incoming = response['diagnostics'] as Map? ?? const {};
      if ((incoming['queue_revision'] as num? ?? 0) >=
          (current['queue_revision'] as num? ?? 0)) {
        setState(() => data['diagnostics'] = {...current, ...incoming});
      }
      homeAwaitingPreparation = true;
      _scheduleHomeStatus(version);
    } catch (_) {
      /* A later listening signal or explicit refresh can retry. */
    } finally {
      prepareInFlight = false;
      if (prepareAgain && mounted) {
        prepareAgain = false;
        _scheduleHomePrepare();
      }
    }
  }

  bool _homePreparing(Map<String, dynamic> payload) {
    final diagnostics = payload['diagnostics'] is Map
        ? payload['diagnostics'] as Map
        : const {};
    final action =
        '${payload['feed_action'] ?? diagnostics['feed_action'] ?? ''}';
    final state =
        '${payload['preparation_state'] ?? diagnostics['artifact_status'] ?? diagnostics['preparation_state'] ?? ''}'
            .toLowerCase();
    final rows = maps(payload['rows']);
    return rows.isEmpty &&
        (action == 'preparing_initial' ||
            state.contains('prepar') ||
            diagnostics['queue_build_inflight'] == true);
  }

  void _scheduleHomeStatus(int version) {
    homePoll?.cancel();
    if (!widget.home ||
        !homeActive ||
        !mounted ||
        version != generation ||
        !homeAwaitingPreparation) return;
    homePoll = Timer(const Duration(seconds: 1), () {
      homePoll = null;
      unawaited(_pollHomeStatus(version));
    });
  }

  Future<void> _pollHomeStatus(int version, {bool wait = true}) async {
    if (!widget.home || !homeActive || !mounted || version != generation)
      return;
    if (homeStatusInFlight) {
      _scheduleHomeStatus(version);
      return;
    }
    homeStatusInFlight = true;
    try {
      final diagnostics =
          data['diagnostics'] is Map ? data['diagnostics'] as Map : const {};
      final status = await catalog.homeQueueStatus(
        revision: (diagnostics['queue_revision'] as num?)?.toInt() ?? 0,
        wait: wait,
      );
      if (!mounted || !homeActive || version != generation) return;
      final incomingDiagnostics = status['diagnostics'] is Map
          ? status['diagnostics'] as Map
          : const {};
      final currentRevision =
          (diagnostics['queue_revision'] as num?)?.toInt() ?? 0;
      final incomingRevision =
          (incomingDiagnostics['queue_revision'] as num?)?.toInt() ??
              currentRevision;
      if (incomingRevision < currentRevision) {
        _scheduleHomeStatus(version);
        return;
      }
      if (incomingRevision >= currentRevision) {
        setState(() => data = {
              ...data,
              'diagnostics': {
                ...Map<String, dynamic>.from(diagnostics),
                ...Map<String, dynamic>.from(incomingDiagnostics)
              }
            });
      }
      if (incomingRevision >= currentRevision && _homeReady(status)) {
        if (maps(data['rows']).isEmpty)
          await _loadHomeArtifact(version);
        else
          homeAwaitingPreparation = false;
      } else if (!_homeFailed(status) &&
          (_homePreparing(data) || _homePreparing(status))) {
        _scheduleHomeStatus(version);
      } else if (!_homeFailed(status) &&
          (incomingDiagnostics['queue_build_inflight'] == true ||
              '${incomingDiagnostics['preparation_state'] ?? ''}'
                  .contains('prepar'))) {
        _scheduleHomeStatus(version);
      } else {
        homeAwaitingPreparation = false;
      }
    } catch (_) {
      if (mounted && homeActive && version == generation)
        _scheduleHomeStatus(version);
    } finally {
      homeStatusInFlight = false;
      if (mounted &&
          widget.home &&
          homeActive &&
          version == generation &&
          homeAwaitingPreparation &&
          homePoll == null) {
        _scheduleHomeStatus(version);
      }
    }
  }

  bool _homeReady(Map<String, dynamic> payload) {
    final diagnostics = payload['diagnostics'] is Map
        ? payload['diagnostics'] as Map
        : const {};
    final ready = (diagnostics['ready_feed_count'] as num?)?.toInt() ??
        (diagnostics['ready_feed_depth'] as num?)?.toInt() ??
        0;
    return ready > 0 ||
        (diagnostics['active_artifact_id']?.toString().trim().isNotEmpty ??
            false);
  }

  bool _homeFailed(Map<String, dynamic> payload) {
    final diagnostics = payload['diagnostics'] is Map
        ? payload['diagnostics'] as Map
        : const {};
    final state =
        '${diagnostics['artifact_status'] ?? diagnostics['preparation_state'] ?? ''}'
            .toLowerCase();
    final action =
        '${payload['feed_action'] ?? diagnostics['feed_action'] ?? ''}'
            .toLowerCase();
    return state.contains('fail') ||
        state.contains('error') ||
        action.contains('fail') ||
        action.contains('error');
  }

  Future<void> _loadHomeArtifact(int version) async {
    if (!homeActive || version != generation) return;
    try {
      final result =
          await catalog.home(signals: widget.homeSignals?.call() ?? const {});
      if (!mounted || !homeActive || version != generation) return;
      setState(() {
        data = result;
        error = null;
      });
      homeAwaitingPreparation = _homePreparing(result);
      if (homeAwaitingPreparation)
        _scheduleHomeStatus(version);
      else
        homePoll?.cancel();
    } catch (_) {
      if (mounted && version == generation) _scheduleHomeStatus(version);
    }
  }

  void _scheduleEnrichment(int version, int count) {
    poll?.cancel();
    if (count >= 8 ||
        !mounted ||
        version != generation ||
        !widget.active ||
        detail != null) return;
    final diagnostics = data['diagnostics'] as Map?;
    if (diagnostics?.containsKey('search_pending_surfaces') == true &&
        (diagnostics!['search_pending_surfaces'] as List?)?.isEmpty == true)
      return;
    poll = Timer(const Duration(seconds: 3), () async {
      if (!mounted || version != generation || !widget.active) return;
      try {
        final preferred =
            (data['search_preference'] as Map?)?['query']?.toString();
        final corrected =
            (data['query_correction'] as Map?)?['corrected']?.toString();
        final next = await catalog.search(
            preferred ?? corrected ?? submittedQuery,
            autoCorrect: false,
            continuation: {'revision': revision, 'revision_wait_ms': 1000});
        if (!mounted || version != generation) return;
        _append(next);
      } catch (_) {
        /* Existing results remain usable; explicit retry is available. */
      }
      pollAttempts = count + 1;
      _scheduleEnrichment(version, pollAttempts);
    });
  }

  void _append(Map<String, dynamic> next, {String? pageSurface}) {
    final nextDiagnostics = next['diagnostics'] as Map?;
    // A long-poll timeout acknowledges the current revision; it is not an
    // authoritative empty first page. Keep both results and pagination.
    if (pageSurface == null &&
        (nextDiagnostics?['revision_wait_timed_out'] == true ||
            next['not_modified'] == true)) return;
    final incomingRevision = ((nextDiagnostics?['search_snapshot_revision'] ??
                nextDiagnostics?['snapshot_revision']) as num? ??
            revision)
        .toInt();
    if (incomingRevision < revision) return;
    setState(() {
      if (pageSurface == null) {
        // A revision is an authoritative first page, including removals,
        // reordering and cleared targets. Separately fetched pages survive.
        for (final surface in surfaces.keys) {
          if (next.containsKey(surface)) data[surface] = maps(next[surface]);
        }
        for (final key in const [
          'top_result',
          'lead_artist',
          'containing_album',
          'query_correction'
        ]) {
          if (next.containsKey(key)) data[key] = next[key];
        }
      } else {
        final incoming = maps(next[pageSurface]);
        final values = pagedItems.putIfAbsent(pageSurface, () => []);
        for (final item in incoming) {
          final key = identity(pageSurface, item);
          final index =
              values.indexWhere((value) => identity(pageSurface, value) == key);
          if (index < 0)
            values.add(item);
          else
            values[index] = {...values[index], ...item};
        }
      }
      seen.clear();
      extras.clear();
      for (final entry in surfaces.entries) {
        if (entry.key == 'results' && data['tracks'] is List) continue;
        for (final item in maps(data[entry.key])) {
          seen.add(identity(entry.value, item));
        }
      }
      for (final entry in pagedItems.entries) {
        for (final item in entry.value) {
          if (seen.add(identity(entry.key, item)))
            extras.add((type: entry.key, item: item));
        }
      }
      if (next['diagnostics'] is Map) data['diagnostics'] = next['diagnostics'];
      if (next['pagination'] is Map)
        pagination = Map<String, dynamic>.from(next['pagination']);
      if (pageSurface != null &&
          (next['pagination'] as Map?)?[pageSurface] is Map) {
        pagedCursors[pageSurface] = Map<String, dynamic>.from(
            (next['pagination'] as Map)[pageSurface] as Map);
      }
      for (final entry in pagedCursors.entries) {
        final firstPage = pagination[entry.key] as Map? ?? const {};
        if ((entry.value['next_offset'] as num? ?? 0) >=
            (firstPage['next_offset'] as num? ?? 0)) {
          pagination[entry.key] = {...firstPage, ...entry.value};
        }
      }
      revision = incomingRevision;
    });
  }

  Future<void> more(String type) async {
    final page = pagination[type] as Map?;
    if (page == null || page['has_more'] != true || busy) return;
    final version = generation;
    poll?.cancel();
    setState(() {
      busy = true;
      error = null;
    });
    try {
      final preferred =
          (data['search_preference'] as Map?)?['query']?.toString();
      final corrected =
          (data['query_correction'] as Map?)?['corrected']?.toString();
      final next = await catalog.search(
          preferred ?? corrected ?? submittedQuery,
          autoCorrect: false,
          offset: (page['next_offset'] as num? ?? 0).toInt(),
          continuation: {'result_type': type});
      if (mounted && version == generation) _append(next, pageSurface: type);
    } catch (_) {
      if (mounted && version == generation)
        setState(() => error = 'More results could not load. Retry.');
    } finally {
      if (mounted && version == generation) {
        setState(() => busy = false);
        _scheduleEnrichment(version, pollAttempts);
      }
    }
  }

  void open(String kind, String id, String title) {
    poll?.cancel();
    setState(() => detail = (kind: kind, id: id, title: title));
  }

  void _closeDetail() {
    setState(() => detail = null);
    if (!widget.home) _scheduleEnrichment(generation, pollAttempts);
  }

  void _playSearchSelection(Map<String, dynamic> track) {
    // Start playback in this synchronous gesture turn; feedback is best effort.
    unawaited(widget.player.start(track));
  }

  Future<void> moreHome(Map<String, dynamic> row) async {
    final version = generation;
    setState(() => busy = true);
    try {
      final response = await catalog.homeRow('${data['session_id'] ?? ''}',
          '${row['id']}', (row['next_offset'] as num? ?? 0).toInt());
      if (!mounted || version != generation) return;
      if (response['row'] is! Map) throw StateError('Missing row');
      final next = Map<String, dynamic>.from(response['row']);
      final ids = <String>{};
      final items = [...maps(row['items']), ...maps(next['items'])]
          .where((item) => ids.add(identity('${row['item_type']}', item)))
          .toList();
      setState(() {
        row.addAll(next);
        row['items'] = items;
        data['rows'] = maps(data['rows'])
            .map((r) => r['id'] == row['id'] ? row : r)
            .toList();
      });
    } catch (_) {
      if (mounted && version == generation)
        setState(() => error = 'Could not load more recommendations. Retry.');
    } finally {
      if (mounted && version == generation) setState(() => busy = false);
    }
  }

  Widget itemTile(String type, Map<String, dynamic> item) {
    final title = (item['title'] ??
            item['radio_title'] ??
            item['name'] ??
            item['artist_name'] ??
            'Untitled')
        .toString();
    final image = _artwork(item, type);
    final leading = ClipRRect(
        borderRadius: BorderRadius.circular(type == 'artists' ? 24 : 6),
        child: ArtworkImage(urls: image, width: 48, height: 48));
    final collection = MusicApi.tracks(item['tracks']);
    if (collection.isNotEmpty)
      return ListTile(
          title: Text(title),
          subtitle: Text('${collection.length} songs'),
          leading: leading,
          trailing: const Icon(Icons.play_arrow),
          onTap: () =>
              unawaited(widget.player.startQueue(collection, 0, title)));
    final id =
        (item['id'] ?? item['browseId'] ?? item['browse_id'] ?? '').toString();
    final track = type == 'tracks' ? normalizeTrack(item) : null;
    final playable = track != null &&
        extractTrackId(track) != null &&
        track['playable'] != false;
    return ListTile(
        leading: leading,
        title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
            '${item['artist'] ?? item['artist_name'] ?? item['channel'] ?? labels[type] ?? ''}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis),
        onTap: type == 'tracks'
            ? (playable ? () => _playSearchSelection(track!) : null)
            : (id.isEmpty
                ? null
                : () =>
                    open(type == 'artists' ? 'artist' : 'album', id, title)),
        trailing: type == 'tracks'
            ? Row(mainAxisSize: MainAxisSize.min, children: [
                IconButton(
                    tooltip: 'Like $title',
                    onPressed: playable ? () => widget.onLike(track!) : null,
                    icon: const Icon(Icons.favorite_border)),
                IconButton(
                    tooltip: 'Play $title',
                    onPressed:
                        playable ? () => _playSearchSelection(track!) : null,
                    icon: const Icon(Icons.play_arrow)),
              ])
            : const Icon(Icons.chevron_right));
  }

  Widget section(String title, String type, List<Map<String, dynamic>> items) =>
      Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(
            padding: const EdgeInsets.only(top: 24, bottom: 8),
            child: Text(title, style: Theme.of(context).textTheme.titleLarge)),
        for (final item in items) itemTile(type, item),
      ]);

  Widget homeRow(Map<String, dynamic> row) {
    final kind = _homeType(row);
    final rowTitle = '${row['title'] ?? ''}';
    final items = maps(row['items']);
    if (kind == 'track' && row['kind'] == 'todays_pick' && items.isNotEmpty) {
      final track = normalizeTrack(items.first);
      return HomePickCard(
          api: widget.api,
          track: track,
          onPlay: extractTrackId(track) == null || track['playable'] == false
              ? null
              : () => unawaited(widget.player.startQueue(
                  items.map(normalizeTrack).toList(),
                  0,
                  rowTitle.isEmpty ? "Today's Pick" : rowTitle)));
    }
    if (kind == 'album' ||
        kind == 'artist' ||
        kind == 'mix' ||
        kind == 'radio') {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(
            padding: const EdgeInsets.only(top: 24, bottom: 8),
            child:
                Text(rowTitle, style: Theme.of(context).textTheme.titleLarge)),
        SizedBox(
            height: (kind == 'artist' ? 200 : 310) +
                (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
                        .clamp(0.0, double.infinity) *
                    100,
            child: ListView(
                key: PageStorageKey(
                    'home-collection:$kind:${row['id'] ?? row['kind'] ?? rowTitle}'),
                scrollDirection: Axis.horizontal,
                children: [
                  for (final item in items) _homeCollectionCard(kind, item),
                ])),
        if (row['has_more'] == true)
          TextButton(
              onPressed: busy ? null : () => moreHome(row),
              child: Text('More $rowTitle')),
      ]);
    }
    final tracks = MusicApi.tracks(items);
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      if (row['kind'] == 'last_played' || row['kind'] == 'frequently_listened')
        _trackStrip(rowTitle, tracks)
      else ...[
        Padding(
            padding: const EdgeInsets.only(top: 24, bottom: 8),
            child:
                Text(rowTitle, style: Theme.of(context).textTheme.titleLarge)),
        for (var index = 0; index < tracks.length; index++)
          _homeTrackTile(tracks[index], tracks, index, rowTitle),
      ],
      if (row['has_more'] == true)
        TextButton(
            onPressed: busy ? null : () => moreHome(row),
            child: Text('More $rowTitle')),
    ]);
  }

  @override
  Widget build(BuildContext context) {
    if (detail != null && detail!.kind == 'collection') {
      final selected = detail!;
      return Scaffold(
        appBar: AppBar(
            title: Text(selected.title),
            leading: IconButton(
                icon: const Icon(Icons.arrow_back), onPressed: _closeDetail)),
        body: ListView(padding: const EdgeInsets.all(20), children: [
          Row(children: [
            HomeArtwork(
                api: widget.api,
                item: _collectionDetailItem,
                kind: _collectionDetailKind,
                size: 112),
            const SizedBox(width: 18),
            Expanded(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                  Text(selected.title,
                      style: Theme.of(context).textTheme.titleLarge),
                  const SizedBox(height: 6),
                  Text('${_collectionDetailTracks.length} songs'),
                  if ('${_collectionDetailItem['description'] ?? ''}'
                      .isNotEmpty)
                    Text('${_collectionDetailItem['description']}',
                        maxLines: 3, overflow: TextOverflow.ellipsis),
                ])),
          ]),
          const SizedBox(height: 18),
          if (_collectionDetailTracks.isNotEmpty)
            Align(
                alignment: Alignment.centerLeft,
                child: FilledButton.icon(
                    label: const Text('Play all'),
                    icon: const Icon(Icons.play_arrow),
                    onPressed: () => unawaited(widget.player.startQueue(
                        _collectionDetailTracks, 0, selected.title)))),
          const SizedBox(height: 14),
          for (var index = 0; index < _collectionDetailTracks.length; index++)
            _homeTrackTile(_collectionDetailTracks[index],
                _collectionDetailTracks, index, selected.title),
        ]),
      );
    }
    if (detail case final selected?)
      return CatalogDetail(
          key: ValueKey('${selected.kind}:${selected.id}'),
          api: catalog,
          id: selected.id,
          kind: selected.kind,
          title: selected.title,
          player: widget.player,
          onLike: widget.onLike,
          isLiked: _isLiked,
          onTrackOptions: _trackOptions,
          onOpen: open,
          onBack: _closeDetail);
    final correction = data['query_correction'] as Map?;
    final rows = maps(data['rows']);
    final albumKeys = <String>{};
    final heroAlbums = [
      for (final row in rows)
        if (_homeType(row) == 'album') ...maps(row['items'])
    ]
        .where((album) => albumKeys.add(_homeDetailId(album, 'album').isNotEmpty
            ? _homeDetailId(album, 'album')
            : identity('album', album)))
        .toList();
    final selectedLane = _availableLanes.contains(homeLane) ? homeLane : 'all';
    final signals = widget.home
        ? widget.homeSignals?.call() ?? const <String, dynamic>{}
        : const <String, dynamic>{};
    final recent = maps(signals['last_played_tracks']);
    final frequent = maps(signals['top_track_snapshots']);
    final readyCount =
        ((data['diagnostics'] as Map?)?['ready_feed_count'] as num?)?.toInt() ??
            0;
    final initial = <String, List<Map<String, dynamic>>>{};
    final displayed = <String>{};
    for (final entry in surfaces.entries) {
      if (entry.key == 'results' && data['tracks'] is List) continue;
      if (entry.key == 'artist_tracks') continue;
      for (final item in maps(data[entry.key])) {
        if (displayed.add(identity(entry.value, item)))
          initial.putIfAbsent(entry.value, () => []).add(item);
      }
    }
    final top = data['top_result'] is Map
        ? Map<String, dynamic>.from(data['top_result'] as Map)
        : null;
    final topItem = top?['item'] is Map
        ? Map<String, dynamic>.from(top!['item'] as Map)
        : null;
    final topType =
        top == null ? '' : (top['entity_type'] ?? 'track').toString();
    final topSurface = topType == 'artist'
        ? 'artists'
        : topType == 'album'
            ? 'albums'
            : 'tracks';
    final lead = data['lead_artist'] is Map
        ? Map<String, dynamic>.from(data['lead_artist'] as Map)
        : null;
    final containingAlbum = data['containing_album'] is Map
        ? Map<String, dynamic>.from(data['containing_album'] as Map)
        : null;
    final topKey = topItem == null ? '' : identity(topSurface, topItem);
    final leadKey = lead == null ? '' : identity('artists', lead);
    final albumKey =
        containingAlbum == null ? '' : identity('albums', containingAlbum);
    for (final type in initial.keys.toList()) {
      initial[type] = initial[type]!.where((item) {
        final key = identity(type, item);
        if (key == topKey) return false;
        if (type == 'artists' && key == leadKey) return false;
        if (type == 'albums' && key == albumKey) return false;
        return true;
      }).toList();
    }
    final hasSearchResult = initial.values.any((items) => items.isNotEmpty) ||
        extras.isNotEmpty ||
        maps(data['artist_tracks']).isNotEmpty ||
        topItem != null ||
        lead != null ||
        containingAlbum != null;
    final artistTracks = maps(data['artist_tracks']);
    final additionalArtistTracks = filter == 'artists'
        ? artistTracks
        : artistTracks
            .where((item) =>
                identity('tracks', item) != topKey &&
                !(initial['tracks'] ?? const <Map<String, dynamic>>[]).any(
                    (track) =>
                        identity('tracks', track) == identity('tracks', item)))
            .toList();
    final artistName = lead?['name']?.toString() ?? '';
    return ListView(
        key: PageStorageKey(widget.home ? 'home' : 'search'),
        padding: const EdgeInsets.all(20),
        children: [
          Row(children: [
            Expanded(
                child: Text(
                    widget.home
                        ? 'Made for your listening'
                        : 'Find your next listen',
                    style: Theme.of(context).textTheme.headlineSmall)),
            if (widget.home)
              IconButton(
                  tooltip: 'Refresh Home',
                  onPressed: busy ? null : () => load(refresh: true),
                  icon: const Icon(Icons.refresh))
          ]),
          if (!widget.home) ...[
            const SizedBox(height: 16),
            TextField(
                controller: query,
                textInputAction: TextInputAction.search,
                onSubmitted: (_) => load(manualSearch: true),
                decoration: InputDecoration(
                    labelText: 'Search songs',
                    hintText: 'Songs, artists and albums',
                    border: const OutlineInputBorder(),
                    suffixIcon: IconButton(
                        tooltip: 'Search',
                        onPressed: () => load(manualSearch: true),
                        icon: const Icon(Icons.search)))),
            const SizedBox(height: 12),
            Wrap(spacing: 8, children: [
              for (final type in ['all', ...labels.keys])
                ChoiceChip(
                    label: Text(type == 'all' ? 'Top' : labels[type]!),
                    selected: filter == type,
                    onSelected: (_) => setState(() => filter = type))
            ]),
          ],
          if (busy) const LinearProgressIndicator(),
          if (error != null) ...[
            Text(error!),
            TextButton(onPressed: () => load(), child: const Text('Retry'))
          ],
          if (correction != null) ...[
            Text('Showing results for ${correction['corrected']}'),
            TextButton(
                onPressed: () {
                  query.text = '${correction['original']}';
                  unawaited(load(autoCorrect: false));
                },
                child: Text('Search instead for ${correction['original']}'))
          ],
          if (!widget.home && data['search_preference'] is Map)
            const Padding(
                padding: EdgeInsets.only(top: 8),
                child: Text('Based on your previous selection')),
          if (!widget.home &&
              topItem != null &&
              (filter == 'all' || filter == topSurface)) ...[
            Builder(builder: (context) {
              return Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Top result',
                        style: Theme.of(context).textTheme.titleLarge),
                    itemTile(topSurface, topItem!),
                  ]);
            }),
          ],
          if (!widget.home &&
              lead != null &&
              leadKey != topKey &&
              (filter == 'all' || filter == 'artists'))
            itemTile('artists', lead),
          if (!widget.home &&
              containingAlbum != null &&
              albumKey != topKey &&
              (filter == 'all' || filter == 'albums'))
            itemTile('albums', containingAlbum),
          if (widget.home) ...[
            if (readyCount > 0)
              TextButton(
                  onPressed: busy ? null : () => load(refresh: true),
                  child: const Text('New recommendations are ready')),
            HomeAlbumHero(
                key: const ValueKey('home-album-hero'),
                api: widget.api,
                albums: heroAlbums,
                active: homeActive,
                onOpen: (album) => _openHomeItem('album', album),
                onPlay: _playHomeAlbum),
            Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Wrap(spacing: 8, runSpacing: 6, children: [
                  for (final lane in _availableLanes)
                    ChoiceChip(
                        label: Text(lane == 'all'
                            ? 'All'
                            : '${lane[0].toUpperCase()}${lane.substring(1)}'),
                        selected: selectedLane == lane,
                        onSelected: (_) => setState(() => homeLane = lane)),
                ])),
            if (selectedLane == 'all' && widget.likedTracks.isNotEmpty)
              _trackStrip('Your likes', widget.likedTracks),
            if (selectedLane == 'all' &&
                recent.isNotEmpty &&
                !rows.any((row) => row['kind'] == 'last_played'))
              _trackStrip('Listening history', recent.take(12).toList()),
            if (selectedLane == 'all' &&
                frequent.isNotEmpty &&
                !rows.any((row) => row['kind'] == 'frequently_listened'))
              _trackStrip('Frequently listened', frequent.take(12).toList()),
            if (!busy && error == null && rows.isEmpty)
              Padding(
                  padding: const EdgeInsets.symmetric(vertical: 24),
                  child: Column(children: [
                    Text(_homeEmptyMessage(), textAlign: TextAlign.center),
                    const SizedBox(height: 8),
                    TextButton(
                        onPressed: () => load(refresh: true),
                        child: const Text('Retry Home')),
                  ])),
            if (selectedLane == 'all') ...[
              for (final row in rows)
                if (_homeType(row) != 'album') homeRow(row),
              for (final row in rows)
                if (_homeType(row) == 'album' && row['has_more'] == true)
                  TextButton(
                      onPressed: busy ? null : () => moreHome(row),
                      child: Text('More ${row['title'] ?? 'albums'}')),
            ] else ...[
              homeRow({
                'kind': 'discoveries',
                'item_type': 'track',
                'title': 'Discoveries',
                'items': _laneItems(selectedLane, 'tracks')
              }),
              if (_laneItems(selectedLane, 'albums').isNotEmpty)
                homeRow({
                  'item_type': 'album',
                  'title': 'Albums',
                  'items': _laneItems(selectedLane, 'albums')
                }),
              if (_laneItems(selectedLane, 'artists').isNotEmpty)
                homeRow({
                  'item_type': 'artist',
                  'title': 'Artists to explore',
                  'items': _laneItems(selectedLane, 'artists')
                }),
            ],
          ] else ...[
            if (submitted && !busy && error == null && !hasSearchResult)
              const Text('No results found.'),
            for (final type in labels.keys)
              if ((filter == 'all' || filter == type) &&
                  (initial[type]?.isNotEmpty ?? false))
                section(labels[type]!, type, initial[type]!),
            if (additionalArtistTracks.isNotEmpty &&
                (filter == 'all' || filter == 'tracks' || filter == 'artists'))
              section(
                  filter == 'artists'
                      ? (artistName.isEmpty
                          ? 'Works by this artist'
                          : 'Works by $artistName')
                      : (artistName.isEmpty
                          ? 'Top songs by this artist'
                          : 'Top songs by $artistName'),
                  'tracks',
                  additionalArtistTracks),
            // Separately fetched pages follow the current ranked first page.
            for (final extra in extras)
              if ((filter == 'all' || filter == extra.type) &&
                  identity(extra.type, extra.item) != topKey &&
                  identity(extra.type, extra.item) != leadKey &&
                  identity(extra.type, extra.item) != albumKey)
                itemTile(extra.type, extra.item),
            Wrap(children: [
              for (final type in labels.keys)
                if ((filter == 'all' || filter == type) &&
                    (pagination[type] as Map?)?['has_more'] == true)
                  TextButton(
                      onPressed: busy ? null : () => more(type),
                      child: Text('More ${labels[type]!.toLowerCase()}'))
            ]),
          ],
        ]);
  }

  String _homeEmptyMessage() {
    final diagnostics =
        data['diagnostics'] is Map ? data['diagnostics'] as Map : const {};
    final action = '${data['feed_action'] ?? diagnostics['feed_action'] ?? ''}';
    final state =
        '${diagnostics['artifact_status'] ?? diagnostics['preparation_state'] ?? ''}'
            .toLowerCase();
    if (action == 'preparing_initial' ||
        state.contains('prepar') ||
        diagnostics['queue_build_inflight'] == true) {
      return 'Your Home feed is being prepared. This can take a little while.';
    }
    if (error != null || state.contains('fail') || state.contains('error')) {
      return 'Home could not load right now. Please retry.';
    }
    return 'Your Home feed is empty. Listen to a few songs, then retry.';
  }

  @override
  void dispose() {
    generation++;
    poll?.cancel();
    homePoll?.cancel();
    homePrepare?.cancel();
    query.dispose();
    super.dispose();
  }
}
