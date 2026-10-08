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
import 'web_auth.dart';
import 'web_library.dart';
import 'account_screen.dart';
import 'library_screen.dart';
import 'catalog_screen.dart';
import 'home_signals.dart';
import 'guest_home_state.dart';
import 'catalog_api.dart';
import 'design_system.dart';
import 'playback_dock.dart';
import 'playback_diagnostics.dart';

const webBuild = '2026-10-08-cache-cold-latency';

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
  late final JSFunction _lifecycle;
  late final StreamSubscription<String> _guestPlaybackEvents;
  WebLibrary? library;
  String scope = 'guest';
  late GuestHomeState _guestHome;
  late GuestHomeState _listeningHistory;
  final _historyTracker = PlaybackHistoryTracker();
  int _guestHomeRevision = 0;
  int _homeForegroundRevision = 0;
  bool _foreground = true;
  int tab = 0;
  static const names = ['Home', 'Search', 'Library', 'Queue', 'Account'];
  static const icons = [
    Icons.home_outlined,
    Icons.search,
    Icons.library_music_outlined,
    Icons.queue_music,
    Icons.person_outline
  ];

  @override
  void initState() {
    super.initState();
    _foreground = web.document.visibilityState == 'visible';
    const configured = String.fromEnvironment('AURALIS_PROXY_URL');
    api = MusicApi(configured.isEmpty
        ? Uri.parse(Uri.base.origin)
        : Uri.parse(configured));
    api.accessToken =
        () => widget.auth.client?.auth.currentSession?.accessToken;
    scope = widget.auth.userId ?? 'guest';
    api.userScopeId = scope;
    _guestHome = _loadGuestHomeState(api.base.toString());
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    _listeningHistory = _loadListeningHistory();
    audio = BrowserAudio();
    audio.setDiagnosticScope(api.base.toString(), scope);
    api.onDiagnostic = audio.record;
    player = WebPlayer(
        api, audio, BrowserSessionStore(api.base.toString(), scope: scope));
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
        if (tab == 2) unawaited(library?.refresh());
      } else {
        if (mounted) setState(() => _foreground = false);
        player.save();
      }
    }).toJS;
    web.window.addEventListener('pagehide', _lifecycle);
    web.window.addEventListener('pageshow', _lifecycle);
    web.document.addEventListener('visibilitychange', _lifecycle);
    widget.auth.addListener(_accountChanged);
    if (widget.auth.passwordRecovery) tab = 4;
    _openLibrary();
    unawaited(_hydrateHistory());
  }

  void _openLibrary() {
    library?.removeListener(_libraryChanged);
    library?.dispose();
    library = widget.auth.userId == null || widget.auth.client == null
        ? null
        : WebLibrary(widget.auth.client!, widget.auth.userId!,
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
      player
          .changeAccount(BrowserSessionStore(api.base.toString(), scope: next));
      audio.setDiagnosticScope(api.base.toString(), next);
      _historyTracker.reset();
      _listeningHistory = _loadListeningHistory();
      _guestHomeRevision++;
      _openLibrary();
      unawaited(_hydrateHistory());
    }
    if (widget.auth.passwordRecovery) tab = 4;
    if (mounted) setState(() {});
  }

  void _navigate(int value) {
    setState(() => tab = value);
    if (value == 2) unawaited(library?.refresh());
  }

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

  Map<String, dynamic> _homeSignals() => buildWebHomeSignals(
        likedTracks: library?.likes ?? const [],
        playlists: library?.playlists ?? const [],
        libraryKnown:
            library == null || (!library!.loading && library!.error == null),
        allowColdStart: widget.auth.userId == null,
        playedSnapshots: _listeningHistory.snapshots,
      );

  String get _listeningStorageKey => scope == 'guest'
      ? 'neatie:guest-home:${api.base.origin}'
      : 'neatie:$scope:home-history:${api.base.origin}';

  GuestHomeState _loadListeningHistory() {
    if (scope == 'guest') return _guestHome;
    try {
      final saved = web.window.localStorage.getItem(_listeningStorageKey);
      if (saved != null)
        return GuestHomeState.decode(saved, expectedScope: scope);
    } catch (_) {/* The account still has an isolated in-memory history. */}
    return GuestHomeState(scopeId: scope);
  }

  Future<void> _hydrateHistory() async {
    if (scope == 'guest') return;
    final expectedScope = scope;
    final history = _listeningHistory;
    try {
      final payload = await CatalogApi(api).history();
      if (!mounted ||
          scope != expectedScope ||
          !identical(history, _listeningHistory) ||
          payload['user_scope_id'] != expectedScope) return;
      history.mergeHistory([
        ...MusicApi.tracks(payload['recent_track_snapshots']),
        ...MusicApi.tracks(payload['frequent_track_snapshots']),
      ]);
      _saveGuestHomeState();
      setState(() => _guestHomeRevision++);
    } catch (_) {/* Saved local evidence remains available for this account. */}
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
    _listeningHistory.recordPlayedSnapshot(track, at: now);
    _guestHomeRevision++;
    _saveGuestHomeState();
    unawaited(CatalogApi(api)
        .recordPlay(track,
            scope: api.homeRequestScopeId,
            occurredAt: now.millisecondsSinceEpoch / 1000)
        .catchError((_) {}));
    if (mounted) setState(() {});
  }

  Widget _queue() => ListView(padding: const EdgeInsets.all(24), children: [
        EditorialHeading(
            title: 'Up next.', eyebrow: 'YOUR QUEUE', subtitle: player.title),
        if (player.queue.isEmpty)
          const Padding(
              padding: EdgeInsets.symmetric(vertical: 24),
              child: Text('Choose a song to start a related-song queue.')),
        for (var i = 0; i < player.queue.length; i++)
          ListTile(
              leading: _queueArtwork(player.queue[i]),
              selected: i == player.index,
              title: Text('${player.queue[i]['title']}',
                  maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: Text(
                  '${player.queue[i]['artist'] ?? player.queue[i]['channel'] ?? ''}',
                  maxLines: 1),
              onTap: () => unawaited(player.playIndex(i)),
              trailing: IconButton(
                  tooltip: 'Like song',
                  icon: const Icon(Icons.favorite_border),
                  onPressed: () => _like(player.queue[i]))),
        if (player.relatedError != null) Text(player.relatedError!),
        if (player.queue.isNotEmpty &&
            (player.hasMore || player.relatedError != null))
          TextButton(
              onPressed: player.loadingRelated
                  ? null
                  : () => unawaited(player.replenish()),
              child: Text(player.loadingRelated
                  ? 'Finding related songs…'
                  : 'Load related songs')),
        if (player.storageError != null) Text(player.storageError!),
      ]);

  Widget _queueArtwork(Map<String, dynamic> track) {
    final uri = api.artworkUri(track['thumbnail']);
    if (uri == null)
      return const SizedBox(width: 48, child: Icon(Icons.music_note));
    return ClipRRect(
        borderRadius: BorderRadius.circular(6),
        child: Image.network(
          uri.toString(),
          width: 48,
          height: 48,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) =>
              const SizedBox(width: 48, child: Icon(Icons.music_note)),
        ));
  }

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
        for (var i = 0; i < names.length; i++)
          Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: AnimatedContainer(
                  duration: MediaQuery.disableAnimationsOf(context)
                      ? Duration.zero
                      : const Duration(milliseconds: 220),
                  curve: Curves.easeOutCubic,
                  decoration: BoxDecoration(
                      color: tab == i
                          ? NeatieDesign.accent.withValues(alpha: .12)
                          : Colors.transparent,
                      borderRadius: BorderRadius.circular(14)),
                  child: Material(
                      color: Colors.transparent,
                      borderRadius: BorderRadius.circular(14),
                      child: InkWell(
                          borderRadius: BorderRadius.circular(14),
                          onTap: () => _navigate(i),
                          child: Padding(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 14, vertical: 14),
                              child: Row(children: [
                                Icon(icons[i],
                                    size: 21,
                                    color: tab == i
                                        ? NeatieDesign.accent
                                        : NeatieDesign.muted),
                                const SizedBox(width: 14),
                                Text(names[i],
                                    style: TextStyle(
                                        fontSize: 13,
                                        fontWeight: FontWeight.w600,
                                        color: tab == i
                                            ? NeatieDesign.accent
                                            : NeatieDesign.ink)),
                              ])))))),
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
            onPressed: () => _navigate(4),
            style: IconButton.styleFrom(backgroundColor: NeatieDesign.raised),
            icon: const Icon(Icons.person_outline_rounded, size: 20)),
      ]));

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
      animation: player,
      builder: (context, _) {
        final wide = MediaQuery.sizeOf(context).width >= 850;
        final pageChildren = <Widget>[
          CatalogScreen(
              key: ValueKey('home:$scope'),
              api: api,
              home: true,
              active: tab == 0,
              player: player,
              onLike: _like,
              homeSignals: _homeSignals,
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
              player: player,
              likedTracks: library?.likes ?? const [],
              onLike: _like),
          LibraryScreen(
              library: library, player: player, onSignIn: () => _navigate(4)),
          _queue(),
          AccountScreen(auth: widget.auth),
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
                    NavigationBar(
                        selectedIndex: tab,
                        onDestinationSelected: _navigate,
                        height: 68,
                        destinations: [
                          for (var i = 0; i < names.length; i++)
                            NavigationDestination(
                                icon: Icon(icons[i], size: 21), label: names[i])
                        ]),
                ])));
      });

  @override
  void dispose() {
    widget.auth.removeListener(_accountChanged);
    library?.removeListener(_libraryChanged);
    library?.dispose();
    web.window.removeEventListener('pagehide', _lifecycle);
    web.window.removeEventListener('pageshow', _lifecycle);
    web.document.removeEventListener('visibilitychange', _lifecycle);
    _guestPlaybackEvents.cancel();
    player.dispose();
    api.client.close();
    super.dispose();
  }
}
