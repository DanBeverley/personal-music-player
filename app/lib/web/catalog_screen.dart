import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import '../logic/track_metadata.dart';
import 'catalog_api.dart';
import 'catalog_detail.dart';
import 'artwork_image.dart';
import 'home_widgets.dart';
import 'design_system.dart';
import 'music_api.dart';
import 'web_player.dart';
import 'playback_port.dart';
import 'search_history.dart';
import 'discovery_shelves.dart';
import 'chart_region.dart';
import 'browse_widgets.dart';

class CatalogScreen extends StatefulWidget {
  final MusicApi api;
  final WebPlayer player;
  final bool home, active;
  final bool homeSignalsReady;
  final int homeSignalRevision;
  final int foregroundRevision;
  final bool foreground;
  final bool motionActive;
  final FocusNode? searchFocus;
  final int searchFocusRevision;
  final List<Map<String, dynamic>> likedTracks;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final Map<String, dynamic> Function()? homeSignals;
  final VoidCallback? onHomePainted;
  final SessionStore? historyStore;
  final SessionStore? homeStore;
  final ChartRegion? chartRegion;
  final Future<List<Map<String, dynamic>>> Function()? recentQueries;
  final ({Map<String, dynamic> artist, int revision})? artistShortcut;
  final List<Map<String, dynamic>> followedArtists;
  final ValueChanged<Map<String, dynamic>>? onFollowArtist;
  final CatalogApi? catalogApi;
  const CatalogScreen(
      {super.key,
      required this.api,
      required this.player,
      required this.home,
      required this.active,
      required this.onLike,
      this.onAddToPlaylist,
      this.homeSignals,
      this.historyStore,
      this.homeStore,
      this.chartRegion,
      this.recentQueries,
      this.onHomePainted,
      this.homeSignalsReady = true,
      this.homeSignalRevision = 0,
      this.foregroundRevision = 0,
      this.foreground = true,
      this.motionActive = true,
      this.searchFocus,
      this.searchFocusRevision = 0,
      this.artistShortcut,
      this.followedArtists = const [],
      this.onFollowArtist,
      this.catalogApi,
      this.likedTracks = const []});
  @override
  State<CatalogScreen> createState() => _CatalogScreenState();
}

