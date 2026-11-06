import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:math';
import 'package:flutter/foundation.dart'
    show defaultTargetPlatform, TargetPlatform;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:web/web.dart' as web;
import '../logic/track_metadata.dart';
import 'browser_audio.dart';
import 'music_api.dart';
import 'web_player.dart';
import 'web_auth.dart';
import 'web_library.dart';
import 'account_screen.dart';
import 'library_screen.dart';
import 'catalog_screen.dart';
import 'home_signals.dart';
import 'home_widgets.dart';
import 'guest_home_state.dart';
import 'catalog_api.dart';
import 'chart_region.dart';
import 'design_system.dart';
import 'playback_dock.dart';
import 'playback_diagnostics.dart';
import 'ui_performance.dart';
import 'artwork_image.dart';

const webBuild = '2026-11-06-artist-home';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  web.window.performance.mark('neatie:auth-start');
  final auth = await WebAuth.initialize();
  web.window.performance.mark('neatie:auth-ready');
  runApp(NeatieWebApp(auth: auth));
}

class NeatieWebApp extends StatelessWidget {
  final WebAuth auth;
  const NeatieWebApp({super.key, required this.auth});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Neatie',
        debugShowCheckedModeBanner: false,
        theme: NeatieDesign.theme,
        home: _PlayerPage(auth: auth),
      );
}

class _PlayerPage extends StatefulWidget {
  final WebAuth auth;
  const _PlayerPage({required this.auth});
  @override
  State<_PlayerPage> createState() => _PlayerPageState();
}

class _PlayerPageState extends State<_PlayerPage> {
  final _frameDiagnostics = GlobalKey<UiPerformanceProbeState>();
  final _imageDiagnostics = GlobalKey<ArtworkDiagnosticsState>();
  late final MusicApi api;
  late final CatalogApi _catalog = CatalogApi(api);
  late final BrowserAudio audio;
  late final WebPlayer player;
  final _playerExpanded = ValueNotifier<bool>(false);
  final _likesRevision = ValueNotifier<int>(0);
  final _searchInputFocus = FocusNode(debugLabel: 'Search input');
  int _searchFocusRevision = 0;
  late final String storageNamespace;
  late final ChartRegion chartRegion;
  late final JSFunction _lifecycle;
  late final JSFunction _network;
  late final StreamSubscription<String> _guestPlaybackEvents;
  WebLibrary? library;
  String scope = 'guest';
  late GuestHomeState _guestHome;
  late GuestHomeState _listeningHistory;
  final _historyTracker = PlaybackHistoryTracker();
  int _guestHomeRevision = 0;
  int _homeForegroundRevision = 0;
  bool _foreground = true;
  GuestHomeState? _historySyncing;
  Timer? _historyRetry;
  DateTime? _lastHistoryRefreshAt;
  bool _historyAgain = false;
  int _historyFailures = 0;
  int tab = 0;
  var _sidebarArtists = false;
  var _shortcutRevision = 0;
  List<Map<String, dynamic>> _followedArtists = const [];
  ({Map<String, dynamic> artist, int revision})? _artistShortcut;
  ({String id, int revision})? _playlistShortcut;
  static const names = ['Home', 'Search', 'Library'];
  static const icons = [
    Icons.home_outlined,
    Icons.search,
    Icons.library_music_outlined,
  ];

