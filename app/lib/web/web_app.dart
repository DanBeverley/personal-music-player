import 'dart:async';
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

const webBuild = '2026-10-03-history-search';

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
    title: 'Neatie', debugShowCheckedModeBanner: false,
    theme: ThemeData.dark(useMaterial3: true).copyWith(
      scaffoldBackgroundColor: const Color(0xff101114),
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xffd7e8b6), brightness: Brightness.dark)),
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
  int tab = 1;
  static const names = ['Home', 'Search', 'Library', 'Queue', 'Account'];
  static const icons = [Icons.home_outlined, Icons.search, Icons.library_music_outlined, Icons.queue_music, Icons.person_outline];

  @override
  void initState() {
    super.initState();
    const configured = String.fromEnvironment('AURALIS_PROXY_URL');
    api = MusicApi(configured.isEmpty ? Uri.parse(Uri.base.origin) : Uri.parse(configured));
    api.accessToken = () => widget.auth.client?.auth.currentSession?.accessToken;
    scope = widget.auth.userId ?? 'guest';
    api.userScopeId = scope;
    _guestHome = _loadGuestHomeState(api.base.toString());
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    _listeningHistory = _loadListeningHistory();
    audio = BrowserAudio();
    audio.setDiagnosticScope(api.base.toString(), scope);
    api.onDiagnostic = audio.record;
    player = WebPlayer(api, audio, BrowserSessionStore(api.base.toString(), scope: scope));
    _guestPlaybackEvents = audio.events.listen(_guestPlaybackEvent);
    audio.diagnosticContext = () => {'build': webBuild, ...player.diagnostics};
    audio.onReconcile = (reason) { player.reconcile(); audio.record('reconciled', {'reason': reason}); };
    audio.onPlay = () => unawaited(player.play());
    audio.onPause = player.pause;
    audio.onNext = () => unawaited(player.next());
    audio.onPrevious = () => unawaited(player.previous());
    _lifecycle = ((web.Event event) {
      audio.record('lifecycle', {'kind': event.type});
      player.lifecycle(event.type, visible: web.document.visibilityState == 'visible');
      if (web.document.visibilityState == 'visible') {
        player.reconcile();
        if (tab == 2) unawaited(library?.refresh());
      } else { player.save(); }
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
    library = widget.auth.userId == null || widget.auth.client == null ? null :
      WebLibrary(widget.auth.client!, widget.auth.userId!,
        playlistWritesEnabled: const bool.fromEnvironment('WEB_PLAYLIST_WRITES'));
    library?.addListener(_libraryChanged);
    unawaited(library?.refresh());
  }
  void _libraryChanged() { if (mounted) setState(() => _guestHomeRevision++); }
  void _accountChanged() {
    final next = widget.auth.userId ?? 'guest';
    api.homeScopeId = widget.auth.userId ?? _guestHome.scopeId;
    if (next != scope) {
      scope = next;
      api.userScopeId = next;
      audio.setDiagnosticScope(api.base.toString(), next);
      player.changeAccount(BrowserSessionStore(api.base.toString(), scope: next));
      _historyTracker.reset();
      _listeningHistory = _loadListeningHistory();
      _guestHomeRevision++;
      _openLibrary();
      unawaited(_hydrateHistory());
    }
    if (widget.auth.passwordRecovery) tab = 4;
    if (mounted) setState(() {});
  }
  void _navigate(int value) { setState(() => tab = value); if (value == 2) unawaited(library?.refresh()); }

  void showDiagnostics() {
    audio.record('user-report', {'build': webBuild});
    final report = audio.diagnostics;
    showDialog<void>(context: context, builder: (context) => AlertDialog(
      title: const Text('Playback diagnostics'),
      content: SingleChildScrollView(child: SelectableText(report)),
      actions: [TextButton(onPressed: () => Clipboard.setData(ClipboardData(text: report)), child: const Text('Copy')),
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Close'))]));
  }

  Future<void> _like(Map<String, dynamic> track) async {
    final repo = library;
    if (repo == null) { _navigate(4); return; }
    final liked = repo.likes.any((t) => extractTrackId(t) == extractTrackId(track));
    try { await repo.setLiked(track, !liked); }
    catch (_) { if (mounted) ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Could not update liked songs. Retry.'))); }
  }

  Map<String, dynamic> _homeSignals() => buildWebHomeSignals(
        likedTracks: library?.likes ?? const [],
        playlists: library?.playlists ?? const [],
        libraryKnown: library == null ||
            (!library!.loading && library!.error == null),
        allowColdStart: widget.auth.userId == null,
        playedSnapshots: _listeningHistory.snapshots,
      );

  String get _listeningStorageKey => scope == 'guest'
    ? 'neatie:guest-home:${api.base.origin}' : 'neatie:$scope:home-history:${api.base.origin}';

  GuestHomeState _loadListeningHistory() {
    if (scope == 'guest') return _guestHome;
    try {
      final saved = web.window.localStorage.getItem(_listeningStorageKey);
      if (saved != null) return GuestHomeState.decode(saved, expectedScope: scope);
    } catch (_) { /* The account still has an isolated in-memory history. */ }
    return GuestHomeState(scopeId: scope);
  }

  Future<void> _hydrateHistory() async {
    if (scope == 'guest') return;
    final expectedScope = scope;
    final history = _listeningHistory;
    try {
      final payload = await CatalogApi(api).history();
      if (!mounted || scope != expectedScope || !identical(history, _listeningHistory) ||
          payload['user_scope_id'] != expectedScope) return;
      history.mergeHistory([
        ...MusicApi.tracks(payload['recent_track_snapshots']),
        ...MusicApi.tracks(payload['frequent_track_snapshots']),
      ]);
      _saveGuestHomeState();
      setState(() => _guestHomeRevision++);
    } catch (_) { /* Saved local evidence remains available for this account. */ }
  }

  GuestHomeState _loadGuestHomeState(String backend) {
    final key = 'neatie:guest-home:${Uri.parse(backend).origin}';
    try {
      final saved = web.window.localStorage.getItem(key);
      if (saved != null) return GuestHomeState.decode(saved);
    } catch (_) {
      // Private browsing may deny storage; this session still gets an isolated scope.
    }
    final fresh = GuestHomeState(scopeId: GuestHomeState.newScopeId(Random.secure()));
    try { web.window.localStorage.setItem(key, fresh.encode()); } catch (_) {}
    return fresh;
  }

  void _saveGuestHomeState() {
    try { web.window.localStorage.setItem(_listeningStorageKey, _listeningHistory.encode()); } catch (_) {}
  }

  void _guestPlaybackEvent(String event) {
    if (!_historyTracker.observe(event, operation: player.listeningEpoch,
        trackId: extractTrackId(player.current), loadedTrackId: audio.loadedTrackId,
        position: audio.position, paused: audio.paused)) return;
    final track = player.current;
    if (track == null) return;
    final now = DateTime.now();
    _listeningHistory.recordPlayedSnapshot(track, at: now);
    _guestHomeRevision++;
    _saveGuestHomeState();
    unawaited(CatalogApi(api).recordPlay(track, scope: api.homeRequestScopeId,
      occurredAt: now.millisecondsSinceEpoch / 1000).catchError((_) {}));
    if (mounted) setState(() {});
  }

  Widget _queue() => ListView(padding: const EdgeInsets.all(24), children: [
    Text(player.title, style: Theme.of(context).textTheme.headlineSmall),
    if (player.queue.isEmpty) const Padding(padding: EdgeInsets.symmetric(vertical: 24), child: Text('Choose a song to start a related-song queue.')),
    for (var i = 0; i < player.queue.length; i++) ListTile(
      leading: _queueArtwork(player.queue[i]),
      selected: i == player.index, title: Text('${player.queue[i]['title']}', maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text('${player.queue[i]['artist'] ?? player.queue[i]['channel'] ?? ''}', maxLines: 1),
      onTap: () => unawaited(player.playIndex(i)),
      trailing: IconButton(tooltip: 'Like song', icon: const Icon(Icons.favorite_border), onPressed: () => _like(player.queue[i]))),
    if (player.relatedError != null) Text(player.relatedError!),
    if (player.queue.isNotEmpty && (player.hasMore || player.relatedError != null)) TextButton(
      onPressed: player.loadingRelated ? null : () => unawaited(player.replenish()),
      child: Text(player.loadingRelated ? 'Finding related songs…' : 'Load related songs')),
    if (player.storageError != null) Text(player.storageError!),
  ]);

  Widget _queueArtwork(Map<String, dynamic> track) {
    final uri = api.artworkUri(track['thumbnail']);
    if (uri == null) return const SizedBox(width: 48, child: Icon(Icons.music_note));
    return ClipRRect(borderRadius: BorderRadius.circular(6), child: Image.network(
      uri.toString(), width: 48, height: 48, fit: BoxFit.cover,
      errorBuilder: (_, __, ___) => const SizedBox(width: 48, child: Icon(Icons.music_note)),
    ));
  }

  Widget _controls() => Material(color: const Color(0xff202226), child: Padding(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6), child: Column(mainAxisSize: MainAxisSize.min, children: [
      Row(children: [Expanded(child: Text(player.current?['title']?.toString() ?? '', maxLines: 1, overflow: TextOverflow.ellipsis)),
        IconButton(tooltip: 'Like current song', onPressed: () => _like(player.current!), icon: Icon(
          library?.likes.any((t) => extractTrackId(t) == extractTrackId(player.current)) == true ? Icons.favorite : Icons.favorite_border))]),
      if (player.error != null) Text(player.error!, maxLines: 2, style: const TextStyle(color: Colors.orangeAccent)),
      if (player.loading) const LinearProgressIndicator(),
      Slider(value: player.position.clamp(0, player.duration > 0 ? player.duration : 1),
        max: player.duration > 0 ? player.duration : 1, onChanged: player.duration > 0 ? player.seek : null),
      Row(mainAxisAlignment: MainAxisAlignment.center, children: [
        IconButton(tooltip: 'Previous', onPressed: player.index > 0 ? () => unawaited(player.previous()) : null, icon: const Icon(Icons.skip_previous)),
        IconButton(tooltip: audio.paused ? 'Play' : 'Pause', onPressed: () => audio.paused ? unawaited(player.play()) : player.pause(),
          icon: Icon(audio.paused ? Icons.play_circle_fill : Icons.pause_circle_filled), iconSize: 40),
        IconButton(tooltip: 'Next', onPressed: player.index + 1 < player.queue.length ? () => unawaited(player.next()) : null, icon: const Icon(Icons.skip_next)),
        Text('${player.position.toInt()} / ${player.duration.toInt()} s'),
        IconButton(tooltip: audio.repeatOne ? 'Turn off repeat' : 'Repeat this song', isSelected: audio.repeatOne,
          onPressed: () => setState(() => audio.repeatOne = !audio.repeatOne), icon: const Icon(Icons.repeat_one)),
      ]),
    ])));

  @override
  Widget build(BuildContext context) => AnimatedBuilder(animation: player, builder: (context, _) {
    final wide = MediaQuery.sizeOf(context).width >= 850;
    final pages = IndexedStack(index: tab, children: [
      CatalogScreen(key: ValueKey('home:$scope'), api: api, home: true, active: tab == 0, player: player, onLike: _like, homeSignals: _homeSignals,
        homeSignalRevision: _guestHomeRevision,
        homeSignalsReady: library == null || !library!.loading),
      CatalogScreen(key: ValueKey('search:$scope'), api: api, home: false, active: tab == 1, player: player, onLike: _like),
      LibraryScreen(library: library, player: player, onSignIn: () => _navigate(4)),
      _queue(), AccountScreen(auth: widget.auth),
    ]);
    return Scaffold(appBar: AppBar(title: const Text('neatie'), actions: [
      TextButton(onPressed: showDiagnostics, child: const Text('Diagnostics')),
      IconButton(tooltip: widget.auth.email ?? 'Account', onPressed: () => _navigate(4), icon: const Icon(Icons.account_circle_outlined))]),
      body: Column(children: [
        const Padding(padding: EdgeInsets.only(bottom: 4), child: Text('Build $webBuild', style: TextStyle(fontSize: 10, color: Colors.grey))),
        Expanded(child: Row(children: [if (wide) NavigationRail(selectedIndex: tab, onDestinationSelected: _navigate,
          labelType: NavigationRailLabelType.all, destinations: [for (var i = 0; i < names.length; i++) NavigationRailDestination(icon: Icon(icons[i]), label: Text(names[i]))]),
          Expanded(child: pages)])),
      ]),
      bottomNavigationBar: SafeArea(top: false, child: Column(mainAxisSize: MainAxisSize.min, children: [
        if (player.current != null) _controls(),
        if (!wide) NavigationBar(selectedIndex: tab, onDestinationSelected: _navigate, height: 64,
          destinations: [for (var i = 0; i < names.length; i++) NavigationDestination(icon: Icon(icons[i]), label: names[i])]),
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