class _CatalogScreenState extends State<CatalogScreen> {
  late final catalog = widget.catalogApi ?? CatalogApi(widget.api);
  late final history =
      SearchHistory(widget.api.userScopeId, store: widget.historyStore);
  final query = TextEditingController();
  final _inputFocus = FocusNode(debugLabel: 'Catalog search');
  final _searchScroll = ScrollController();
  FocusNode get _activeInputFocus => widget.searchFocus ?? _inputFocus;
  Map<String, dynamic> data = {}, pagination = {};
  final List<({String type, Map<String, dynamic> item})> extras = [];
  final Set<String> seen = {};
  final Map<String, List<Map<String, dynamic>>> pagedItems = {};
  final Map<String, Map<String, dynamic>> pagedCursors = {};
  int pollAttempts = 0;
  bool enrichmentStopped = false;
  bool busy = false, submitted = false, automatic = true;
  String? error;
  String submittedQuery = '', filter = 'all';
  String displayedQuery = '';
  int generation = 0;
  int revision = 0;
  Timer? poll, homePoll, homeRetry;
  int homeFailures = 0;
  DateTime? homeWaitStartedAt;
  Timer? homePrepare;
  Timer? searchDebounce;
  Completer<void>? searchCancel, suggestionCancel;
  Stopwatch? searchWatch;
  int suggestionVersion = 0;
  bool historyLoading = false;
  DateTime? historyLoadedAt;
  List<Map<String, dynamic>> suggestions = [];
  final String launchToken = 'web:${DateTime.now().microsecondsSinceEpoch}';
  String? _selectedTrackId;
  String lastPreparedSignals = '';
  DateTime? lastPreparedAt;
  bool prepareInFlight = false, prepareAgain = false, launchedHome = false;
  bool homeStatusInFlight = false, homeAwaitingPreparation = false;
  int? pendingHomeRefreshVersion;
  String pendingHomeRefreshToken = '';
  bool completingHomeRefresh = false;
  bool homePaintReported = false;
  bool _restoredHome = false;
  static const _homeStoreLimit = 384 * 1024;
  static const _homeRowOrder = {
    'featured_new_albums': -2,
    'todays_pick': -1,
    'made_for_you': 0,
    'last_played': 1,
    'because_you_played': 2,
    'quiet_picks': 3,
    'recommended_artists': 4,
    'popular_radio': 5,
    'recommended_albums': 6,
    'frequently_listened': 8,
  };
  DateTime? lastHomeRevalidatedAt;
  String homeLane = 'all';
  Map<String, dynamic>? _homeShelf;
  String _homeShelfSession = '';
  bool _homeShelfLoading = false;
  String? _homeShelfError;
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
    _selectedTrackId = extractTrackId(widget.player.current);
    widget.player.addListener(_playbackSelectionChanged);
    // Persisted Home can arrive independently of cloud library hydration.
    // Updated signals prepare successors without replacing the active feed.
    if (widget.home) {
      _restoreHome();
      if (homeActive) unawaited(load());
    }
    if (!widget.home && widget.active) unawaited(_loadHistory());
    if (!widget.home && widget.active) _focusSearchInput();
  }

  void _restoreHome() {
    try {
      final stored = widget.homeStore?.read();
      if (stored == null) return;
      final entry = jsonDecode(stored) as Map<String, dynamic>;
      final savedAt = DateTime.fromMillisecondsSinceEpoch(entry['savedAt'] as int);
      final age = widget.api.clock().difference(savedAt);
      final feed = entry['feed'] as Map<String, dynamic>;
      if (age.isNegative ||
          age > const Duration(hours: 24) ||
          !_hasHomeRows(feed)) return;
      data = feed;
      submitted = true;
      _restoredHome = true;
    } catch (_) {
      // Invalid or unavailable browser storage is a cache miss.
    }
  }

  bool _hasHomeRows(Map<String, dynamic> feed) =>
      maps(feed['rows']).any((row) => maps(row['items']).isNotEmpty);

  List<Map<String, dynamic>> _orderedHomeRows(Map<String, dynamic> feed) =>
      maps(feed['rows'])
        ..sort((a, b) =>
            (_homeRowOrder[a['kind']] ?? 9)
                .compareTo(_homeRowOrder[b['kind']] ?? 9));

  void _acceptHome(Map<String, dynamic> feed) {
    if (!_hasHomeRows(feed)) {
      if (!_hasHomeRows(data)) data = feed;
      return;
    }
    final store = widget.homeStore;
    final changed = _restoredHome ||
        !_hasHomeRows(data) ||
        _homeVersion(data) != _homeVersion(feed) ||
        data['session_id'] != feed['session_id'];
    data = feed;
    if (store == null || !changed) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !identical(data, feed)) return;
      try {
        // Keep complete row pages so mixes and row offsets still work.
        // Live revalidation supplies any remaining rows and mood lanes.
        final rows = <Map<String, dynamic>>[];
        var remaining = _homeStoreLimit - 1024;
        for (final row in _orderedHomeRows(feed)) {
          if (maps(row['items']).isEmpty) continue;
          final size = jsonEncode(row).length + 1;
          if (size > remaining) continue;
          rows.add(row);
          remaining -= size;
        }
        if (rows.isEmpty) return;
        final entry = jsonEncode({
          'savedAt': widget.api.clock().millisecondsSinceEpoch,
          'feed': {
            'session_id': feed['session_id'],
            'feed_version': _homeVersion(feed),
            'rows': rows,
          },
        });
        if (entry.length <= _homeStoreLimit) {
          store.write(entry);
          _restoredHome = false;
        }
      } catch (_) {
        // A full or unavailable cache must not prevent Home from opening.
      }
    });
  }

  void _focusSearchInput() {
    final revision = widget.searchFocusRevision;
    bool relevant() =>
        mounted &&
        !widget.home &&
        widget.active &&
        detail == null &&
        revision == widget.searchFocusRevision;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!relevant()) return;
      if (_searchScroll.hasClients) _searchScroll.jumpTo(0);
      if (_activeInputFocus.hasFocus) {
        _activeInputFocus.unfocus();
        FocusManager.instance.applyFocusChangesIfNeeded();
      }
      // Let the old native input detach before reopening it after navigation.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (relevant()) _activeInputFocus.requestFocus();
      });
      WidgetsBinding.instance.scheduleFrame();
    });
  }

  void _clearSearchInput() {
    query.clear();
    _queryChanged('');
    _focusSearchInput();
  }

  void _playbackSelectionChanged() {
    final selected = extractTrackId(widget.player.current);
    if (selected == _selectedTrackId) return;
    _selectedTrackId = selected;
    if (mounted && widget.active) setState(() {});
  }

  void _queryChanged(String value) {
    searchWatch = Stopwatch()..start();
    generation++;
    suggestionVersion++;
    _cancelSearchRequests();
    poll?.cancel();
    searchDebounce?.cancel();
    widget.api.clearSearchContext();
    displayedQuery = '';
    setState(() {
      suggestions = _localSuggestions(value);
      error = null;
      detail = null;
      // A new input owns a new result set. Keep suggestions separate from the
      // previous query; enrichment of an unchanged query still preserves it.
      data = {};
      extras.clear();
      pagination = {};
      pagedItems.clear();
      pagedCursors.clear();
      submitted = false;
      busy = false;
    });
    if (value.trim().isEmpty) return;
    searchDebounce = Timer(const Duration(milliseconds: 160), () {
      final version = suggestionVersion;
      unawaited(_suggest(value.trim(), version));
    });
  }

  Future<void> _suggest(String value, int version) async {
    if (suggestionCancel?.isCompleted == false) suggestionCancel!.complete();
    final cancel = suggestionCancel = Completer<void>();
    try {
      final incoming = await catalog.suggest(value,
          recentQueries: history.queries,
          recentTracks:
              maps(widget.homeSignals?.call()['recent_track_snapshots']),
          cancel: cancel.future);
      if (mounted &&
          suggestionVersion == version &&
          (busy || displayedQuery != value) &&
          query.text.trim() == value) {
        // Text completion may augment a saved playable match, never erase it.
        final merged = <Map<String, dynamic>>[];
        final keys = <String>{};
        for (final suggestion in [..._localSuggestions(value), ...incoming]) {
          final track = suggestion['track'];
          final key = track is Map
              ? 'track:${extractTrackId(Map<String, dynamic>.from(track))}'
              : 'query:${suggestion['text'] ?? suggestion['query'] ?? ''}'
                  .toLowerCase();
          if (keys.add(key)) merged.add(suggestion);
        }
        setState(() => suggestions = merged.take(6).toList());
      }
    } catch (_) {/* Suggestions never prevent full search. */}
  }

  void _cancelSearchRequests() {
    for (final request in [searchCancel, suggestionCancel]) {
      if (request?.isCompleted == false) request!.complete();
    }
  }

  List<Map<String, dynamic>> _localSuggestions(String value) {
    final needle = value.trim().toLowerCase();
    if (needle.isEmpty) return [];
    return [
      for (final entry in history.entries)
        if ('${entry['query']}'.toLowerCase().contains(needle))
          {
            'text': entry['query'],
            'track': entry['track'],
            'direct_play': entry['track'] != null
          }
    ].take(5).toList();
  }

  Future<void> _loadHistory() async {
    if (widget.home ||
        !widget.active ||
        historyLoading ||
        (historyLoadedAt != null &&
            DateTime.now().difference(historyLoadedAt!) <
                const Duration(seconds: 45))) return;
    historyLoading = true;
    final scope = widget.api.userScopeId;
    final incoming = <Map<String, dynamic>>[];
    await Future.wait([
      if (widget.recentQueries != null)
        () async {
          try {
            incoming.addAll(await widget.recentQueries!());
          } catch (_) {}
        }(),
      () async {
        try {
          incoming.addAll(await catalog.recentSearchPicks());
        } catch (_) {}
      }(),
    ]);
    if (!mounted || scope != widget.api.userScopeId) return;
    setState(() {
      history.merge(incoming);
      historyLoading = false;
      historyLoadedAt = DateTime.now();
    });
  }

  void _recordQuery(String value, {Map<String, dynamic>? track}) {
    history.record(value, track: track);
  }

  void _selectSuggestion(Map<String, dynamic> suggestion) {
    final value = '${suggestion['text'] ?? suggestion['query'] ?? ''}'.trim();
    final raw = suggestion['track'];
    if ((suggestion['direct_play'] == true ||
            suggestion['directPlay'] == true) &&
        raw is Map) {
      final track = normalizeTrack(Map<String, dynamic>.from(raw));
      if (extractTrackId(track) != null && track['playable'] != false) {
        generation++;
        suggestionVersion++;
        poll?.cancel();
        searchDebounce?.cancel();
        _cancelSearchRequests();
        suggestions = [];
        widget.api.setManualSearchContext(value);
        unawaited(widget.player.start(track));
        _recordQuery(value, track: track);
        setState(() => busy = false);
        return;
      }
    }
    query.text = value;
    _queryChanged(value);
    _recordQuery(value);
    unawaited(load(manualSearch: true));
  }

  Widget _suggestionTile(Map<String, dynamic> suggestion) {
    final raw = suggestion['track'];
    final track =
        raw is Map ? normalizeTrack(Map<String, dynamic>.from(raw)) : null;
    final direct = (suggestion['direct_play'] == true ||
            suggestion['directPlay'] == true) &&
        track != null &&
        extractTrackId(track) != null &&
        track['playable'] != false;
    return ListTile(
        dense: true,
        leading: direct
            ? HomeArtwork(api: widget.api, item: track, kind: 'track', size: 40)
            : const Icon(Icons.search),
        title: Text(
            direct ? '${track['title']}' : '${suggestion['text'] ?? ''}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis),
        subtitle: direct
            ? Text('${track['artist'] ?? track['channel'] ?? ''}',
                maxLines: 1, overflow: TextOverflow.ellipsis)
            : null,
        trailing:
            Icon(direct ? Icons.play_arrow_rounded : Icons.north_west_rounded),
        onTap: () => _selectSuggestion(suggestion));
  }

  void _selectFilter(String value) {
    if (filter == value) return;
    generation++;
    _cancelSearchRequests();
    poll?.cancel();
    setState(() => filter = value);
    if (submitted && query.text.trim().isNotEmpty) {
      unawaited(load());
    }
  }

  @override
  void didUpdateWidget(covariant CatalogScreen old) {
    super.didUpdateWidget(old);
    final shortcut = widget.artistShortcut;
    if (!widget.home &&
        widget.active &&
        (!old.active ||
            widget.searchFocusRevision != old.searchFocusRevision)) {
      detail = null;
      _detailSeed = null;
      _detailOrigin = null;
      _focusSearchInput();
    }
    if (!widget.home && shortcut != null && shortcut != old.artistShortcut) {
      final artist = shortcut.artist;
      open('artist', '${artist['id']}', '${artist['name']}', seed: artist);
    }
    if (old.player != widget.player) {
      old.player.removeListener(_playbackSelectionChanged);
      _selectedTrackId = extractTrackId(widget.player.current);
      widget.player.addListener(_playbackSelectionChanged);
    }
    if (widget.home &&
        homeActive &&
        (!old.active ||
            !old.foreground ||
            widget.foregroundRevision != old.foregroundRevision)) {
      if (maps(data['rows']).isEmpty && !busy)
        unawaited(load());
      else
        _revalidateHome();
    }
    if (!homeActive) homePoll?.cancel();
    if (widget.home &&
        widget.homeSignalsReady &&
        (widget.homeSignalRevision != old.homeSignalRevision ||
            !old.homeSignalsReady)) {
      _scheduleHomePrepare();
    }
    if (!widget.home && old.active && !widget.active) {
      _activeInputFocus.unfocus();
      poll?.cancel();
      generation++;
      suggestionVersion++;
      searchDebounce?.cancel();
      _cancelSearchRequests();
      busy = false;
    }
    if (!widget.home && widget.active && !old.active) {
      unawaited(_loadHistory());
      if (submitted && query.text.trim() != displayedQuery) {
        unawaited(load());
        return;
      }
      if (submitted && detail == null)
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
    if (pendingHomeRefreshVersion != null) {
      homeAwaitingPreparation = true;
      unawaited(_pollHomeStatus(generation, wait: false));
      return;
    }
    // A return is an opportunity, not a forced refresh. The backend applies
    // the durable age/ready-bundle gates across tabs and devices.
    if (detail != null || _homeShelf != null) {
      unawaited(_pollHomeStatus(generation, wait: false));
    } else {
      unawaited(_returnHome(generation));
    }
  }

  Future<void> _returnHome(int version) async {
    homeStatusInFlight = true;
    try {
      final result = await catalog.home(
        launchToken:
            '$launchToken:return:${widget.api.clock().microsecondsSinceEpoch}',
        signals: widget.homeSignals?.call() ?? const {},
      );
      if (!mounted || !homeActive || version != generation) return;
      setState(() {
        // An unchanged response must not disturb rows or paging while browsing.
        if (result['feed_action'] == 'promoted_prepared' ||
            _homeVersion(result) > _homeVersion(data)) {
          _acceptHome(result);
          pagedItems.clear();
          pagedCursors.clear();
        } else {
          data = {
            ...data,
            'diagnostics': result['diagnostics'] ?? data['diagnostics']
          };
        }
      });
      widget.api.onDiagnostic?.call('home-return', {
        'action': result['feed_action'] ?? 'served_active',
        'sessionId': result['session_id'],
        'queueRevision': (result['diagnostics'] as Map?)?['queue_revision'],
      });
      homeAwaitingPreparation = _homePreparing(result);
      if (homeAwaitingPreparation) _scheduleHomeStatus(version);
    } catch (_) {
      // Keep the active feed; a later return or manual refresh retries.
    } finally {
      homeStatusInFlight = false;
    }
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

  Widget _homeCollectionCard(
    String kind,
    Map<String, dynamic> item, {
    double? width,
  }) {
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
            : () => _openHomeItem(kind, item),
      );
    return HomeCollectionCard(
      width: width,
      onArtworkOpen: kind == 'album'
          ? (rect) => open(
                'album',
                _homeDetailId(item, kind),
                title,
                seed: item,
                origin: rect,
              )
          : null,
      api: widget.api,
      item: item,
      kind: kind,
      title: title,
      subtitle: kind == 'radio'
          ? (artist.isEmpty ? 'Artist radio' : artist)
          : kind == 'album'
              ? [(artist.isEmpty ? 'Album' : artist), catalogReleaseYear(item)]
                  .where((v) => v.isNotEmpty)
                  .join(' · ')
              : collection.isEmpty
                  ? 'Mix'
                  : '${collection.length} songs',
      onOpen: collection.isNotEmpty || _homeDetailId(item, kind).isNotEmpty
          ? () => _openHomeItem(kind, item)
          : null,
      onPlay: collection.isEmpty
          ? null
          : () => unawaited(widget.player.startQueue(collection, 0, title)),
    );
  }

  void _openHomeItem(String kind, Map<String, dynamic> item) {
    final title =
        '${item['radio_title'] ?? item['title'] ?? item['name'] ?? item['artist_name'] ?? ''}';
    if (kind == 'mix' || kind == 'radio') {
      final tracks = _collectionTracks(item);
      if (tracks.isNotEmpty) _openHomeCollection(kind, item, tracks);
    } else {
      final id = _homeDetailId(item, kind);
      if (id.isNotEmpty) open(kind, id, title, seed: item);
    }
  }

  void _playHomeAlbum(Map<String, dynamic> album) {
    final tracks = _collectionTracks(album);
    if (tracks.isEmpty) {
      _openHomeItem('album', album);
      return;
    }
    // Embedded previews start inside the gesture; otherwise open persisted detail.
    unawaited(
      widget.player.startQueue(
        tracks,
        0,
        '${album['title'] ?? album['name'] ?? 'Album'}',
      ),
    );
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

  void _openHomeCollection(
    String kind,
    Map<String, dynamic> item,
    List<Map<String, dynamic>> tracks,
  ) {
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
      setState(
        () => detail = (
          kind: 'collection',
          id: identity(kind, item),
          title: title,
        ),
      );
    }
  }

  List<Map<String, dynamic>> _collectionDetailTracks = const [];
  Map<String, dynamic> _collectionDetailItem = const {};
  String _collectionDetailKind = 'mix';

  void _openHomeShelf(Map<String, dynamic> row) {
    final shelf = Map<String, dynamic>.from(row);
    setState(() {
      _homeShelf = shelf;
      _homeShelfSession = '${data['session_id'] ?? ''}';
      _homeShelfLoading = false;
      _homeShelfError = null;
      detail = (
        kind: 'home_shelf',
        id: '${row['id'] ?? row['title']}',
        title: '${row['title'] ?? ''}'
      );
    });
    if (shelf['has_more'] == true) unawaited(moreHome(shelf));
  }

  void _playHomeShelfTrack(Map<String, dynamic>? row,
      List<Map<String, dynamic>> tracks, int index, String title) {
    if (const {
      'last_played', 'frequently_listened', 'quiet_picks', 'because_you_played',
    }.contains(row?['kind'] ?? row?['id'])) {
      widget.api.clearSearchContext();
      unawaited(widget.player.start(tracks[index]));
    } else {
      unawaited(widget.player.startQueue(tracks, index, title));
    }
  }

  Widget _homeShelfPage(Map<String, dynamic> row) {
    final kind = _homeType(row), title = '${row['title'] ?? ''}';
    final items = maps(row['items']);
    final tracks =
        kind == 'track' ? MusicApi.tracks(items) : <Map<String, dynamic>>[];
    return PageEntrance(
        key: ValueKey('home-shelf:${row['id'] ?? title}'),
        active: widget.active,
        child: Scaffold(
            appBar: AppBar(
                title: Text(title),
                leading: IconButton(
                    tooltip: 'Back to Home',
                    icon: const Icon(Icons.arrow_back),
                    onPressed: () => setState(() {
                          _homeShelf = null;
                          detail = null;
                        }))),
            body: CustomScrollView(
                key: PageStorageKey(
                    'home-shelf-scroll:${row['id'] ?? title}'),
                slivers: [
                  SliverPadding(
                    padding: const EdgeInsets.all(24),
                    sliver: kind == 'track'
                        ? HomeTrackStrip(
                            api: widget.api,
                            tracks: tracks,
                            sliver: true,
                            onMenu: _trackOptions,
                            onPlay: (index) => _playHomeShelfTrack(
                                row, tracks, index, title),
                          )
                        : SliverGrid.builder(
                            gridDelegate:
                                SliverGridDelegateWithMaxCrossAxisExtent(
                              maxCrossAxisExtent: 222,
                              mainAxisExtent: kind == 'artist' ? 216 : 310,
                              crossAxisSpacing: 12,
                              mainAxisSpacing: 20,
                            ),
                            itemCount: items.length,
                            itemBuilder: (_, index) =>
                                _homeCollectionCard(kind, items[index]),
                          ),
                  ),
                  if (_homeShelfError != null)
                    SliverToBoxAdapter(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text(_homeShelfError!),
                      ),
                    ),
                  if (row['has_more'] == true)
                    SliverToBoxAdapter(
                      child: Align(
                        alignment: Alignment.center,
                        child: TextButton.icon(
                          onPressed: _homeShelfLoading
                              ? null
                              : () => unawaited(moreHome(row)),
                          icon: _homeShelfLoading
                              ? const NeatieLoading(
                                  label: 'Loading recommendations', size: 16)
                              : const Icon(Icons.add),
                          label: Text(_homeShelfLoading ? 'Loading' : 'Load more'),
                        ),
                      ),
                    ),
                ])));
  }

  bool _isLiked(Map<String, dynamic> track) => widget.likedTracks.any(
        (item) => extractTrackId(item) == extractTrackId(track),
      );

  void _trackOptions(Map<String, dynamic> track) {
    final accountScope = widget.api.userScopeId;
    final title = '${track['title'] ?? ''}',
        artist = '${track['artist'] ?? track['channel'] ?? ''}';
    final albumId = '${track['album_id'] ?? track['album_browse_id'] ?? ''}';
    final artistId = '${track['artist_id'] ?? track['artist_browse_id'] ?? ''}';
    unawaited(
      showModalBottomSheet<void>(
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
                  Row(
                    children: [
                      HomeArtwork(
                        api: widget.api,
                        item: track,
                        kind: 'track',
                        size: 72,
                      ),
                      const SizedBox(width: 16),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              title,
                              style: Theme.of(context).textTheme.titleLarge,
                            ),
                            Text(artist),
                            if ('${track['album'] ?? ''}'.isNotEmpty)
                              Text('${track['album']}'),
                            if (trackDurationSeconds(track) > 0)
                              Text(
                                '${trackDurationSeconds(track) ~/ 60}:${(trackDurationSeconds(track) % 60).toString().padLeft(2, '0')}',
                              ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  ListTile(
                    leading: const Icon(Icons.play_arrow),
                    title: const Text('Play song and discover'),
                    enabled: track['playable'] != false,
                    onTap: () {
                      Navigator.pop(sheet);
                      unawaited(widget.player.start(track));
                    },
                  ),
                  ListTile(
                    leading: const Icon(Icons.playlist_play),
                    title: const Text('Play next'),
                    enabled: track['playable'] != false,
                    onTap: () {
                      Navigator.pop(sheet);
                      if (mounted && widget.api.userScopeId == accountScope)
                        widget.player.playNext(track);
                    },
                  ),
                  ListTile(
                    leading: Icon(
                      _isLiked(track) ? Icons.favorite : Icons.favorite_border,
                    ),
                    title: Text(
                      _isLiked(track) ? 'Remove from likes' : 'Add to likes',
                    ),
                    onTap: () {
                      Navigator.pop(sheet);
                      unawaited(widget.onLike(track));
                    },
                  ),
                  if (widget.onAddToPlaylist != null)
                    ListTile(
                        leading: const Icon(Icons.playlist_add_rounded),
                        title: const Text('Add to playlist'),
                        onTap: () {
                          Navigator.pop(sheet);
                          if (mounted && widget.api.userScopeId == accountScope)
                            unawaited(widget.onAddToPlaylist!(track));
                        }),
                  if (albumId.isNotEmpty)
                    ListTile(
                      leading: const Icon(Icons.album_outlined),
                      title: const Text('Open album'),
                      onTap: () {
                        Navigator.pop(sheet);
                        open('album', albumId, '${track['album'] ?? 'Album'}');
                      },
                    ),
                  if (artistId.isNotEmpty)
                    ListTile(
                      leading: const Icon(Icons.person_outline),
                      title: const Text('Open artist'),
                      onTap: () {
                        Navigator.pop(sheet);
                        open('artist', artistId, artist);
                      },
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _trackStrip(
    String title,
    List<Map<String, dynamic>> tracks, {
    Map<String, dynamic>? row,
  }) {
    final previewSize = const {'quiet_picks', 'because_you_played'}
        .contains(row?['kind']) ? 8 : 5;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SectionHeading(
          title: title,
          action: tracks.length > 5 || row?['has_more'] == true
              ? BrowseMoreButton(
                  label: 'View all $title',
                  onPressed: () => _openHomeShelf({
                        ...?row,
                        'title': title,
                        'item_type': 'track',
                        'items': tracks
                      }))
              : null,
        ),
        HomeTrackStrip(
          key: PageStorageKey('home-tracks:$title'),
          api: widget.api,
          tracks: tracks.take(previewSize).toList(),
          onMenu: _trackOptions,
          onPlay: (index) => _playHomeShelfTrack(row, tracks, index, title),
        ),
      ],
    );
  }

  Map<String, dynamic> get _homeDiagnostics => Map<String, dynamic>.from(
        (widget.home
                ? data['diagnostics']
                : PageStorage.maybeOf(context)?.readState(
                    context,
                    identifier:
                        'discovery-lanes:${widget.api.homeRequestScopeId}',
                  )) as Map? ??
            {},
      );
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
    if (widget.home && busy) return;
    if (!widget.home && query.text.trim().isEmpty) return;
    if (widget.home) {
      homeRetry?.cancel();
      homeWaitStartedAt = widget.api.clock();
      if (refresh) homeFailures = 0;
      if (refresh && pendingHomeRefreshVersion == null) {
        pendingHomeRefreshVersion = _homeVersion(data);
        pendingHomeRefreshToken =
            '$launchToken:refresh:${widget.api.clock().microsecondsSinceEpoch}';
      }
    }
    if (!widget.home) {
      searchDebounce?.cancel();
      if (searchCancel?.isCompleted == false) searchCancel!.complete();
      searchCancel = Completer<void>();
      if (manualSearch) {
        _recordQuery(query.text.trim());
        searchWatch = Stopwatch()..start();
      }
    }
    if (!widget.home && manualSearch) widget.api.clearSearchContext();
    final version = ++generation;
    final retryEmptySearch = !widget.home &&
        manualSearch &&
        submittedQuery == query.text.trim() &&
        !surfaces.keys.any((key) => maps(data[key]).isNotEmpty) &&
        maps(data['playlists']).isEmpty;
    final renderWatch = searchWatch ?? (Stopwatch()..start());
    poll?.cancel();
    homePoll?.cancel();
    homeAwaitingPreparation = false;
    pagedItems.clear();
    pagedCursors.clear();
    pollAttempts = 0;
    enrichmentStopped = false;
    widget.player.preloadSearch([]);
    submittedQuery = query.text.trim();
    automatic = autoCorrect;
    setState(() {
      busy = true;
      submitted = true;
      error = null;
      detail = null;
      if (data.isEmpty) data = {};
      extras.clear();
      seen.clear();
      pagination = {};
    });
    try {
      final promoteOnLaunch = widget.home && !refresh && !launchedHome;
      final refreshIntent = pendingHomeRefreshToken;
      final result = widget.home
          ? await catalog.home(
              refresh: refresh,
              refreshFromVersion: refresh ? pendingHomeRefreshVersion : null,
              refreshToken: pendingHomeRefreshToken,
              launchToken: promoteOnLaunch ? launchToken : '',
              signals: widget.homeSignals?.call() ?? const {})
          : await catalog.search(submittedQuery,
              autoCorrect: automatic,
              preview: false,
              forceRefresh: refresh || retryEmptySearch,
              cancel: searchCancel!.future,
              continuation: filter == 'all' ? null : {'result_type': filter});
      if (!mounted || version != generation) return;
      if (widget.home && refresh && refreshIntent != pendingHomeRefreshToken)
        return;
      if (widget.home) {
        homeFailures = 0;
        launchedHome = true;
        lastHomeRevalidatedAt = widget.api.clock();
        if (pendingHomeRefreshVersion != null &&
            _homeVersion(result) > pendingHomeRefreshVersion!) {
          pendingHomeRefreshVersion = null;
          pendingHomeRefreshToken = '';
        }
      }
      if (!widget.home && manualSearch)
        widget.api.setManualSearchContext(submittedQuery);
      if (!widget.home) {
        suggestionVersion++;
        if (suggestionCancel?.isCompleted == false)
          suggestionCancel!.complete();
      }
      setState(() {
        if (widget.home) {
          _acceptHome(result);
        } else {
          data = result;
        }
        if (!widget.home) {
          displayedQuery = submittedQuery;
          suggestions = [];
        }
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
        searchWatch = null;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted || version != generation || !widget.active) return;
          widget.api.onDiagnostic?.call('search-rendered', {
            'imagePlayerKey': 'search-top-$version',
            'topResultId':
                ((result['top_result'] as Map?)?['item'] as Map?)?['id'],
            'topResultType': (result['top_result'] as Map?)?['entity_type'],
            'inputToResultsMs': renderWatch.elapsedMilliseconds,
            'resultCount':
                MusicApi.tracks(result['tracks'] ?? result['results']).length,
            'preview': false,
            'queryLength': submittedQuery.length,
            'pendingSurfaceCount': ((result['diagnostics']
                        as Map?)?['search_pending_surfaces'] as List?)
                    ?.length ??
                0,
            'artworkReadyCount':
                MusicApi.tracks(result['tracks'] ?? result['results'])
                    .where((track) =>
                        widget.api.artworkUrls(track, 'track').isNotEmpty)
                    .length,
          });
        });
        widget.player
            .preloadSearch(MusicApi.tracks(data['tracks'] ?? data['results']));
        _scheduleEnrichment(version, 0);
      } else if (_homePreparing(result) || pendingHomeRefreshVersion != null) {
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
          if (!widget.home && displayedQuery != submittedQuery) data = {};
        });
      if (widget.home) _scheduleHomeRetry(version);
    } finally {
      if (mounted && version == generation) setState(() => busy = false);
    }
  }

  void _scheduleHomeRetry(int version) {
    homeRetry?.cancel();
    if (!mounted || !homeActive || version != generation || homeFailures >= 3)
      return;
    homeFailures++;
    homeRetry = Timer(Duration(seconds: 1 << homeFailures), () {
      if (mounted && homeActive && version == generation) unawaited(load());
    });
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
      if ((incoming['queue_revision'] as num? ?? 0) >=
              (current['queue_revision'] as num? ?? 0) &&
          _homeReady(response)) {
        if (pendingHomeRefreshVersion != null) {
          await _completeHomeRefresh(version);
        } else if (maps(data['rows']).isEmpty) {
          await _loadHomeArtifact(version);
        } else {
          homePoll?.cancel();
          setState(() {
            homeAwaitingPreparation = false;
            homeWaitStartedAt = null;
            if (error ==
                'New recommendations aren’t ready yet. You can keep listening and try again later.')
              error = null;
          });
        }
        if (homeAwaitingPreparation) _scheduleHomeStatus(version);
        return;
      }
      if (!homeAwaitingPreparation) homeWaitStartedAt = widget.api.clock();
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
    homeWaitStartedAt ??= widget.api.clock();
    if (widget.api.clock().difference(homeWaitStartedAt!) >=
        const Duration(seconds: 90)) {
      setState(() {
        homeAwaitingPreparation = false;
        pendingHomeRefreshVersion = null;
        pendingHomeRefreshToken = '';
        error = maps(data['rows']).isEmpty
            ? 'Home is taking longer than expected. Please retry, or keep listening while it prepares.'
            : 'New recommendations aren’t ready yet. You can keep listening and try again later.';
      });
      return;
    }
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
      if (pendingHomeRefreshVersion != null &&
          (_homeReady(status) ||
              _homeVersion(status) > pendingHomeRefreshVersion!)) {
        await _completeHomeRefresh(version);
      } else if ('${incomingDiagnostics['active_artifact_id'] ?? ''}'.isNotEmpty &&
          (maps(data['rows']).isEmpty ||
              homeAwaitingPreparation && _homeVersion(status) > _homeVersion(data))) {
        await _loadHomeArtifact(version);
      } else if (incomingRevision >= currentRevision && _homeReady(status)) {
        if (maps(data['rows']).isEmpty)
          await _loadHomeArtifact(version);
        else {
          setState(() {
            homeAwaitingPreparation = false;
            homeWaitStartedAt = null;
            if (error ==
                'New recommendations aren’t ready yet. You can keep listening and try again later.')
              error = null;
          });
        }
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
        if (pendingHomeRefreshVersion != null) {
          setState(() {
            error = incomingDiagnostics['preparation_state'] == 'blocked'
                ? 'A fresh feed couldn’t be prepared yet. Your current recommendations are still available.'
                : 'No fresh recommendations are ready yet. Please try again later.';
            pendingHomeRefreshVersion = null;
            pendingHomeRefreshToken = '';
          });
        }
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
    return ready > 0;
  }

  int _homeVersion(Map<String, dynamic> payload) =>
      (payload['feed_version'] as num? ??
              (payload['diagnostics'] as Map?)?['feed_version'] as num? ??
              0)
          .toInt();

  Future<void> _completeHomeRefresh(int version) async {
    if (completingHomeRefresh || pendingHomeRefreshVersion == null) return;
    completingHomeRefresh = true;
    final baseline = pendingHomeRefreshVersion!;
    final intent = pendingHomeRefreshToken;
    try {
      final result = await catalog.home(
          refresh: true,
          refreshFromVersion: baseline,
          refreshToken: pendingHomeRefreshToken,
          signals: widget.homeSignals?.call() ?? const {});
      if (!mounted || !homeActive || version != generation) return;
      if (pendingHomeRefreshVersion != baseline ||
          pendingHomeRefreshToken != intent) return;
      if (_homeVersion(result) > baseline && _hasHomeRows(result)) {
        setState(() {
          _acceptHome(result);
          error = null;
          pagedItems.clear();
          pagedCursors.clear();
          pendingHomeRefreshVersion = null;
          pendingHomeRefreshToken = '';
          homeAwaitingPreparation = false;
        });
      } else {
        homeAwaitingPreparation = true;
      }
    } catch (_) {
      homeAwaitingPreparation = true;
    } finally {
      completingHomeRefresh = false;
    }
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
    return '${diagnostics['preparation_state'] ?? payload['preparation_state'] ?? ''}' ==
            'blocked' ||
        state.contains('fail') ||
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
        _acceptHome(result);
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
    if (!mounted || version != generation || !widget.active || detail != null)
      return;
    final diagnostics = data['diagnostics'] as Map?;
    if (diagnostics?.containsKey('search_pending_surfaces') == true &&
        (diagnostics!['search_pending_surfaces'] as List?)?.isEmpty == true)
      return;
    if (count >= 8) {
      setState(() => enrichmentStopped = true);
      widget.api.onDiagnostic?.call('search-enrichment-stopped', {
        'pollAttempts': count,
        'revision': revision,
        'queryLength': submittedQuery.length,
      });
      return;
    }
    // Long-poll the existing snapshot promptly; do not start a second search.
    // Back off after the first update and retain the eight-request budget.
    poll = Timer(
        Duration(
            milliseconds: count == 0
                ? 350
                : count == 1
                    ? 900
                    : 1500), () async {
      if (!mounted || version != generation || !widget.active) return;
      try {
        final preferred =
            (data['search_preference'] as Map?)?['query']?.toString();
        final corrected =
            (data['query_correction'] as Map?)?['corrected']?.toString();
        final next = await catalog.search(
            preferred ?? corrected ?? submittedQuery,
            limit: filter == 'artists'
                ? (maps(data['artists']).length +
                        (pagedItems['artists']?.length ?? 0))
                    .clamp(24, 100)
                : 24,
            autoCorrect: false,
            preview: false,
            cancel: searchCancel?.future,
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
    if (next['pagination_expired'] == true) {
      setState(() =>
          error = 'These results have refreshed. Search again to load more.');
      return;
    }
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
        final previousInventory =
            (data['diagnostics'] as Map?)?['inventory_id'];
        final nextInventory = nextDiagnostics?['inventory_id'];
        if (previousInventory != null &&
            nextInventory != null &&
            previousInventory != nextInventory) {
          pagedItems.clear();
          pagedCursors.clear();
        }
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
          cancel: searchCancel?.future,
          offset: (page['next_offset'] as num? ?? 0).toInt(),
          continuation: {
            'result_type': type,
            if (page['cursor'] != null) 'cursor': page['cursor']
          });
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

  Map<String, dynamic>? _detailSeed;
  Rect? _detailOrigin;
  void open(
    String kind,
    String id,
    String title, {
    Map<String, dynamic>? seed,
    Rect? origin,
  }) {
    _detailSeed = seed;
    _detailOrigin = origin;
    if (!widget.home && displayedQuery.isNotEmpty) _recordQuery(displayedQuery);
    if (!widget.home) _activeInputFocus.unfocus();
    poll?.cancel();
    setState(() => detail = (kind: kind, id: id, title: title));
  }

  void _closeDetail() {
    setState(() => detail = _homeShelf == null
        ? null
        : (
            kind: 'home_shelf',
            id: '${_homeShelf!['id'] ?? _homeShelf!['title']}',
            title: '${_homeShelf!['title'] ?? ''}'
          ));
    if (!widget.home) {
      _scheduleEnrichment(generation, pollAttempts);
      _focusSearchInput();
    }
  }

  void _playSearchSelection(Map<String, dynamic> track) {
    // Start playback in this synchronous gesture turn; feedback is best effort.
    if (displayedQuery == query.text.trim()) {
      widget.api.setManualSearchContext(displayedQuery);
    } else {
      widget.api.clearSearchContext();
    }
    final top = data['top_result'];
    final artist = top is Map && top['entity_type'] == 'artist' ? top['item'] : null;
    final artistId = artist is Map ? '${artist['id'] ?? ''}' : '';
    final artistName = artist is Map ? '${artist['name'] ?? ''}' : '';
    bool credited(Map<String, dynamic> item) =>
        item['artist_id'] == artistId ||
        (item['artist_ids'] is List && (item['artist_ids'] as List).contains(artistId));
    if (artistId.isNotEmpty &&
        artistName.trim().toLowerCase() == displayedQuery.trim().toLowerCase() &&
        displayedQuery == query.text.trim() && credited(track)) {
      unawaited(widget.player.startArtistQueue(
        track,
        maps(data['artist_tracks']).where(credited).toList(),
        artistName,
        loadSongs: () => catalog.artistWorks(artistId, 'songs'),
        wrap: false,
      ));
    } else {
      unawaited(widget.player.start(track));
    }
    if (displayedQuery == query.text.trim())
      _recordQuery(displayedQuery, track: track);
  }

  Future<void> moreHome(Map<String, dynamic> row) async {
    final session = _homeShelfSession;
    if (_homeShelfLoading ||
        session.isEmpty ||
        row['has_more'] != true ||
        !identical(row, _homeShelf)) return;
    final api = widget.api;
    final scope = api.homeRequestScopeId;
    final offset = (row['next_offset'] as num? ?? 0).toInt();
    bool ownsShelf() => mounted && identical(row, _homeShelf) &&
        identical(api, widget.api) && scope == api.homeRequestScopeId;
    setState(() {
      _homeShelfLoading = true;
      _homeShelfError = null;
    });
    try {
      final response = await catalog.homeRow(session, '${row['id']}', offset);
      if (!ownsShelf()) return;
      if (response['row'] is! Map) throw StateError('Missing row');
      final next = Map<String, dynamic>.from(response['row']);
      if (response['session_id'] != session || next['id'] != row['id'] ||
          (next['has_more'] == true &&
              (next['next_offset'] as num? ?? 0) <= offset)) {
        throw StateError('Invalid recommendation page');
      }
      final ids = <String>{};
      final items = [...maps(row['items']), ...maps(next['items'])]
          .where((item) => ids.add(identity('${row['item_type']}', item)))
          .toList();
      setState(() {
        row.addAll(next);
        row['items'] = items;
      });
    } catch (_) {
      if (ownsShelf()) {
        setState(() =>
            _homeShelfError = 'Could not load more recommendations. Retry.');
      }
    } finally {
      if (ownsShelf()) setState(() => _homeShelfLoading = false);
    }
  }

  void _openSearchEntity(
    String type,
    Map<String, dynamic> item, {
    Rect? origin,
  }) {
    final id = '${item['id'] ?? item['browseId'] ?? ''}';
    if (id.isNotEmpty)
      open(
        type == 'artists' ? 'artist' : 'album',
        id,
        '${item['title'] ?? item['name'] ?? ''}',
        seed: item,
        origin: origin,
      );
  }

  String _songLength(Map<String, dynamic> track) {
    final seconds = trackDurationSeconds(track);
    return seconds > 0
        ? '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}'
        : '';
  }

  Widget _searchSpotlight(String type, Map<String, dynamic> item,
          List<Map<String, dynamic>> songs) =>
      LayoutBuilder(builder: (context, bounds) {
        final feature = Padding(
            padding: const EdgeInsets.only(top: 16),
            child:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const BrowseSectionHeading(title: 'Top result'),
              _topResult(type, item),
            ]));
        if (filter != 'all' || songs.isEmpty) return feature;
        final matches = section('Songs', 'tracks', songs.take(5).toList());
        return bounds.maxWidth < 1000
            ? Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [feature, matches])
            : Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Expanded(flex: 5, child: feature),
                const SizedBox(width: 36),
                Expanded(flex: 7, child: matches),
              ]);
      });

  Widget _topResult(String type, Map<String, dynamic> item) => LayoutBuilder(
        builder: (context, bounds) {
          final track = type == 'tracks';
          final title = '${item['title'] ?? item['name'] ?? ''}';
          final albumMeta = data['containing_album'] as Map? ?? const {};
          final leadMeta = data['lead_artist'] as Map? ?? const {};
          final artist =
              '${item['artist'] ?? item['channel'] ?? item['artist_name'] ?? (track ? leadMeta['name'] : null) ?? ''}';
          final album =
                  '${item['album'] ?? (track ? albumMeta['title'] : null) ?? ''}',
              albumId =
                  '${item['album_id'] ?? (track ? albumMeta['id'] : null) ?? ''}';
          final canPlay = track &&
              extractTrackId(item) != null &&
              item['playable'] != false;
          return BrowseHero(
            key: const ValueKey('search-top-result'),
            title: title,
            eyebrow: track
                ? 'SONG'
                : type == 'artists'
                    ? 'ARTIST'
                    : 'ALBUM',
            compact: true,
            artist: type == 'artists',
            artwork: (size) => ArtworkOpen(
              onOpen: (rect) {
                if (track) {
                  if (canPlay) _playSearchSelection(normalizeTrack(item));
                } else {
                  _openSearchEntity(type, item, origin: rect);
                }
              },
              child: HomeArtwork(
                api: widget.api,
                item: item,
                kind: track
                    ? 'track'
                    : type == 'artists'
                        ? 'artist'
                        : 'album',
                size: size,
                diagnosticKey: 'search-top-$generation',
              ),
            ),
            subtitle:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (artist.isNotEmpty && type != 'artists')
                TextButton(
                  style: TextButton.styleFrom(
                    padding: EdgeInsets.zero,
                    alignment: Alignment.centerLeft,
                  ),
                  onPressed:
                      '${item['artist_id'] ?? leadMeta['id'] ?? ''}'.isEmpty
                          ? null
                          : () => open(
                                'artist',
                                '${item['artist_id'] ?? leadMeta['id']}',
                                artist,
                              ),
                  child: Text(
                    artist,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              if (album.isNotEmpty)
                TextButton(
                  style: TextButton.styleFrom(
                    padding: EdgeInsets.zero,
                    alignment: Alignment.centerLeft,
                  ),
                  onPressed: albumId.isEmpty
                      ? null
                      : () => open('album', albumId, album),
                  child: Text(
                    album,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
            ]),
            metadata: [catalogReleaseDate(item), if (track) _songLength(item)]
                .where((value) => value.isNotEmpty)
                .join('  ·  '),
            actions: [
              FilledButton.icon(
                onPressed: track
                    ? (canPlay
                        ? () => _playSearchSelection(
                              normalizeTrack(item),
                            )
                        : null)
                    : () => _openSearchEntity(type, item),
                icon: Icon(
                  track
                      ? Icons.play_arrow_rounded
                      : Icons.arrow_forward_rounded,
                ),
                label: Text(
                  track ? 'Play' : 'Open',
                ),
              ),
              if (canPlay)
                IconButton(
                  tooltip: 'Like $title',
                  onPressed: () => widget.onLike(normalizeTrack(item)),
                  icon: Icon(
                    _isLiked(item) ? Icons.favorite : Icons.favorite_border,
                  ),
                ),
              if (canPlay && widget.onAddToPlaylist != null)
                IconButton(
                    tooltip: 'Add $title to playlist',
                    onPressed: () =>
                        widget.onAddToPlaylist!(normalizeTrack(item)),
                    icon: const Icon(Icons.playlist_add_rounded)),
            ],
          );
        },
      );

  Widget _searchSong(Map<String, dynamic> item) => LayoutBuilder(
        key: ValueKey(identity('tracks', item)),
        builder: (context, bounds) {
          final track = normalizeTrack(item), wide = bounds.maxWidth >= 620;
          final title = '${item['title'] ?? ''}',
              artist = '${item['artist'] ?? item['channel'] ?? ''}';
          final album = '${item['album'] ?? ''}',
              albumId = '${item['album_id'] ?? ''}';
          final playable =
              extractTrackId(track) != null && track['playable'] != false;
          return BrowseTrackRow(
            title: title,
            artist: artist,
            duration: _songLength(item),
            albumTitle: [album, catalogReleaseDate(item)]
                .where((value) => value.isNotEmpty)
                .join(' · '),
            artwork: HomeArtwork(
                api: widget.api, item: track, kind: 'track', size: 46),
            onTap: playable ? () => _playSearchSelection(track) : null,
            album: TextButton(
              style: TextButton.styleFrom(
                alignment: Alignment.centerLeft,
              ),
              onPressed:
                  albumId.isEmpty ? null : () => open('album', albumId, album),
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(album, maxLines: 1, overflow: TextOverflow.ellipsis),
                    if (catalogReleaseDate(item).isNotEmpty)
                      Text(catalogReleaseDate(item),
                          style: const TextStyle(
                              fontSize: 10, color: NeatieDesign.muted)),
                  ]),
            ),
            actions: [
              if (wide)
                IconButton(
                  tooltip: 'Like $title',
                  onPressed: playable ? () => widget.onLike(track) : null,
                  icon: Icon(
                    _isLiked(track) ? Icons.favorite : Icons.favorite_border,
                    size: 20,
                  ),
                ),
              IconButton(
                tooltip: 'Play $title',
                onPressed: playable ? () => _playSearchSelection(track) : null,
                icon: const Icon(Icons.play_arrow_rounded),
              ),
              IconButton(
                tooltip: 'Options for $title',
                onPressed: () => _trackOptions(track),
                icon: const Icon(Icons.more_horiz, size: 20),
              ),
            ],
          );
        },
      );

  Widget itemTile(String type, Map<String, dynamic> item) {
    if (type == 'tracks' && MusicApi.tracks(item['tracks']).isEmpty)
      return _searchSong(item);
    final title = (item['title'] ??
            item['radio_title'] ??
            item['name'] ??
            item['artist_name'] ??
            'Untitled')
        .toString();
    final image = _artwork(item, type);
    final leading = ClipRRect(
      borderRadius: BorderRadius.circular(type == 'artists' ? 24 : 6),
      child: ArtworkImage(urls: image, width: 48, height: 48),
    );
    final collection = MusicApi.tracks(item['tracks']);
    if (collection.isNotEmpty)
      return ListTile(
        key: ValueKey(identity(type, item)),
        title: Text(title),
        subtitle: Text('${collection.length} songs'),
        leading: leading,
        trailing: const Icon(Icons.play_arrow),
        onTap: () => unawaited(widget.player.startQueue(collection, 0, title)),
      );
    final id =
        (item['id'] ?? item['browseId'] ?? item['browse_id'] ?? '').toString();
    final track = type == 'tracks' ? normalizeTrack(item) : null;
    final playable = track != null &&
        extractTrackId(track) != null &&
        track['playable'] != false;
    return ListTile(
      key: ValueKey(identity(type, item)),
      leading: leading,
      title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text(
        '${item['artist'] ?? item['artist_name'] ?? item['channel'] ?? labels[type] ?? ''}',
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
      ),
      onTap: type == 'tracks'
          ? (playable ? () => _playSearchSelection(track!) : null)
          : (id.isEmpty
              ? null
              : () => open(type == 'artists' ? 'artist' : 'album', id, title)),
      trailing: type == 'tracks'
          ? Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                IconButton(
                  tooltip: 'Like $title',
                  onPressed: playable ? () => widget.onLike(track!) : null,
                  icon: const Icon(Icons.favorite_border),
                ),
                IconButton(
                  tooltip: 'Play $title',
                  onPressed:
                      playable ? () => _playSearchSelection(track!) : null,
                  icon: const Icon(Icons.play_arrow),
                ),
              ],
            )
          : const Icon(Icons.chevron_right),
    );
  }

  Widget section(
    String title,
    String type,
    List<Map<String, dynamic>> items,
  ) =>
      Padding(
        padding: const EdgeInsets.only(top: 16, bottom: 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            BrowseSectionHeading(
              title: title,
              action: filter == 'all' &&
                      title != 'Similar artists' &&
                      ((pagination[type] as Map?)?['total'] as num? ?? 0) >
                          items.length
                  ? Tooltip(
                      message:
                          'View all ${(pagination[type] as Map)['total']} ${labels[type]!.toLowerCase()}',
                      child: TextButton(
                        onPressed: busy ? null : () => _selectFilter(type),
                        child: const Text('See all'),
                      ),
                    )
                  : null,
            ),
            if (type == 'tracks') ...[
              for (final item in items) itemTile(type, item),
            ] else
              SizedBox(
                height: type == 'artists' ? 216 : 270,
                child: ListView.builder(
                  key: PageStorageKey('search-section:$title:$displayedQuery'),
                  scrollDirection: Axis.horizontal,
                  itemCount: items.length,
                  itemBuilder: (_, index) {
                    final item = items[index];
                    return type == 'artists'
                        ? HomeArtistCard(
                            api: widget.api,
                            item: item,
                            onOpen: () => _openSearchEntity(type, item),
                          )
                        : HomeCollectionCard(
                            api: widget.api,
                            item: item,
                            kind: 'album',
                            title: '${item['title'] ?? item['name'] ?? ''}',
                            subtitle: [
                              '${item['artist'] ?? item['channel'] ?? ''}',
                              catalogReleaseDate(item)
                            ].where((value) => value.isNotEmpty).join(' · '),
                            onPlay: null,
                            onOpen: () => _openSearchEntity(type, item),
                            onArtworkOpen: (rect) =>
                                _openSearchEntity(type, item, origin: rect),
                          );
                  },
                ),
              ),
          ],
        ),
      );

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
            : () => unawaited(
                  widget.player.startQueue(
                    items.map(normalizeTrack).toList(),
                    0,
                    rowTitle.isEmpty ? "Today's Pick" : rowTitle,
                  ),
                ),
      );
    }
    if (kind == 'album' ||
        kind == 'artist' ||
        kind == 'mix' ||
        kind == 'radio') {
      if (items.isEmpty) return const SizedBox.shrink();
      final prominent = row['kind'] == 'made_for_you';
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHeading(
            title: prominent ? 'Your rotation' : rowTitle,
            action: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (items.length > 3 || row['has_more'] == true)
                  BrowseMoreButton(
                    label: 'View all $rowTitle',
                    onPressed: () => _openHomeShelf(row),
                  ),
              ],
            ),
          ),
          LayoutBuilder(
            builder: (context, bounds) {
              final width = prominent
                  ? (bounds.maxWidth >= 700
                          ? bounds.maxWidth / 3
                          : bounds.maxWidth * .8)
                      .clamp(170.0, double.infinity)
                  : 202.0;
              final height = kind == 'artist'
                  ? 216.0
                  : width -
                      38 +
                      118 +
                      (MediaQuery.textScalerOf(context).scale(16) / 16 - 1)
                              .clamp(0.0, double.infinity) *
                          100;
              return SizedBox(
                height: height,
                child: ListView.builder(
                  key: PageStorageKey(
                    'home-collection:$kind:${row['id'] ?? row['kind'] ?? rowTitle}',
                  ),
                  scrollDirection: Axis.horizontal,
                  itemCount: items.length,
                  itemBuilder: (context, index) => _homeCollectionCard(
                    kind,
                    items[index],
                    width: kind == 'artist' ? null : width,
                  ),
                ),
              );
            },
          ),
        ],
      );
    }
    final signals = widget.homeSignals?.call() ?? const <String, dynamic>{};
    final historyKey = row['kind'] == 'last_played'
        ? 'last_played_tracks'
        : row['kind'] == 'frequently_listened'
            ? 'top_track_snapshots'
            : null;
    // Listening history is live account/device state, separate from immutable
    // recommendations. A previous feed must not override newly heard plays.
    final tracks = MusicApi.tracks(
      historyKey != null && signals['listening_history_available'] == true
          ? signals[historyKey]
          : items,
    );
    if (tracks.isEmpty) return const SizedBox.shrink();
    return _trackStrip(rowTitle, tracks, row: row);
  }

  List<({List<String> urls, double size})> _warmArtwork() {
    final result = <({List<String> urls, double size})>[];
    var target = result;
    final sections = <List<({List<String> urls, double size})>>[];
    void add(Map<String, dynamic> item, String kind, double size) {
      final urls = widget.api.artworkUrls(item, kind);
      if (urls.isNotEmpty) target.add((urls: urls, size: size));
    }

    if (widget.home) {
      for (final row in maps(data['rows'])) {
        target = <({List<String> urls, double size})>[];
        sections.add(target);
        final kind = _homeType(row);
        for (final item in maps(row['items']).take(kind == 'track' ? 4 : 3)) {
          final tracks =
              kind == 'mix' ? MusicApi.tracks(item['tracks']) : const [];
          if (kind == 'mix' && tracks.isNotEmpty) {
            if (row['kind'] != 'made_for_you') {
              for (final track in tracks.take(4)) add(track, 'track', 72);
            }
            add(tracks.first, 'track', 202);
          } else {
            add(
                item,
                kind,
                kind == 'track'
                    ? row['kind'] == 'todays_pick'
                        ? 96
                        : 44
                    : kind == 'artist'
                        ? 116
                        : row['kind'] == 'featured_new_albums'
                            ? MediaQuery.sizeOf(context).width < 560
                                ? 112
                                : 240
                            : 144);
          }
        }
      }
      // Give early shelves a slot before warming a second image for any shelf.
      for (var depth = 0; result.length < 12; depth++) {
        var added = false;
        for (final section in sections) {
          if (depth < section.length && result.length < 12) {
            result.add(section[depth]);
            added = true;
          }
        }
        if (!added) break;
      }
    } else {
      for (final type in ['tracks', 'artists', 'albums', 'similar_artists']) {
        if (type == 'tracks' && data['top_result'] is Map) {
          final top = data['top_result'] as Map;
          if (top['item'] is Map)
            add(
                Map<String, dynamic>.from(top['item'] as Map),
                '${top['entity_type'] ?? 'track'}',
                MediaQuery.sizeOf(context).width < 600 ? 92 : 164);
        }
        for (final item in maps(data[type]).take(3)) {
          add(
              item,
              type == 'tracks'
                  ? 'track'
                  : type == 'albums'
                      ? 'album'
                      : 'artist',
              type == 'tracks'
                  ? 46
                  : type == 'albums'
                      ? 144
                      : 116);
        }
      }
    }
    return result;
  }

  @override
  Widget build(BuildContext context) {
    if (detail?.kind == 'home_shelf' && _homeShelf != null)
      return _homeShelfPage(_homeShelf!);
    if (detail != null && detail!.kind == 'collection') {
      final selected = detail!;
      return Scaffold(
        appBar: AppBar(
          title: Text(selected.title),
          leading: IconButton(
            icon: const Icon(Icons.arrow_back),
            onPressed: _closeDetail,
          ),
        ),
        body: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Row(
              children: [
                HomeArtwork(
                  api: widget.api,
                  item: _collectionDetailItem,
                  kind: _collectionDetailKind,
                  size: MediaQuery.sizeOf(context).width >= 850 ? 184 : 112,
                ),
                const SizedBox(width: 18),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        selected.title,
                        style: NeatieDesign.display(
                          MediaQuery.sizeOf(context).width >= 850 ? 40 : 28,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text('${_collectionDetailTracks.length} songs'),
                      if ('${_collectionDetailItem['description'] ?? ''}'
                          .isNotEmpty)
                        Text(
                          '${_collectionDetailItem['description']}',
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                        ),
                    ],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 18),
            if (_collectionDetailTracks.isNotEmpty)
              Align(
                alignment: Alignment.centerLeft,
                child: FilledButton.icon(
                  label: const Text('Play all'),
                  icon: const Icon(Icons.play_arrow),
                  onPressed: () => unawaited(
                    widget.player.startQueue(
                      _collectionDetailTracks,
                      0,
                      selected.title,
                    ),
                  ),
                ),
              ),
            const SizedBox(height: 14),
            HomeTrackStrip(
                api: widget.api,
                tracks: _collectionDetailTracks,
                onMenu: _trackOptions,
                onPlay: (index) => unawaited(widget.player.startQueue(
                    _collectionDetailTracks, index, selected.title))),
          ],
        ),
      );
    }
    if (detail case final selected?)
      return CatalogDetail(
        key: ValueKey('${selected.kind}:${selected.id}'),
        active: widget.active && widget.foreground && widget.motionActive,
        api: catalog,
        initialItem: _detailSeed,
        artworkOrigin: _detailOrigin,
        id: selected.id,
        kind: selected.kind,
        title: selected.title,
        player: widget.player,
        onLike: widget.onLike,
        isLiked: _isLiked,
        onTrackOptions: _trackOptions,
        following: widget.followedArtists.any((artist) => artist['id'] == selected.id),
        onFollow: widget.onFollowArtist,
        onOpen: open,
        onBack: _closeDetail,
      );
    final correction = data['query_correction'] as Map?;
    final rows = widget.home ? _orderedHomeRows(data) : maps(data['rows']);
    if (widget.home &&
        !homePaintReported &&
        (rows.isNotEmpty ||
            error != null ||
            (submitted && !busy && !_homePreparing(data)))) {
      final version = generation;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted || version != generation || homePaintReported) return;
        homePaintReported = true;
        widget.onHomePainted?.call();
      });
    }
    final albumKeys = <String>{};
    final heroAlbums = [
      for (final row in rows)
        if (row['kind'] == 'featured_new_albums' ||
            (!rows.any((r) => r['kind'] == 'featured_new_albums') &&
                _homeType(row) == 'album'))
          ...maps(row['items']),
    ]
        .where(
          (album) => albumKeys.add(
            _homeDetailId(album, 'album').isNotEmpty
                ? _homeDetailId(album, 'album')
                : identity('album', album),
          ),
        )
        .toList();
    if (widget.home)
      PageStorage.maybeOf(context)?.writeState(
        context,
        _homeDiagnostics,
        identifier: 'discovery-lanes:${widget.api.homeRequestScopeId}',
      );
    final selectedLane = widget.home
        ? 'all'
        : (_availableLanes.contains(homeLane) ? homeLane : 'all');
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
      if (entry.key == 'artist_tracks' || entry.key == 'similar_artists')
        continue;
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
        return true;
      }).toList();
    }
    final hasSearchResult = filter == 'all'
        ? initial.values.any((items) => items.isNotEmpty) ||
            extras.isNotEmpty ||
            maps(data['artist_tracks']).isNotEmpty ||
            topItem != null ||
            lead != null ||
            containingAlbum != null
        : maps(data[filter]).isNotEmpty ||
            (filter == 'tracks' && maps(data['artist_tracks']).isNotEmpty) ||
            (filter == 'artists' && lead != null) ||
            (filter == 'albums' && containingAlbum != null) ||
            (topSurface == filter && topItem != null);
    final pendingSurfaces =
        (data['diagnostics'] as Map?)?['search_pending_surfaces'] as List? ??
            const [];
    final selectedPending = filter == 'all'
        ? pendingSurfaces.isNotEmpty
        : pendingSurfaces.contains(filter);
    final exhaustedSurfaces =
        (data['diagnostics'] as Map?)?['search_exhausted_surfaces'] as List? ??
            const [];
    final selectedExhausted = filter == 'all'
        ? exhaustedSurfaces.isNotEmpty
        : exhaustedSurfaces.contains(filter);
    final artistTracks = maps(data['artist_tracks']);
    final additionalArtistTracks = filter == 'artists'
        ? artistTracks
        : artistTracks
            .where(
              (item) =>
                  identity('tracks', item) != topKey &&
                  !(initial['tracks'] ?? const <Map<String, dynamic>>[]).any(
                    (track) =>
                        identity('tracks', track) == identity('tracks', item),
                  ),
            )
            .toList();
    final artistName = lead?['name']?.toString() ?? '';
    return ArtworkWarmup(
        enabled: widget.active &&
            widget.foreground &&
            widget.motionActive &&
            !widget.player.busy,
        items: _warmArtwork(),
        child: PageEntrance(
          active: true,
          duration: const Duration(milliseconds: 420),
          child: ListView(
            key: PageStorageKey(widget.home ? 'home' : 'search'),
            controller: widget.home ? null : _searchScroll,
            cacheExtent: 700,
            padding: EdgeInsets.fromLTRB(
              MediaQuery.sizeOf(context).width >= 850 ? 36 : 20,
              0,
              MediaQuery.sizeOf(context).width >= 850 ? 36 : 20,
              36,
            ),
            children: [
              if (!widget.home)
                const SearchMasthead()
              else
                EditorialHeading(
                  title: widget.home
                      ? (DateTime.now().hour < 12
                          ? 'Good morning.'
                          : DateTime.now().hour < 18
                              ? 'Good afternoon.'
                              : 'Good evening.')
                      : 'Find your next favourite.',
                  eyebrow: widget.home ? '' : 'EXPLORE NEATIE',
                  subtitle: widget.home
                      ? 'What sounds good today?'
                      : 'Songs, artists and albums. Follow your curiosity.',
                  action: widget.home
                      ? IconButton.outlined(
                          tooltip: readyCount > 0
                              ? 'New recommendations are ready'
                              : 'Refresh Home',
                          onPressed: busy || pendingHomeRefreshVersion != null
                              ? null
                              : () => load(refresh: true),
                          icon: busy
                              ? const NeatieLoading(
                                  label: 'Refreshing Home',
                                  size: 20,
                                )
                              : Badge(
                                  isLabelVisible: readyCount > 0,
                                  smallSize: 6,
                                  backgroundColor: NeatieDesign.accent,
                                  child: const Icon(Icons.refresh_rounded,
                                      size: 20),
                                ),
                        )
                      : null,
                ),
              if (widget.home && pendingHomeRefreshVersion != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Row(children: [
                    const Expanded(child: Text('Preparing recommendations…')),
                    TextButton(
                        onPressed: () => setState(() {
                              pendingHomeRefreshVersion = null;
                              pendingHomeRefreshToken = '';
                              homeAwaitingPreparation = false;
                              homePoll?.cancel();
                            }),
                        child: const Text('Cancel')),
                  ]),
                ),
              if (!widget.home) ...[
                Semantics(
                  label: 'Search songs',
                  child: TextField(
                    controller: query,
                    focusNode: _activeInputFocus,
                    onChanged: _queryChanged,
                    textInputAction: TextInputAction.search,
                    onSubmitted: (_) => load(manualSearch: true),
                    decoration: InputDecoration(
                      contentPadding: const EdgeInsets.symmetric(
                        horizontal: 20,
                        vertical: 24,
                      ),
                      prefixIcon: const Icon(Icons.search_rounded),
                      suffixIcon: IconButton(
                        tooltip: query.text.isEmpty ? 'Search' : 'Clear search',
                        onPressed: query.text.isEmpty
                            ? () => load(manualSearch: true)
                            : _clearSearchInput,
                        icon:
                            SearchFieldActionIcon(clear: query.text.isNotEmpty),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                if (suggestions.isNotEmpty &&
                    query.text.trim().isNotEmpty &&
                    (busy || displayedQuery != query.text.trim()))
                  EditorialSurface(
                    child: Material(
                      color: Colors.transparent,
                      child: Column(
                        children: [
                          for (final suggestion in suggestions)
                            _suggestionTile(suggestion),
                        ],
                      ),
                    ),
                  ),
                if (query.text.trim().isEmpty &&
                    _availableLanes.length > 1) ...[
                  const SectionHeading(title: 'Explore a mood'),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      for (final lane in _availableLanes)
                        ChoiceChip(
                          label: Text(
                            lane == 'all'
                                ? 'All'
                                : '${lane[0].toUpperCase()}${lane.substring(1)}',
                          ),
                          selected: selectedLane == lane,
                          onSelected: (_) => setState(() => homeLane = lane),
                        ),
                    ],
                  ),
                  if (selectedLane != 'all')
                    homeRow({
                      'kind': 'discoveries',
                      'item_type': 'track',
                      'title':
                          'Explore ${selectedLane[0].toUpperCase()}${selectedLane.substring(1)}',
                      'items': _laneItems(selectedLane, 'tracks'),
                    }),
                ],
                if (query.text.trim().isEmpty &&
                    history.entries.isNotEmpty) ...[
                  SectionHeading(
                    title: 'Recent searches',
                    action: TextButton(
                      onPressed: () => setState(() => history.clear()),
                      child: const Text('Clear'),
                    ),
                  ),
                  for (final entry in history.entries.take(8))
                    ListTile(
                      leading: entry['track'] is Map
                          ? HomeArtwork(
                              api: widget.api,
                              item: Map<String, dynamic>.from(entry['track']),
                              kind: 'track',
                              size: 40,
                            )
                          : const Icon(Icons.history),
                      title: Text(
                        entry['track'] is Map
                            ? '${entry['track']['title'] ?? entry['query']}'
                            : '${entry['query']}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      subtitle: entry['track'] is Map
                          ? Text(
                              '${entry['track']['artist'] ?? entry['track']['channel'] ?? entry['query']}',
                            )
                          : null,
                      onTap: () => _selectSuggestion({
                        'text': entry['query'],
                        'track': entry['track'],
                        'direct_play': entry['track'] != null,
                      }),
                      trailing: IconButton(
                        tooltip: 'Remove recent search',
                        icon: const Icon(Icons.close),
                        onPressed: () =>
                            setState(() => history.remove('${entry['query']}')),
                      ),
                    ),
                ],
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final type in ['all', ...labels.keys])
                      ChoiceChip(
                        label: Text(type == 'all' ? 'Top' : labels[type]!),
                        selected: filter == type,
                        onSelected: (_) => _selectFilter(type),
                      ),
                  ],
                ),
              ],
              if (busy && !widget.home)
                const Padding(
                  padding: EdgeInsets.only(top: 18),
                  child: NeatieLoading(
                    label: 'Finding your music',
                    showLabel: true,
                  ),
                ),
              if (error != null) ...[
                Text(error!),
                TextButton(
                  onPressed: () => load(refresh: true),
                  child: const Text('Retry'),
                ),
              ],
              if (correction != null) ...[
                Text('Showing results for ${correction['corrected']}'),
                TextButton(
                  onPressed: () {
                    query.text = '${correction['original']}';
                    unawaited(load(autoCorrect: false));
                  },
                  child: Text('Search instead for ${correction['original']}'),
                ),
              ],
              if (!widget.home && data['search_preference'] is Map)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Text('Based on your previous selection'),
                ),
              if (!widget.home &&
                  topItem != null &&
                  (filter == 'all' || filter == topSurface)) ...[
                _searchSpotlight(topSurface, topItem!, initial['tracks'] ?? []),
              ],
              if (widget.home) ...[
                HomeAlbumHero(
                  key: const ValueKey('home-album-hero'),
                  api: widget.api,
                  albums: heroAlbums,
                  active: homeActive && widget.motionActive,
                  onOpen: (album) => _openHomeItem('album', album),
                  onArtworkOpen: (album, rect) => open(
                    'album',
                    _homeDetailId(album, 'album'),
                    '${album['title'] ?? album['name'] ?? ''}',
                    seed: album,
                    origin: rect,
                  ),
                  onPlay: _playHomeAlbum,
                ),
                HomeLikesSection(
                    api: widget.api,
                    tracks: widget.likedTracks,
                    onOpen: () => _openHomeShelf({
                          'title': 'Your likes',
                          'item_type': 'track',
                          'items': widget.likedTracks
                        }),
                    onPlay: (index) => unawaited(widget.player
                        .startQueue(widget.likedTracks, index, 'Your likes')),
                    onShuffle: () {
                      final shuffled =
                          List<Map<String, dynamic>>.from(widget.likedTracks)
                            ..shuffle();
                      unawaited(
                          widget.player.startQueue(shuffled, 0, 'Your likes'));
                    }),
                if (!busy && error == null && rows.isEmpty)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 24),
                    child: Column(
                      children: [
                        Text(_homeEmptyMessage(), textAlign: TextAlign.center),
                        const SizedBox(height: 8),
                        TextButton(
                          onPressed: () => load(refresh: true),
                          child: const Text('Retry Home'),
                        ),
                      ],
                    ),
                  ),
                if (selectedLane == 'all') ...[
                  for (final row in rows)
                    if (row['kind'] != 'featured_new_albums' &&
                        (row['kind'] != 'recommended_albums' ||
                            rows.any(
                                (r) => r['kind'] == 'featured_new_albums')))
                      Builder(
                        builder: (_) => Padding(
                          padding: const EdgeInsets.only(bottom: 16),
                          child: homeRow(row),
                        ),
                      ),
                  if (recent.isNotEmpty &&
                      !rows.any((row) => row['kind'] == 'last_played'))
                    _trackStrip('Listening history', recent,
                        row: const {'kind': 'last_played'}),
                ] else ...[
                  homeRow({
                    'kind': 'discoveries',
                    'item_type': 'track',
                    'title': 'Discoveries',
                    'items': _laneItems(selectedLane, 'tracks'),
                  }),
                  if (_laneItems(selectedLane, 'albums').isNotEmpty)
                    homeRow({
                      'item_type': 'album',
                      'title': 'Albums',
                      'items': _laneItems(selectedLane, 'albums'),
                    }),
                  if (_laneItems(selectedLane, 'artists').isNotEmpty)
                    homeRow({
                      'item_type': 'artist',
                      'title': 'Artists to explore',
                      'items': _laneItems(selectedLane, 'artists'),
                    }),
                ],
                if (selectedLane == 'all' && (submitted || rows.isNotEmpty))
                  DiscoveryShelves(
                    key: ValueKey('discovery:${widget.api.userScopeId}'),
                    api: catalog,
                    player: widget.player,
                    active: homeActive,
                    region: widget.chartRegion,
                    onLike: widget.onLike,
                    isLiked: _isLiked,
                    onAddToPlaylist: widget.onAddToPlaylist,
                    onOpenAlbum: (entry) =>
                        open('album', entry.id, entry.title),
                  ),
                if (selectedLane == 'all' &&
                    frequent.isNotEmpty &&
                    !rows.any((row) => row['kind'] == 'frequently_listened'))
                  _trackStrip('Frequently listened', frequent,
                      row: const {'kind': 'frequently_listened'}),
              ] else ...[
                if (!submitted && !busy && !hasSearchResult)
                  Padding(
                    padding: const EdgeInsets.only(top: 36),
                    child: EditorialSurface(
                      child: Padding(
                        padding: const EdgeInsets.all(28),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            const Icon(
                              Icons.travel_explore_rounded,
                              color: NeatieDesign.accent,
                              size: 36,
                            ),
                            const SizedBox(height: 22),
                            Text(
                              'Every great listen starts somewhere.',
                              style: NeatieDesign.display(30),
                            ),
                            const SizedBox(height: 12),
                            const Text(
                              'Start with a song you love, an artist you miss, or an album you have been meaning to hear.',
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                if (submitted &&
                    !busy &&
                    error == null &&
                    !hasSearchResult) ...[
                  Text(
                    selectedPending && !enrichmentStopped
                        ? 'Still finding matches…'
                        : selectedExhausted ||
                                enrichmentStopped ||
                                (data['diagnostics'] as Map?)?['search_outcome'] ==
                                    'provider_unavailable'
                            ? 'Search could not finish. Try again.'
                            : 'No results found.',
                  ),
                  if (!selectedPending || enrichmentStopped)
                    Align(
                      alignment: Alignment.centerLeft,
                      child: TextButton.icon(
                        onPressed: () => load(refresh: true),
                        icon: const Icon(Icons.refresh_rounded),
                        label: const Text('Retry search'),
                      ),
                    ),
                ],
                for (final type in labels.keys)
                  if ((filter == 'all' || filter == type) &&
                      !(filter == 'all' &&
                          topItem != null &&
                          type == 'tracks') &&
                      (initial[type]?.isNotEmpty ?? false))
                    section(
                      labels[type]!,
                      type,
                      filter == 'all'
                          ? initial[type]!
                              .take(
                                type == 'tracks'
                                    ? 5
                                    : type == 'artists'
                                        ? 6
                                        : 4,
                              )
                              .toList()
                          : initial[type]!,
                    ),
                if (additionalArtistTracks.isNotEmpty &&
                    (filter == 'all' ||
                        filter == 'tracks' ||
                        filter == 'artists'))
                  section(
                    filter == 'artists'
                        ? (artistName.isEmpty
                            ? 'Works by this artist'
                            : 'Works by $artistName')
                        : (artistName.isEmpty
                            ? 'Top songs by this artist'
                            : 'Top songs by $artistName'),
                    'tracks',
                    filter == 'all'
                        ? additionalArtistTracks.take(3).toList()
                        : additionalArtistTracks,
                  ),
                if ((filter == 'all' || filter == 'artists') &&
                    maps(data['similar_artists']).isNotEmpty)
                  section(
                    'Similar artists',
                    'artists',
                    maps(data['similar_artists']),
                  ),
                // Separately fetched pages follow the current ranked first page.
                for (final extra in (filter == 'all' ? extras.take(3) : extras))
                  if ((filter == 'all' || filter == extra.type) &&
                      identity(extra.type, extra.item) != topKey &&
                      identity(extra.type, extra.item) != leadKey &&
                      identity(extra.type, extra.item) != albumKey)
                    itemTile(extra.type, extra.item),
                Padding(
                  padding: const EdgeInsets.only(top: 24),
                  child: Wrap(
                    spacing: 12,
                    runSpacing: 12,
                    children: [
                      for (final type in labels.keys)
                        if (filter == type &&
                            (pagination[type] as Map?)?['has_more'] == true)
                          OutlinedButton.icon(
                            onPressed: busy ? null : () => more(type),
                            icon: const Icon(Icons.arrow_forward_rounded,
                                size: 18),
                            label: Text(
                                'Load more ${labels[type]!.toLowerCase()}'),
                          ),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ));
  }

  String _homeEmptyMessage() {
    if (error != null) return error!;
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
    widget.player.removeListener(_playbackSelectionChanged);
    homeRetry?.cancel();
    _cancelSearchRequests();
    generation++;
    poll?.cancel();
    homePoll?.cancel();
    homePrepare?.cancel();
    searchDebounce?.cancel();
    query.dispose();
    _inputFocus.dispose();
    _searchScroll.dispose();
    super.dispose();
  }
}