  @override
  void initState() {
    super.initState();
    _foreground = web.document.visibilityState == 'visible';
    const configured = String.fromEnvironment('AURALIS_PROXY_URL');
    storageNamespace =
        configured.isEmpty ? Uri.base.origin : Uri.parse(configured).toString();
    const sameOrigin = bool.fromEnvironment(
      'NEATIE_SAME_ORIGIN_API',
      defaultValue: true,
    );
    api = MusicApi(
      configured.isEmpty || sameOrigin
          ? Uri.parse(Uri.base.origin)
          : Uri.parse(configured),
      mediaBase: const String.fromEnvironment('NEATIE_MEDIA_ORIGIN').isEmpty
          ? null
          : Uri.parse(const String.fromEnvironment('NEATIE_MEDIA_ORIGIN')),
      storageBase:
          const String.fromEnvironment('NEATIE_AUDIO_STORAGE_ORIGIN').isEmpty
              ? null
              : Uri.parse(
                  const String.fromEnvironment('NEATIE_AUDIO_STORAGE_ORIGIN')),
    );
    api.catalogMediaHints = true;
    final format = Uri.base.queryParameters['audio_format'];
    api.preferredCachedVariant = format == 'opus'
        ? 'opus-v1'
        : format == 'aac' || defaultTargetPlatform == TargetPlatform.iOS
            ? 'aac-v1'
            : '';
    api.accessToken =
        () => widget.auth.client?.auth.currentSession?.accessToken;
    api.online = () => web.window.navigator.onLine;
    api.transportRoute =
        api.base.origin == Uri.base.origin ? 'same_origin' : 'cross_origin';
    chartRegion = ChartRegion(
      store: BrowserSessionStore('$storageNamespace::chart-region'),
      detect: detectChartCountry,
    );
    scope = widget.auth.userId ?? 'guest';
    _loadFollowedArtists();
    api.userScopeId = scope;
    _guestHome = _loadGuestHomeState(storageNamespace);
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    _listeningHistory = _loadListeningHistory();
    audio =
        BrowserAudio(artworkUrls: (track) => api.artworkUrls(track, 'track'));
    audio.setDiagnosticScope(storageNamespace, scope);
    audio.beforeDiagnosticCapture = () {
      _frameDiagnostics.currentState?.flush();
      _imageDiagnostics.currentState?.flush();
    };
    api.onDiagnostic = audio.record;
    player = WebPlayer(
      api,
      audio,
      BrowserSessionStore(storageNamespace, scope: scope),
    );
    _guestPlaybackEvents = audio.events.listen(_guestPlaybackEvent);
    audio.diagnosticContext = () => {'build': webBuild, ...player.diagnostics};
    audio.onReconcile = (reason) {
      player.reconcile();
      audio.record('reconciled', {'reason': reason});
    };
    audio.onPlay = () => unawaited(player.play());
    audio.onPause = player.pause;
    audio.onNext = () => unawaited(player.next());
    audio.onPrevious = () => unawaited(player.previous());
    audio.onSeek = player.seek;
    _lifecycle = ((web.Event event) {
      final visible =
          event.type != 'pagehide' && web.document.visibilityState == 'visible';
      if (visible) {
        audio.foreground();
      } else {
        audio.background();
      }
      audio.record('lifecycle', {'kind': event.type});
      player.lifecycle(event.type, visible: visible);
      if (visible) {
        if (mounted)
          setState(() {
            _foreground = true;
            _homeForegroundRevision++;
          });
        player.reconcile();
        unawaited(_hydrateHistory());
        if (tab == 2) unawaited(library?.refresh());
      } else {
        if (mounted) setState(() => _foreground = false);
        player.save();
      }
    }).toJS;
    web.window.addEventListener('pagehide', _lifecycle);
    web.window.addEventListener('pageshow', _lifecycle);
    web.document.addEventListener('visibilitychange', _lifecycle);
    _network = ((web.Event event) {
      audio.record('network-state', {
        'kind': event.type,
        'online': web.window.navigator.onLine,
      });
      if (event.type == 'online' && mounted && _foreground) {
        setState(() => _homeForegroundRevision++);
        unawaited(_hydrateHistory(force: true));
      }
    }).toJS;
    web.window.addEventListener('online', _network);
    web.window.addEventListener('offline', _network);
    widget.auth.addListener(_accountChanged);
    if (widget.auth.passwordRecovery) tab = 2;
    _openLibrary();
    unawaited(_hydrateHistory());
  }

