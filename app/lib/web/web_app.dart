import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:web/web.dart' as web;
import '../logic/track_metadata.dart';
import 'browser_audio.dart';
import 'music_api.dart';
import 'web_player.dart';
import 'queue_screen.dart';
import 'web_auth.dart';
import 'web_library.dart';
import 'account_screen.dart';
import 'library_screen.dart';
import 'catalog_screen.dart';
import 'home_signals.dart';
import 'guest_home_state.dart';
import 'catalog_api.dart';
import 'chart_region.dart';
import 'design_system.dart';
import 'playback_dock.dart';
import 'playback_diagnostics.dart';

const webBuild = '2026-10-15-stable-media';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final auth = await WebAuth.initialize();
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
      home: _PlayerPage(auth: auth));
}

class _PlayerPage extends StatefulWidget {
  final WebAuth auth;
  const _PlayerPage({required this.auth});
  @override
  State<_PlayerPage> createState() => _PlayerPageState();
}

class _PlayerPageState extends State<_PlayerPage> {
  late final MusicApi api;
  late final BrowserAudio audio;
  late final WebPlayer player;
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
  static const names = ['Home', 'Search', 'Library', 'Queue'];
  static const icons = [
    Icons.home_outlined,
    Icons.search,
    Icons.library_music_outlined,
    Icons.queue_music
  ];