  void _openLibrary() {
    final accountId = widget.auth.userId;
    library?.removeListener(_libraryChanged);
    library?.dispose();
    library = accountId == null || widget.auth.client == null
        ? null
        : WebLibrary(
            widget.auth.client!,
            accountId,
            accessToken: api.accessToken,
            scopeIsCurrent: () => widget.auth.userId == accountId,
            playlistWritesEnabled: const bool.fromEnvironment(
              'WEB_PLAYLIST_WRITES',
            ),
          );
    library?.addListener(_libraryChanged);
    _likesRevision.value++;
    unawaited(library?.refresh());
  }

  void _libraryChanged() {
    _likesRevision.value++;
    api.preloadCachedMedia(library?.likes ?? const []);
    if (mounted) setState(() => _guestHomeRevision++);
  }

  Future<void> _addToPlaylist(Map<String, dynamic> track) =>
      showSongPlaylistPicker(context, library, player, track,
          onSignIn: _showAccount);

  void _accountChanged() {
    final next = widget.auth.userId ?? 'guest';
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    if (next != scope) {
      scope = next;
      _loadFollowedArtists();
      _artistShortcut = null;
      _playlistShortcut = null;
      api.userScopeId = next;
      player.changeAccount(BrowserSessionStore(storageNamespace, scope: next));
      audio.setDiagnosticScope(storageNamespace, next);
      _historyTracker.reset();
      _listeningHistory = _loadListeningHistory();
      _historyRetry?.cancel();
      _lastHistoryRefreshAt = null;
      _historyFailures = 0;
      _historyAgain = false;
      _guestHomeRevision++;
      _openLibrary();
      unawaited(_hydrateHistory());
    }
    if (widget.auth.passwordRecovery) tab = 2;
    if (mounted) setState(() {});
  }

  void _navigate(int value) {
    if (value == 4) {
      _navigate(2);
      _showAccount();
      return;
    }
    setState(() {
      tab = value;
      if (value == 1) _searchFocusRevision++;
    });
    if (value == 1) _searchInputFocus.requestFocus();
    if (value == 0) unawaited(_hydrateHistory());
    if (value == 2) unawaited(library?.refresh());
  }

  BrowserSessionStore get _artistStore =>
      BrowserSessionStore('$storageNamespace::followed-artists', scope: scope);

  void _loadFollowedArtists() {
    try {
      _followedArtists = scope == 'guest'
          ? const []
          : (jsonDecode(_artistStore.read() ?? '[]') as List)
              .whereType<Map<String, dynamic>>().take(100).toList();
    } catch (_) {
      _followedArtists = const [];
    }
  }

  void _toggleArtistFollow(Map<String, dynamic> artist) {
    if (scope == 'guest' || artist['id'] is! String) return;
    final id = artist['id'];
    final exists = _followedArtists.any((item) => item['id'] == id);
    setState(() => _followedArtists = [
      if (!exists) artist,
      ..._followedArtists.where((item) => item['id'] != id).take(exists ? 100 : 99),
    ]);
    try {
      _artistStore.write(jsonEncode(_followedArtists));
    } catch (_) {/* Keep the current view usable if browser storage is blocked. */}
  }

  void _showAccount() {
    showDialog<void>(
      context: context,
      builder: (context) => Dialog(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520, maxHeight: 660),
          child: Stack(
            children: [
              AccountScreen(auth: widget.auth),
              Positioned(
                right: 8,
                top: 8,
                child: IconButton(
                  tooltip: 'Close',
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void showDiagnostics() async {
    audio.record('user-report-open', {'build': webBuild});
    var report = await audio.refreshDiagnosticReport(current: false);
    if (!mounted) return;
    var reportLoading = false;
    showDialog<void>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, update) => AlertDialog(
          title: const Text('Playback diagnostics'),
          content: SizedBox(
            width: 460,
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  SelectableText(PlaybackDiagnostics.summary(report)),
                  const SizedBox(height: 14),
                  const Text(
                    'The session file contains every event in the latest listening run, including reloads and failures. Older runs remain available through Full history. Account IDs and tokens are excluded.',
                  ),
                  Text(
                    'Archived events: ${(report['archive'] as Map?)?['records'] ?? 0}',
                  ),
                  if ((report['archive'] as Map?)?['complete'] == false)
                    const Text(
                      'Some archive data could not be saved or read. Keep this page open and export now.',
                    ),
                  TextButton.icon(
                    onPressed: () {
                      player.pause();
                      web.window.open(
                        '/_media-comparison/',
                        '_blank',
                        'noopener',
                      );
                    },
                    icon: const Icon(Icons.compare_arrows),
                    label: const Text('PC / iPhone comparison'),
                  ),
                  TextButton.icon(
                    onPressed: reportLoading
                        ? null
                        : () async {
                            update(() => reportLoading = true);
                            final next = await audio.refreshDiagnosticReport();
                            if (context.mounted)
                              update(() {
                                report = next;
                                reportLoading = false;
                              });
                          },
                    icon: const Icon(Icons.refresh),
                    label: const Text('Capture current state'),
                  ),
                  TextButton.icon(
                    onPressed: reportLoading
                        ? null
                        : () async {
                            update(() => reportLoading = true);
                            audio.markUnexpectedSilence();
                            final next = await audio.refreshDiagnosticReport();
                            if (context.mounted)
                              update(() {
                                report = next;
                                reportLoading = false;
                              });
                          },
                    icon: const Icon(Icons.volume_off_outlined),
                    label: const Text('Mark unexpected silence'),
                  ),
                  ExpansionTile(
                    title: const Text('Event details'),
                    children: [
                      SelectableText(
                        const JsonEncoder.withIndent('  ').convert({
                          for (final entry in report.entries)
                            if (entry.key != 'archive') entry.key: entry.value,
                        }),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          actions: [
            if (audio.canShareReport(report))
              TextButton.icon(
                onPressed: reportLoading
                    ? null
                    : () async {
                        try {
                          await audio.shareReport(report);
                        } catch (_) {
                          if (context.mounted)
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(
                                content: Text(
                                  'Report was not shared. You can use Download instead.',
                                ),
                              ),
                            );
                        }
                      },
                icon: const Icon(Icons.ios_share),
                label: const Text('Share report'),
              ),
            TextButton.icon(
              onPressed:
                  reportLoading ? null : () => audio.downloadReport(report),
              icon: const Icon(Icons.download),
              label: const Text('Download session report'),
            ),
            TextButton(
              onPressed: reportLoading
                  ? null
                  : () async {
                      update(() => reportLoading = true);
                      final history = await audio.refreshDiagnosticReport(
                          fullHistory: true);
                      if (!context.mounted) return;
                      audio.downloadReport(history);
                      update(() => reportLoading = false);
                    },
              child: const Text('Download full history'),
            ),
            TextButton(
              onPressed: () =>
                  audio.downloadReport(audio.diagnosticReport(full: false)),
              child: const Text('Download compact report'),
            ),
            TextButton(
              onPressed: () => Clipboard.setData(
                ClipboardData(text: PlaybackDiagnostics.summary(report)),
              ),
              child: const Text('Copy summary'),
            ),
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Close'),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _like(Map<String, dynamic> track) async {
    final repo = library;
    if (repo == null) {
      _navigate(4);
      return;
    }
    final liked = repo.likes.any(
      (t) => extractTrackId(t) == extractTrackId(track),
    );
    try {
      await repo.setLiked(track, !liked);
    } catch (_) {
      if (mounted && identical(library, repo))
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not update liked songs. Retry.')),
        );
    }
  }

  Map<String, dynamic> _homeSignals() => {
        ...buildWebHomeSignals(
          likedTracks: library?.likes ?? const [],
          playlists: library?.playlists ?? const [],
          libraryKnown:
              library == null || (!library!.loading && library!.error == null),
          allowColdStart: widget.auth.userId == null,
          playedSnapshots: _listeningHistory.snapshots,
          frequentSnapshots: _listeningHistory.frequentSnapshots,
        ),
        'listening_history_available':
            scope == 'guest' || _listeningHistory.hasHistorySnapshot,
      };

  String get _listeningStorageKey => scope == 'guest'
      ? 'neatie:guest-home:${Uri.parse(storageNamespace).origin}'
      : 'neatie:$scope:home-history:${Uri.parse(storageNamespace).origin}';

  GuestHomeState _loadListeningHistory() {
    if (scope == 'guest') return _guestHome;
    try {
      final saved = web.window.localStorage.getItem(_listeningStorageKey);
      if (saved != null)
        return GuestHomeState.decode(saved, expectedScope: scope);
    } catch (_) {
      /* The account still has an isolated in-memory history. */
    }
    return GuestHomeState(scopeId: scope);
  }

  Future<void> _hydrateHistory({bool force = false}) async {
    if (scope == 'guest') return;
    final expectedScope = scope;
    final history = _listeningHistory;
    if (identical(_historySyncing, history)) {
      if (force) _historyAgain = true;
      return;
    }
    if (!force &&
        history.pendingPlays.isEmpty &&
        _lastHistoryRefreshAt != null &&
        DateTime.now().difference(_lastHistoryRefreshAt!) <
            const Duration(seconds: 15)) return;
    _historyRetry?.cancel();
    _historySyncing = history;
    var failed = false;
    bool current() =>
        mounted &&
        scope == expectedScope &&
        identical(history, _listeningHistory);
    try {
      final catalog = CatalogApi(api);
      for (final event in history.pendingPlays.take(16)) {
        if (!current()) return;
        try {
          await catalog.recordPlay(
            Map<String, dynamic>.from(event['track'] as Map),
            scope: expectedScope,
            occurredAt: (event['occurred_at'] as num).toDouble(),
            playKey: '${event['key']}',
          );
        } catch (_) {
          failed = true;
          break;
        }
      }
      if (!current()) return;
      // Also confirm writes whose HTTP acknowledgement was lost. The history
      // response and its acknowledgements come from the same event snapshot.
      final payload = await catalog.history(
        pendingPlayKeys: history.pendingPlayKeys,
      );
      if (!current() || payload['user_scope_id'] != expectedScope) return;
      if (payload['status'] != 'success')
        throw StateError('History is unavailable.');
      history.reconcileAccount(
        recent: MusicApi.tracks(payload['recent_track_snapshots']),
        frequent: MusicApi.tracks(payload['frequent_track_snapshots']),
        acknowledgedPlayKeys: (payload['acknowledged_play_keys'] as List? ?? [])
            .whereType<String>(),
      );
      _lastHistoryRefreshAt = DateTime.now();
      _historyFailures = 0;
      _saveGuestHomeState();
      setState(() => _guestHomeRevision++);
    } catch (_) {
      failed = true;
    } finally {
      if (identical(_historySyncing, history)) _historySyncing = null;
      if (current()) {
        if (failed) _historyFailures++;
        final again = _historyAgain;
        _historyAgain = false;
        if (failed || history.pendingPlays.isNotEmpty || again) {
          audio.record('history-sync', {
            'failed': failed,
            'kind': history.pendingPlays.isEmpty ? 'refresh' : 'pending',
          });
          _historyRetry = Timer(
            Duration(seconds: min(30, 2 << min(_historyFailures, 4))),
            () {
              if (current() && _foreground)
                unawaited(_hydrateHistory(force: true));
            },
          );
        }
      }
    }
  }

  GuestHomeState _loadGuestHomeState(String backend) {
    final key = 'neatie:guest-home:${Uri.parse(backend).origin}';
    try {
      final saved = web.window.localStorage.getItem(key);
      if (saved != null) return GuestHomeState.decode(saved);
    } catch (_) {
      // Private browsing may deny storage; this session still gets an isolated scope.
    }
    final fresh = GuestHomeState(
      scopeId: GuestHomeState.newScopeId(Random.secure()),
    );
    try {
      web.window.localStorage.setItem(key, fresh.encode());
    } catch (_) {}
    return fresh;
  }

  void _saveGuestHomeState() {
    try {
      web.window.localStorage.setItem(
        _listeningStorageKey,
        _listeningHistory.encode(),
      );
    } catch (_) {}
  }

  void _guestPlaybackEvent(String event) {
    if (!_historyTracker.observe(
      event,
      operation: player.listeningEpoch,
      trackId: extractTrackId(player.current),
      loadedTrackId: audio.loadedTrackId,
      position: audio.position,
      paused: audio.paused,
    )) return;
    final track = player.current;
    if (track == null) return;
    final now = DateTime.now();
    if (scope == 'guest') {
      _listeningHistory.recordPlayedSnapshot(track, at: now);
    } else {
      try {
        _listeningHistory.queueAccountPlay(track, at: now);
      } catch (_) {
        audio.record('history-sync', {'failed': true, 'kind': 'backlog_full'});
        if (mounted)
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('Listening history needs to reconnect and sync.'),
            ),
          );
      }
    }
    _guestHomeRevision++;
    _saveGuestHomeState();
    if (scope != 'guest') {
      unawaited(_hydrateHistory(force: true));
    }
    if (mounted) setState(() {});
  }

  Widget _sidebar({required bool desktop}) => Container(
        width: 218,
        decoration: const BoxDecoration(
          color: NeatieDesign.surface,
          border: Border(right: BorderSide(color: NeatieDesign.line)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 30, 20, 18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
                padding: EdgeInsets.only(left: 8), child: NeatieBrand()),
            const SizedBox(height: 46),
            NeatieNavigation(
              labels: names,
              icons: icons,
              selected: tab,
              onSelect: _navigate,
              vertical: true,
            ),
            if (scope == 'guest' || !desktop)
              const Spacer()
            else ...[
              const SizedBox(height: 20),
              Wrap(spacing: 6, children: [
                ChoiceChip(label: const Text('Playlists'), selected: !_sidebarArtists,
                  onSelected: (_) => setState(() => _sidebarArtists = false)),
                ChoiceChip(label: const Text('Artists'), selected: _sidebarArtists,
                  onSelected: (_) => setState(() => _sidebarArtists = true)),
              ]),
              const SizedBox(height: 8),
              Expanded(child: _sidebarCollection()),
            ],
            const Divider(),
            TextButton.icon(
              onPressed: showDiagnostics,
              icon: const Icon(Icons.tune_rounded, size: 16),
              label: const Text('Diagnostics'),
            ),
            const SizedBox(height: 8),
            const Text(
              'Build $webBuild',
              style: TextStyle(fontSize: 9, color: NeatieDesign.muted),
            ),
          ],
        ),
      );

  Widget _sidebarCollection() {
    final rows = _sidebarArtists ? _followedArtists : library?.playlists ?? const [];
    if (rows.isEmpty) {
      return Padding(
        padding: const EdgeInsets.only(top: 10),
        child: Text(_sidebarArtists ? 'Follow artists to see them here.' : 'Your playlists will appear here.',
            style: const TextStyle(fontSize: 11, color: NeatieDesign.muted)),
      );
    }
    return ListView.builder(
      itemCount: rows.length,
      itemBuilder: (_, index) {
        final row = rows[index];
        final id = '${row['id']}';
        final name = '${row['name'] ?? row['title'] ?? ''}';
        final tracks = row['tracks'] as List? ?? const [];
        final art = _sidebarArtists ? row : tracks.firstOrNull;
        return ListTile(
          dense: true,
          contentPadding: EdgeInsets.zero,
          minLeadingWidth: 32,
          horizontalTitleGap: 8,
          leading: ClipRRect(
            borderRadius: BorderRadius.circular(_sidebarArtists ? 18 : 6),
            child: ArtworkImage(
              width: 32, height: 32,
              urls: art is Map<String, dynamic>
                ? api.artworkUrls(art, _sidebarArtists ? 'artist' : 'track') : const [],
              icon: _sidebarArtists ? Icons.person_outline : Icons.playlist_play,
            ),
          ),
          title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis),
          onTap: () => setState(() {
            if (_sidebarArtists) {
              tab = 1;
              _artistShortcut = (artist: row, revision: ++_shortcutRevision);
            } else {
              tab = 2;
              _playlistShortcut = (id: id, revision: ++_shortcutRevision);
            }
          }),
        );
      },
    );
  }

  Widget _header(bool wide) => Padding(
        padding: EdgeInsets.symmetric(horizontal: wide ? 36 : 20, vertical: 14),
        child: Row(
          children: [
            if (!wide) const NeatieBrand(),
            const Spacer(),
            if (!wide)
              IconButton(
                tooltip: 'Diagnostics',
                onPressed: showDiagnostics,
                icon: const Icon(Icons.tune_rounded, size: 19),
              ),
            IconButton(
              tooltip: widget.auth.email ?? 'Account',
              onPressed: () => _navigate(2),
              style: IconButton.styleFrom(backgroundColor: NeatieDesign.raised),
              icon: widget.auth.avatarUrl == null
                  ? const Icon(Icons.person_outline_rounded, size: 20)
                  : CircleAvatar(
                      radius: 14,
                      foregroundImage: NetworkImage(widget.auth.avatarUrl!),
                      onForegroundImageError: (_, __) {},
                      child: const Icon(Icons.person_outline_rounded, size: 18),
                    ),
            ),
          ],
        ),
      );

  @override
  Widget build(BuildContext context) {
    final wide = MediaQuery.sizeOf(context).width >= 850;
    final desktop = defaultTargetPlatform != TargetPlatform.iOS &&
        defaultTargetPlatform != TargetPlatform.android;
    final pageChildren = <Widget>[
      if (scope == 'guest')
        GuestHome(onSignIn: _showAccount, onSearch: () => _navigate(1))
      else
        CatalogScreen(
          key: ValueKey('home:$scope'),
          api: api,
          catalogApi: _catalog,
          home: true,
          homeStore: BrowserSessionStore('$storageNamespace::home-feed', scope: scope),
          motionActive: !_playerExpanded.value,
          chartRegion: chartRegion,
          active: tab == 0,
          player: player,
          onLike: _like,
          onAddToPlaylist: _addToPlaylist,
          homeSignals: _homeSignals,
          onHomePainted: () => web.window.performance.mark('neatie:home-painted'),
          homeSignalRevision: _guestHomeRevision,
          foreground: _foreground,
          foregroundRevision: _homeForegroundRevision,
          likedTracks: library?.likes ?? const [],
          followedArtists: _followedArtists,
          onFollowArtist: _toggleArtistFollow,
          homeSignalsReady: library == null || !library!.loading,
        ),
      CatalogScreen(
        key: ValueKey('search:$scope'),
        searchFocus: _searchInputFocus,
        searchFocusRevision: _searchFocusRevision,
        api: api,
        catalogApi: _catalog,
        home: false,
        active: tab == 1,
        historyStore: BrowserSessionStore(
          '$storageNamespace::search-history',
          scope: scope == 'guest' ? _guestHome.scopeId : scope,
        ),
        recentQueries: library?.recentSearchQueries,
        homeSignals: _homeSignals,
        player: player,
        likedTracks: library?.likes ?? const [],
        artistShortcut: _artistShortcut,
        followedArtists: _followedArtists,
        onFollowArtist: scope == 'guest' ? null : _toggleArtistFollow,
        onLike: _like,
        onAddToPlaylist: _addToPlaylist,
      ),
      LibraryScreen(
        auth: widget.auth,
        library: library,
        player: player,
        onSignIn: _showAccount,
        playlistShortcut: _playlistShortcut,
      ),
    ];
    final pages = IndexedStack(
      index: tab,
      children: [
        for (var i = 0; i < pageChildren.length; i++)
          PageEntrance(
            key: ValueKey('page:$scope:$i'),
            active: tab == i,
            child: pageChildren[i],
          ),
      ],
    );
    return UiPerformanceProbe(
        key: _frameDiagnostics,
        surface: _playerExpanded.value ? 'player' : names[tab].toLowerCase(),
        enabled: _foreground,
        onSummary: (summary) => audio.record('ui-frame-summary', summary),
        child: ArtworkDiagnosticsScope(
            key: _imageDiagnostics,
            onSummary: (summary) => audio.record('image-load-summary', summary),
            child: Scaffold(
              body: SafeArea(
                bottom: false,
                child: Row(
                  children: [
                    if (wide) _sidebar(desktop: desktop),
                    Expanded(
                      child: Column(
                        children: [
                          _header(wide),
                          Expanded(
                              child: TickerMode(
                                  enabled: !_playerExpanded.value,
                                  child: pages)),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              bottomNavigationBar: SafeArea(
                top: false,
                child: AnimatedBuilder(
                  animation: Listenable.merge([player, _playerExpanded]),
                  child: !wide
                      ? NeatieNavigation(
                          labels: names,
                          icons: icons,
                          selected: tab,
                          onSelect: _navigate,
                        )
                      : null,
                  builder: (context, navigation) => Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (player.current != null)
                        IgnorePointer(
                            ignoring: _playerExpanded.value,
                            child: AnimatedOpacity(
                                opacity: _playerExpanded.value ? 0 : 1,
                                duration:
                                    MediaQuery.disableAnimationsOf(context)
                                        ? Duration.zero
                                        : const Duration(milliseconds: 160),
                                child: PlaybackDock(
                                  onExpandedChanged: (value) {
                                    if (mounted)
                                      setState(
                                          () => _playerExpanded.value = value);
                                  },
                                  player: player,
                                  onQueueLike: _like,
                                  onAddToPlaylist: _addToPlaylist,
                                  likesListenable: _likesRevision,
                                  isLiked: (track) =>
                                      library?.likes.any(
                                        (item) =>
                                            extractTrackId(item) ==
                                            extractTrackId(track),
                                      ) ==
                                      true,
                                  audio: audio,
                                  wide: wide,
                                  liked: library?.likes.any(
                                        (t) =>
                                            extractTrackId(t) ==
                                            extractTrackId(player.current),
                                      ) ==
                                      true,
                                  onLike: () =>
                                      unawaited(_like(player.current!)),
                                  onRepeat: () => setState(
                                      () => audio.repeatOne = !audio.repeatOne),
                                ))),
                      if (navigation != null) navigation,
                    ],
                  ),
                ),
              ),
            )));
  }

  @override
  void dispose() {
    _searchInputFocus.dispose();
    _likesRevision.dispose();
    _playerExpanded.dispose();
    _historyRetry?.cancel();
    web.window.removeEventListener('online', _network);
    web.window.removeEventListener('offline', _network);
    widget.auth.removeListener(_accountChanged);
    library?.removeListener(_libraryChanged);
    library?.dispose();
    web.window.removeEventListener('pagehide', _lifecycle);
    web.window.removeEventListener('pageshow', _lifecycle);
    web.document.removeEventListener('visibilitychange', _lifecycle);
    _guestPlaybackEvents.cancel();
    player.dispose();
    chartRegion.dispose();
    api.client.close();
    super.dispose();
  }
}