  @override
  void initState() {
    super.initState();
    _foreground = web.document.visibilityState == 'visible';
    const configured = String.fromEnvironment('AURALIS_PROXY_URL');
    storageNamespace =
        configured.isEmpty ? Uri.base.origin : Uri.parse(configured).toString();
    const sameOrigin =
        bool.fromEnvironment('NEATIE_SAME_ORIGIN_API', defaultValue: true);
    api = MusicApi(configured.isEmpty || sameOrigin
        ? Uri.parse(Uri.base.origin)
        : Uri.parse(configured));
    api.accessToken =
        () => widget.auth.client?.auth.currentSession?.accessToken;
    api.online = () => web.window.navigator.onLine;
    api.transportRoute =
        api.base.origin == Uri.base.origin ? 'same_origin' : 'cross_origin';
    chartRegion = ChartRegion(
        store: BrowserSessionStore('$storageNamespace::chart-region'),
        detect: detectChartCountry);
    scope = widget.auth.userId ?? 'guest';
    api.userScopeId = scope;
    _guestHome = _loadGuestHomeState(storageNamespace);
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    _listeningHistory = _loadListeningHistory();
    audio = BrowserAudio();
    audio.setDiagnosticScope(storageNamespace, scope);
    api.onDiagnostic = audio.record;
    player = WebPlayer(
        api, audio, BrowserSessionStore(storageNamespace, scope: scope));
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
      audio.record('network-state',
          {'kind': event.type, 'online': web.window.navigator.onLine});
      if (event.type == 'online' && mounted && _foreground) {
        setState(() => _homeForegroundRevision++);
        unawaited(_hydrateHistory(force: true));
      }
    }).toJS;
    web.window.addEventListener('online', _network);
    web.window.addEventListener('offline', _network);
    widget.auth.addListener(_accountChanged);
    if (widget.auth.passwordRecovery) {
      tab = 2;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _releaseWelcome();
      });
    }
    _openLibrary();
    unawaited(_hydrateHistory());
  }

  void _openLibrary() {
    final accountId = widget.auth.userId;
    library?.removeListener(_libraryChanged);
    library?.dispose();
    library = accountId == null || widget.auth.client == null
        ? null
        : WebLibrary(widget.auth.client!, accountId,
            accessToken: api.accessToken,
            scopeIsCurrent: () => widget.auth.userId == accountId,
            playlistWritesEnabled:
                const bool.fromEnvironment('WEB_PLAYLIST_WRITES'));
    library?.addListener(_libraryChanged);
    unawaited(library?.refresh());
  }

  void _libraryChanged() {
    if (mounted) setState(() => _guestHomeRevision++);
  }

  void _accountChanged() {
    final next = widget.auth.userId ?? 'guest';
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    if (next != scope) {
      scope = next;
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
    if (value != 0) _releaseWelcome();
    setState(() => tab = value);
    if (value == 0) unawaited(_hydrateHistory());
    if (value == 2) unawaited(library?.refresh());
  }

  void _showAccount() {
    _releaseWelcome();
    showDialog<void>(
        context: context,
        builder: (context) => Dialog(
            child: ConstrainedBox(
                constraints:
                    const BoxConstraints(maxWidth: 520, maxHeight: 660),
                child: Stack(children: [
                  AccountScreen(auth: widget.auth),
                  Positioned(
                      right: 8,
                      top: 8,
                      child: IconButton(
                          tooltip: 'Close',
                          onPressed: () => Navigator.pop(context),
                          icon: const Icon(Icons.close))),
                ]))));
  }

  void _releaseWelcome() =>
      web.window.dispatchEvent(web.Event('neatie-home-painted'));

  void showDiagnostics() {
    audio.record('user-report-open', {'build': webBuild});
    var report = audio.diagnosticReport();
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
                              SelectableText(
                                  PlaybackDiagnostics.summary(report)),
                              const SizedBox(height: 14),
                              const Text(
                                  'Share the small report file here. It contains device details and playback IDs, without account IDs or tokens.'),
                              TextButton.icon(
                                  onPressed: () => update(() => report =
                                      audio.diagnosticReport(current: true)),
                                  icon: const Icon(Icons.refresh),
                                  label: const Text('Capture current state')),
                              ExpansionTile(
                                  title: const Text('Event details'),
                                  children: [
                                    SelectableText(
                                        const JsonEncoder.withIndent('  ')
                                            .convert(report)),
                                  ]),
                            ]))),
                    actions: [
                      if (audio.canShareReport(report))
                        TextButton.icon(
                            onPressed: () async {
                              try {
                                await audio.shareReport(report);
                              } catch (_) {
                                if (context.mounted)
                                  ScaffoldMessenger.of(context).showSnackBar(
                                      const SnackBar(
                                          content: Text(
                                              'Report was not shared. You can use Download instead.')));
                              }
                            },
                            icon: const Icon(Icons.ios_share),
                            label: const Text('Share report')),
                      TextButton.icon(
                          onPressed: () => audio.downloadReport(report),
                          icon: const Icon(Icons.download),
                          label: const Text('Download report')),
                      TextButton(
                          onPressed: () => Clipboard.setData(ClipboardData(
                              text: PlaybackDiagnostics.summary(report))),
                          child: const Text('Copy summary')),
                      TextButton(
                          onPressed: () => Navigator.pop(context),
                          child: const Text('Close'))
                    ])));
  }

  Future<void> _like(Map<String, dynamic> track) async {
    final repo = library;
    if (repo == null) {
      _navigate(4);
      return;
    }
    final liked =
        repo.likes.any((t) => extractTrackId(t) == extractTrackId(track));
    try {
      await repo.setLiked(track, !liked);
    } catch (_) {
      if (mounted)
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not update liked songs. Retry.')));
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
    } catch (_) {/* The account still has an isolated in-memory history. */}
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
              playKey: '${event['key']}');
        } catch (_) {
          failed = true;
          break;
        }
      }
      if (!current()) return;
      // Also confirm writes whose HTTP acknowledgement was lost. The history
      // response and its acknowledgements come from the same event snapshot.
      final payload =
          await catalog.history(pendingPlayKeys: history.pendingPlayKeys);
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
            'kind': history.pendingPlays.isEmpty ? 'refresh' : 'pending'
          });
          _historyRetry = Timer(
              Duration(seconds: min(30, 2 << min(_historyFailures, 4))), () {
            if (current() && _foreground)
              unawaited(_hydrateHistory(force: true));
          });
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
    final fresh =
        GuestHomeState(scopeId: GuestHomeState.newScopeId(Random.secure()));
    try {
      web.window.localStorage.setItem(key, fresh.encode());
    } catch (_) {}
    return fresh;
  }

  void _saveGuestHomeState() {
    try {
      web.window.localStorage
          .setItem(_listeningStorageKey, _listeningHistory.encode());
    } catch (_) {}
  }

  void _guestPlaybackEvent(String event) {
    if (!_historyTracker.observe(event,
        operation: player.listeningEpoch,
        trackId: extractTrackId(player.current),
        loadedTrackId: audio.loadedTrackId,
        position: audio.position,
        paused: audio.paused)) return;
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
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
              content: Text('Listening history needs to reconnect and sync.')));
      }
    }
    _guestHomeRevision++;
    _saveGuestHomeState();
    if (scope != 'guest') {
      unawaited(_hydrateHistory(force: true));
    } else {
      unawaited(CatalogApi(api)
          .recordPlay(track,
              scope: api.homeRequestScopeId,
              occurredAt: now.millisecondsSinceEpoch / 1000)
          .catchError((_) {
        audio.record('history-sync', {'failed': true, 'kind': 'guest_signal'});
      }));
    }
    if (mounted) setState(() {});
  }

  Widget _queue() => QueueScreen(
      key: ValueKey('queue:$scope'),
      player: player,
      active: tab == 3 && _foreground,
      onLike: _like,
      isLiked: (track) =>
          library?.likes
              .any((liked) => extractTrackId(liked) == extractTrackId(track)) ==
          true);

  Widget _sidebar() => Container(
      width: 218,
      decoration: const BoxDecoration(
          color: NeatieDesign.surface,
          border: Border(right: BorderSide(color: NeatieDesign.line))),
      padding: const EdgeInsets.fromLTRB(20, 30, 20, 18),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Padding(padding: EdgeInsets.only(left: 8), child: NeatieBrand()),
        const SizedBox(height: 46),
        Padding(
            padding: const EdgeInsets.only(left: 12, bottom: 14),
            child: Text('YOUR LISTENING ROOM', style: NeatieDesign.eyebrow)),
        NeatieNavigation(
            labels: names,
            icons: icons,
            selected: tab,
            onSelect: _navigate,
            vertical: true),
        const Spacer(),
        const Divider(),
        TextButton.icon(
            onPressed: showDiagnostics,
            icon: const Icon(Icons.tune_rounded, size: 16),
            label: const Text('Diagnostics')),
        const SizedBox(height: 8),
        const Text('Build $webBuild',
            style: TextStyle(fontSize: 9, color: NeatieDesign.muted)),
      ]));

  Widget _header(bool wide) => Padding(
      padding: EdgeInsets.symmetric(horizontal: wide ? 36 : 20, vertical: 14),
      child: Row(children: [
        if (!wide)
          const NeatieBrand()
        else
          Text(names[tab].toUpperCase(), style: NeatieDesign.eyebrow),
        const Spacer(),
        if (wide && tab != 1)
          TextButton.icon(
              onPressed: () => _navigate(1),
              icon: const Icon(Icons.search, size: 18),
              label: const Text('Find a song')),
        if (!wide)
          IconButton(
              tooltip: 'Diagnostics',
              onPressed: showDiagnostics,
              icon: const Icon(Icons.tune_rounded, size: 19)),
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
                    child: const Icon(Icons.person_outline_rounded, size: 18))),
      ]));

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
      animation: player,
      child: LibraryScreen(
          auth: widget.auth,
          library: library,
          player: player,
          onSignIn: _showAccount),
      builder: (context, libraryPage) {
        final wide = MediaQuery.sizeOf(context).width >= 850;
        final pageChildren = <Widget>[
          CatalogScreen(
              key: ValueKey('home:$scope'),
              api: api,
              home: true,
              chartRegion: chartRegion,
              active: tab == 0,
              player: player,
              onLike: _like,
              homeSignals: _homeSignals,
              onHomePainted: _releaseWelcome,
              homeSignalRevision: _guestHomeRevision,
              foreground: _foreground,
              foregroundRevision: _homeForegroundRevision,
              likedTracks: library?.likes ?? const [],
              homeSignalsReady: library == null || !library!.loading),
          CatalogScreen(
              key: ValueKey('search:$scope'),
              api: api,
              home: false,
              active: tab == 1,
              historyStore: BrowserSessionStore(
                  '$storageNamespace::search-history',
                  scope: scope == 'guest' ? _guestHome.scopeId : scope),
              recentQueries: library?.recentSearchQueries,
              homeSignals: _homeSignals,
              player: player,
              likedTracks: library?.likes ?? const [],
              onLike: _like),
          libraryPage!,
          _queue(),
        ];
        final pages = IndexedStack(index: tab, children: [
          for (var i = 0; i < pageChildren.length; i++)
            PageEntrance(
                key: ValueKey('page:$scope:$i'),
                active: tab == i,
                child: pageChildren[i]),
        ]);
        return Scaffold(
            body: SafeArea(
                bottom: false,
                child: Row(children: [
                  if (wide) _sidebar(),
                  Expanded(
                      child: Column(
                          children: [_header(wide), Expanded(child: pages)])),
                ])),
            bottomNavigationBar: SafeArea(
                top: false,
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  if (player.current != null)
                    PlaybackDock(
                        player: player,
                        audio: audio,
                        wide: wide,
                        liked: library?.likes.any((t) =>
                                extractTrackId(t) ==
                                extractTrackId(player.current)) ==
                            true,
                        onLike: () => unawaited(_like(player.current!)),
                        onRepeat: () =>
                            setState(() => audio.repeatOne = !audio.repeatOne),
                        onQueue: () => _navigate(3)),
                  if (!wide)
                    NeatieNavigation(
                        labels: names,
                        icons: icons,
                        selected: tab,
                        onSelect: _navigate),
                ])));
      });

  @override
  void dispose() {
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
