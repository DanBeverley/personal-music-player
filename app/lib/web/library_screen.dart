import 'dart:async';

import 'package:flutter/material.dart';

import '../logic/track_metadata.dart';
import 'web_library.dart';
import 'web_player.dart';
import 'design_system.dart';
import 'web_auth.dart';
import 'account_screen.dart';
import 'collection_search.dart';
import 'browse_widgets.dart';
import 'home_widgets.dart' show HomeArtwork;
import 'library_assistant.dart';

String _songCount(int count) => '$count ${count == 1 ? 'song' : 'songs'}';

String _createdLabel(Map<String, dynamic> playlist) {
  final date = catalogReleaseDate({'release_date': playlist['created_at']});
  return date.isEmpty ? '' : 'Created $date';
}

/// Every web surface uses the same account-scoped cloud edits as Library.
Future<void> showSongPlaylistPicker(BuildContext context, WebLibrary? library,
    WebPlayer player, Map<String, dynamic> track,
    {required VoidCallback onSignIn}) async {
  if (library == null) {
    final signIn = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
                title: const Text('Keep this song'),
                content: const Text('Sign in to save songs to your playlists.'),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context),
                      child: const Text('Not now')),
                  FilledButton(
                      onPressed: () => Navigator.pop(context, true),
                      child: const Text('Sign in'))
                ]));
    if (signIn == true && context.mounted) onSignIn();
    return;
  }
  if (!library.active) return;
  await showDialog<void>(
      context: context,
      builder: (_) => _SongPlaylistPicker(
          library: library, player: player, track: Map.of(track)));
}

class _SongPlaylistPicker extends StatefulWidget {
  const _SongPlaylistPicker(
      {required this.library, required this.player, required this.track});
  final WebLibrary library;
  final WebPlayer player;
  final Map<String, dynamic> track;
  @override
  State<_SongPlaylistPicker> createState() => _SongPlaylistPickerState();
}

class _SongPlaylistPickerState extends State<_SongPlaylistPicker> {
  final _name = TextEditingController();
  bool _busy = false;
  String? _error;
  bool _contains(Map<String, dynamic> playlist) =>
      (playlist['tracks'] as List? ?? const []).whereType<Map>().any((track) =>
          extractTrackId(Map<String, dynamic>.from(track)) ==
          extractTrackId(widget.track));
  Future<void> _save([String? id]) async {
    final owner = widget.library;
    if (_busy || !owner.active || !owner.playlistWritesEnabled) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (id == null) {
        id = await owner.createPlaylist(_name.text, initialTrack: widget.track);
      } else {
        final playlist =
            owner.playlists.where((p) => p['id'] == id).firstOrNull;
        if (playlist == null)
          throw StateError('This playlist is no longer available.');
        if (!_contains(playlist))
          await owner.addTrack(id, widget.track,
              expectedRevision: playlist['revision'] as int?);
      }
      if (!mounted || !owner.active) return;
      final playlist = owner.playlists.where((p) => p['id'] == id).first;
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Added to ${playlist['name']}')));
      Navigator.pop(context);
    } catch (_) {
      if (mounted && owner.active)
        setState(() =>
            _error = owner.error ?? 'Could not add this song. Please retry.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
      animation: Listenable.merge([widget.library, widget.player]),
      builder: (context, _) {
        final owner = widget.library;
        return AlertDialog(
            title: Row(children: [
              const Expanded(child: Text('Add to playlist')),
              IconButton(
                  tooltip: 'Close playlist picker',
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close))
            ]),
            content: SizedBox(
                width: 420,
                child: !owner.active
                    ? const Text(
                        'Your account changed. Reopen this window to continue.')
                    : !owner.playlistWritesEnabled
                        ? const Text(
                            'Playlist editing is currently unavailable.')
                        : SingleChildScrollView(
                            child: Column(
                                mainAxisSize: MainAxisSize.min,
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                ListTile(
                                    contentPadding: EdgeInsets.zero,
                                    leading: HomeArtwork(
                                        api: widget.player.api,
                                        item: widget.track,
                                        kind: 'track',
                                        size: 48),
                                    title: Text(
                                        '${widget.track['title'] ?? 'Song'}',
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis),
                                    subtitle: Text(
                                        '${widget.track['artist'] ?? widget.track['channel'] ?? ''}',
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis)),
                                if (_error != null)
                                  Padding(
                                      padding: const EdgeInsets.symmetric(
                                          vertical: 12),
                                      child: Text(_error!,
                                          semanticsLabel: _error)),
                                if (owner.loading)
                                  const LinearProgressIndicator(),
                                for (final playlist in owner.playlists)
                                  ListTile(
                                      contentPadding: EdgeInsets.zero,
                                      title: Text('${playlist['name']}',
                                          maxLines: 2,
                                          overflow: TextOverflow.ellipsis),
                                      subtitle: Text([
                                        _songCount((playlist['tracks'] as List)
                                            .length),
                                        _createdLabel(playlist)
                                      ].where((s) => s.isNotEmpty).join(' · ')),
                                      trailing: Icon(_contains(playlist)
                                          ? Icons.check_rounded
                                          : Icons.add_rounded),
                                      enabled: !_busy && !_contains(playlist),
                                      onTap: () =>
                                          _save(playlist['id'] as String)),
                                const Divider(height: 28),
                                TextField(
                                    controller: _name,
                                    enabled: !_busy,
                                    maxLength: 120,
                                    decoration: const InputDecoration(
                                        labelText: 'New playlist name'),
                                    onChanged: (_) => setState(() {}),
                                    onSubmitted: (_) {
                                      if (_name.text.trim().isNotEmpty) _save();
                                    }),
                                Align(
                                    alignment: Alignment.centerRight,
                                    child: FilledButton.icon(
                                        onPressed:
                                            _busy || _name.text.trim().isEmpty
                                                ? null
                                                : () => _save(),
                                        icon: _busy
                                            ? const SizedBox(
                                                width: 16,
                                                height: 16,
                                                child:
                                                    CircularProgressIndicator(
                                                        strokeWidth: 2))
                                            : const Icon(Icons.add),
                                        label: Text(_busy
                                            ? 'Saving…'
                                            : 'Create and add'))),
                              ]))));
      });
}

class LibraryScreen extends StatefulWidget {
  const LibraryScreen(
      {super.key,
      required this.library,
      required this.player,
      this.auth,
      this.playlistShortcut,
      required this.onSignIn});

  final WebLibrary? library;
  final WebPlayer player;
  final WebAuth? auth;
  final VoidCallback onSignIn;
  final ({String id, int revision})? playlistShortcut;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  String? _selectedPlaylist;
  bool _showLikes = false;
  String? _writeError;
  bool _writing = false;
  int _writeGeneration = 0;
  final _playlistSearch = TextEditingController();
  DialogRoute<void>? _songsRoute;

  void _closeSongsDialog() {
    final route = _songsRoute;
    _songsRoute = null;
    if (route != null)
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (route.isActive) route.navigator?.removeRoute(route);
      });
  }

  @override
  void dispose() {
    _closeSongsDialog();
    _playlistSearch.dispose();
    super.dispose();
  }

  @override
  void didUpdateWidget(covariant LibraryScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.library, widget.library)) {
      _closeSongsDialog();
      _selectedPlaylist = null;
      _showLikes = false;
      _playlistSearch.clear();
      _writeGeneration++;
      _writing = false;
      _writeError = null;
    }
    if (widget.playlistShortcut case final shortcut?
        when shortcut != oldWidget.playlistShortcut) {
      _selectedPlaylist = shortcut.id;
      _showLikes = false;
      _playlistSearch.clear();
    }
  }

  Future<bool> _write(Future<void> Function(WebLibrary repo) action,
      {WebLibrary? owner}) async {
    final repo = owner ?? widget.library;
    if (!mounted ||
        repo == null ||
        !identical(repo, widget.library) ||
        _writing) return false;
    final generation = ++_writeGeneration;
    setState(() {
      _writing = true;
      _writeError = null;
    });
    try {
      await action(repo);
      return mounted && identical(repo, widget.library);
    } catch (_) {
      if (mounted && identical(repo, widget.library))
        setState(() =>
            _writeError = repo.error ?? 'Could not save this change. Retry.');
      return false;
    } finally {
      if (mounted && generation == _writeGeneration)
        setState(() => _writing = false);
    }
  }

  Future<String?> _nameDialog({String? initial, required String title}) =>
      showDialog<String>(
          context: context,
          builder: (_) =>
              _PlaylistNameDialog(initial: initial ?? '', title: title));

  void _askNeatie(WebLibrary repo) {
    final auth = widget.auth;
    if (auth == null) return;
    final scope = widget.player.api.userScopeId;
    showDialog<void>(context: context, builder: (_) => AnimatedBuilder(
      animation: auth,
      builder: (context, _) => Dialog(
        child: auth.userId == scope
            ? LibraryAssistant(library: repo, player: widget.player)
            : const Padding(padding: EdgeInsets.all(24),
                child: Text('Account changed. Reopen chat to continue.')),
      ),
    ));
  }

  Future<void> _addSongs(String id) async {
    final owner = widget.library;
    if (owner == null || !owner.playlistWritesEnabled || _songsRoute != null)
      return;
    final route = DialogRoute<void>(
        context: context,
        builder: (context) => Dialog(
            child: ConstrainedBox(
                constraints: BoxConstraints(
                    maxWidth: 620,
                    maxHeight: MediaQuery.sizeOf(context).height * .85),
                child: Padding(
                    padding: const EdgeInsets.all(20),
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      Row(children: [
                        Expanded(
                            child: Text('Add songs',
                                style: Theme.of(context).textTheme.titleLarge)),
                        IconButton(
                            tooltip: 'Close song search',
                            onPressed: () => Navigator.pop(context),
                            icon: const Icon(Icons.close))
                      ]),
                      Flexible(
                          child: SingleChildScrollView(
                              child: AnimatedBuilder(
                                  animation: owner,
                                  builder: (context, _) {
                                    final playlist = owner.playlists
                                        .where((p) => p['id'] == id)
                                        .firstOrNull;
                                    if (!mounted ||
                                        !identical(owner, widget.library) ||
                                        playlist == null) {
                                      return const Text(
                                          'Close this window and reopen your playlist.');
                                    }
                                    final tracks =
                                        playlist['tracks'] as List? ?? const [];
                                    return Column(
                                        mainAxisSize: MainAxisSize.min,
                                        children: [
                                          if (owner.error != null)
                                            Text(owner.error!),
                                          CatalogTrackSearch(
                                              api: widget.player.api,
                                              label:
                                                  'Find songs to add to this playlist',
                                              actionLabel: 'Add to playlist',
                                              isAdded: (track) => tracks
                                                  .whereType<Map>()
                                                  .any((saved) =>
                                                      extractTrackId(Map<String,
                                                              dynamic>.from(
                                                          saved)) ==
                                                      extractTrackId(track)),
                                              onAdd: (track) => _write(
                                                  (r) => r.addTrack(id, track,
                                                      expectedRevision:
                                                          playlist['revision']
                                                              as int?),
                                                  owner: owner)),
                                        ]);
                                  }))),
                    ])))));
    _songsRoute = route;
    await Navigator.of(context).push(route);
    if (identical(_songsRoute, route)) _songsRoute = null;
  }

  Future<bool> _confirm(String title, String message) async =>
      await showDialog<bool>(
          context: context,
          builder: (context) =>
              AlertDialog(title: Text(title), content: Text(message), actions: [
                TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel')),
                FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('Delete'))
              ])) ??
      false;

  String _trackTitle(Map<String, dynamic> track) =>
      track['title']?.toString() ?? 'Untitled song';
  String _trackSubtitle(Map<String, dynamic> track) =>
      track['artist']?.toString() ?? track['channel']?.toString() ?? '';

  Widget _trackTile(
      Map<String, dynamic> track, List<Map<String, dynamic>> queue, int index,
      {VoidCallback? remove, VoidCallback? moveUp, VoidCallback? moveDown}) {
    final playable = extractTrackId(track)?.isNotEmpty == true;
    final owner = widget.library;
    final seconds = trackDurationSeconds(track);
    return BrowseTrackRow(
        title: _trackTitle(track),
        artist: [_trackSubtitle(track), catalogReleaseDate(track)]
            .where((value) => value.isNotEmpty)
            .join(' · '),
        number: index + 1,
        duration: seconds > 0
            ? '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}'
            : '',
        artwork: _trackArtwork(track),
        onTap: playable
            ? () => unawaited(widget.player.startQueue(queue, index, 'Library'))
            : null,
        actions: [
          IconButton(
              tooltip: 'Add ${_trackTitle(track)} to playlist',
              onPressed: !playable
                  ? null
                  : () => showSongPlaylistPicker(
                      context, widget.library, widget.player, track,
                      onSignIn: widget.onSignIn),
              icon: const Icon(Icons.playlist_add_rounded)),
          IconButton(
              tooltip: 'Play next ${_trackTitle(track)}',
              onPressed: !playable
                  ? null
                  : () {
                      if (mounted && identical(owner, widget.library))
                        widget.player.playNext(track);
                    },
              icon: const Icon(Icons.playlist_play)),
          if (moveUp != null || moveDown != null)
            PopupMenuButton<String>(
                tooltip: 'Playlist options for ${_trackTitle(track)}',
                enabled: !_writing,
                onSelected: (value) => switch (value) {
                      'up' => moveUp?.call(),
                      'down' => moveDown?.call(),
                      _ => remove?.call(),
                    },
                itemBuilder: (_) => [
                      PopupMenuItem(
                          value: 'up',
                          enabled: moveUp != null,
                          child: const Text('Move up')),
                      PopupMenuItem(
                          value: 'down',
                          enabled: moveDown != null,
                          child: const Text('Move down')),
                      PopupMenuItem(
                          value: 'remove',
                          enabled: remove != null,
                          child: const Text('Remove from playlist')),
                    ])
          else if (remove != null)
            IconButton(
                tooltip: 'Remove from playlist',
                onPressed: _writing ? null : remove,
                icon: const Icon(Icons.remove_circle_outline))
        ]);
  }

  Widget _trackArtwork(Map<String, dynamic> track) {
    return HomeArtwork(
        api: widget.player.api, item: track, kind: 'track', size: 46);
  }

  @override
  Widget build(BuildContext context) {
    if (widget.auth?.passwordRecovery == true) {
      return AccountScreen(auth: widget.auth!);
    }
    final repo = widget.library;
    if (repo == null)
      return ListView(padding: const EdgeInsets.all(32), children: [
        const EditorialHeading(
            title: 'Your collection.',
            eyebrow: 'SAVED & LOVED',
            subtitle: 'The songs that stay with you.'),
        BrowseHero(
            title: 'Make yourself at home.',
            eyebrow: 'A SPACE FOR YOUR MUSIC',
            compact: true,
            artwork: (size) => _collectionArtwork([], size, liked: true),
            subtitle: const Text(
                'Keep your favourite songs and build playlists you can return to, on any device.',
                style: TextStyle(
                    fontSize: 14, height: 1.6, color: BrowsePalette.muted)),
            actions: [
              FilledButton.icon(
                  onPressed: widget.onSignIn,
                  icon: const Icon(Icons.arrow_forward_rounded, size: 18),
                  label: const Text('Sign in'))
            ]),
      ]);
    return AnimatedBuilder(
        animation: repo,
        builder: (context, _) {
          final selected = repo.playlists
              .where((p) => p['id'] == _selectedPlaylist)
              .firstOrNull;
          if (_selectedPlaylist != null && selected == null)
            _selectedPlaylist = null;
          final selectedTracks = selected == null
              ? const <Map<String, dynamic>>[]
              : List<Map<String, dynamic>>.from(
                  selected['tracks'] as List? ?? const []);
          final visible =
              matchingTrackIndices(selectedTracks, _playlistSearch.text);
          final collectionOpen = selected != null || _showLikes;
          return PageEntrance(
              key: ValueKey(
                  selected?['id'] ?? (_showLikes ? 'likes' : 'library')),
              active: true,
              child: ListView.builder(
                  key: PageStorageKey(
                      selected?['id'] ?? (_showLikes ? 'likes' : 'library')),
                  padding: EdgeInsets.fromLTRB(
                      MediaQuery.sizeOf(context).width < 700 ? 20 : 36,
                      12,
                      MediaQuery.sizeOf(context).width < 700 ? 20 : 36,
                      48),
                  itemCount: 1 +
                      (_showLikes
                          ? repo.likes.length
                          : selected != null
                              ? visible.length
                              : 0),
                  itemBuilder: (context, row) {
                    if (row > 0) {
                      if (_showLikes) {
                        final i = row - 1;
                        return _trackTile(repo.likes[i], repo.likes, i,
                            remove: () => _write(
                                (r) => r.setLiked(repo.likes[i], false)));
                      }
                      final i = visible[row - 1];
                      final playlist = selected!;
                      final id = playlist['id'].toString();
                      final entry = '${selectedTracks[i]['playlist_entry_id']}';
                      final revision = playlist['revision'] as int?;
                      return _trackTile(selectedTracks[i], selectedTracks, i,
                          remove: repo.playlistWritesEnabled
                              ? () => _write(
                                  (r) => r.removeTrack(id, entry,
                                      expectedRevision: revision),
                                  owner: repo)
                              : null,
                          moveUp: repo.playlistWritesEnabled && i > 0
                              ? () => _write(
                                  (r) => r.moveTrack(id, entry, i - 1,
                                      expectedRevision: revision),
                                  owner: repo)
                              : null,
                          moveDown: repo.playlistWritesEnabled &&
                                  i + 1 < selectedTracks.length
                              ? () => _write(
                                  (r) => r.moveTrack(id, entry, i + 1, expectedRevision: revision),
                                  owner: repo)
                              : null);
                    }
                    return Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (collectionOpen)
                            Align(
                                alignment: Alignment.centerLeft,
                                child: TextButton.icon(
                                    onPressed: () => setState(() {
                                          _selectedPlaylist = null;
                                          _showLikes = false;
                                          _playlistSearch.clear();
                                        }),
                                    icon: const Icon(Icons.arrow_back_rounded,
                                        size: 18),
                                    label: const Text('Back to library')))
                          else
                            _libraryHeading(repo),
                          if (!collectionOpen)
                            Align(alignment: Alignment.centerLeft,
                              child: TextButton.icon(
                                onPressed: () => _askNeatie(repo),
                                icon: const Icon(Icons.auto_awesome_outlined, size: 18),
                                label: const Text('Ask Neatie'),
                              )),
                          if (repo.loading)
                            const Padding(
                                padding: EdgeInsets.symmetric(vertical: 12),
                                child: NeatieLoading(
                                    label: 'Opening your library',
                                    showLabel: true)),
                          if (repo.error != null) ...[
                            Text(repo.error!,
                                style: TextStyle(
                                    color:
                                        Theme.of(context).colorScheme.error)),
                            TextButton(
                                onPressed: () => unawaited(repo.refresh()),
                                child: const Text('Retry'))
                          ],
                          if (_writeError != null)
                            Text(_writeError!,
                                style: TextStyle(
                                    color:
                                        Theme.of(context).colorScheme.error)),
                          if (!repo.playlistWritesEnabled)
                            const Padding(
                                padding: EdgeInsets.symmetric(vertical: 8),
                                child: Text(
                                    'Playlist editing is currently disabled on the web.')),
                          if (!collectionOpen) ...[
                            LayoutBuilder(builder: (context, bounds) {
                              final favourites = _likedCollection(repo);
                              if (bounds.maxWidth < 900 || repo.likes.isEmpty)
                                return favourites;
                              return Row(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Expanded(flex: 5, child: favourites),
                                    const SizedBox(width: 36),
                                    Expanded(
                                        flex: 6,
                                        child: Column(
                                            crossAxisAlignment:
                                                CrossAxisAlignment.start,
                                            children: [
                                              const Padding(
                                                  padding: EdgeInsets.only(
                                                      bottom: 16),
                                                  child: Text(
                                                      'FROM YOUR COLLECTION',
                                                      style: NeatieDesign
                                                          .eyebrow)),
                                              for (var i = 0;
                                                  i <
                                                      repo.likes.length
                                                          .clamp(0, 3);
                                                  i++)
                                                _trackTile(repo.likes[i],
                                                    repo.likes, i),
                                            ])),
                                  ]);
                            }),
                            BrowseSectionHeading(
                                title: 'Playlists',
                                caption:
                                    '${repo.playlists.length} collections, made by you.',
                                action: repo.playlistWritesEnabled
                                    ? TextButton.icon(
                                        onPressed: _writing
                                            ? null
                                            : () async {
                                                final name = await _nameDialog(
                                                    title: 'New playlist');
                                                if (name != null)
                                                  await _write(
                                                      (r) => r
                                                          .createPlaylist(name),
                                                      owner: repo);
                                              },
                                        icon: const Icon(Icons.add),
                                        label: const Text('Create playlist'))
                                    : null),
                            if (repo.playlists.isEmpty)
                              const Text('No playlists yet.'),
                            LayoutBuilder(builder: (context, bounds) {
                              final columns = bounds.maxWidth >= 900 ? 3 : 2;
                              final width =
                                  (bounds.maxWidth - (columns - 1) * 18) /
                                      columns;
                              return Wrap(
                                  spacing: 18,
                                  runSpacing: 24,
                                  children: [
                                    for (final (index, playlist)
                                        in repo.playlists.indexed)
                                      SizedBox(
                                          width: width,
                                          child: _playlistRow(playlist,
                                              index: index)),
                                  ]);
                            }),
                            if (widget.auth case final auth?) ...[
                              const SizedBox(height: 36),
                              const Divider(),
                              Row(children: [
                                CircleAvatar(
                                    radius: 16,
                                    child: Text(auth.initial,
                                        style: const TextStyle(fontSize: 12))),
                                const SizedBox(width: 12),
                                Expanded(
                                    child: Text(
                                        auth.displayName ?? 'Your account',
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis)),
                                TextButton(
                                    onPressed: auth.busy ? null : auth.signOut,
                                    child: const Text('Sign out'))
                              ]),
                              if (auth.error != null) Text(auth.error!),
                            ],
                          ],
                          if (_showLikes) ...[
                            BrowseHero(
                                title: 'Liked songs',
                                eyebrow: 'YOUR FAVOURITES',
                                metadata: _songCount(repo.likes.length),
                                artwork: (size) => _collectionArtwork(
                                    repo.likes, size,
                                    liked: true),
                                actions: [
                                  FilledButton.icon(
                                      onPressed: repo.likes.isEmpty
                                          ? null
                                          : () => unawaited(widget.player
                                              .startQueue(repo.likes, 0,
                                                  'Liked songs')),
                                      icon:
                                          const Icon(Icons.play_arrow_rounded),
                                      label: const Text('Play all'))
                                ]),
                            const SizedBox(height: 24),
                            if (!repo.loading && repo.likes.isEmpty)
                              const Text('No liked songs yet.'),
                          ],
                          if (selected != null) ...[
                            const SizedBox(height: 12),
                            BrowseHero(
                                title:
                                    selected['name']?.toString() ?? 'Playlist',
                                eyebrow: 'YOUR PLAYLIST',
                                metadata: [
                                  _songCount(selectedTracks.length),
                                  _createdLabel(selected)
                                ].where((s) => s.isNotEmpty).join(' · '),
                                artwork: (size) =>
                                    _collectionArtwork(selectedTracks, size),
                                actions: [
                                  FilledButton.icon(
                                      onPressed: selectedTracks.isEmpty
                                          ? null
                                          : () => unawaited(widget.player
                                              .startQueue(selectedTracks, 0,
                                                  '${selected['name'] ?? 'Playlist'}')),
                                      icon:
                                          const Icon(Icons.play_arrow_rounded),
                                      label: const Text('Play all')),
                                  if (repo.playlistWritesEnabled)
                                    TextButton.icon(
                                        onPressed: _writing
                                            ? null
                                            : () => unawaited(_addSongs(
                                                selected['id'].toString())),
                                        icon: const Icon(Icons.add),
                                        label: const Text('Add songs')),
                                  if (repoActions(selected) case final actions?)
                                    actions,
                                ]),
                            const SizedBox(height: 28),
                            CollectionSearchField(
                                controller: _playlistSearch,
                                label: 'Search this playlist',
                                onChanged: (_) => setState(() {})),
                            const SizedBox(height: 8),
                            Text(
                                '${visible.length} of ${_songCount(selectedTracks.length)}'),
                            if (selectedTracks.isEmpty)
                              const Text('This playlist is empty.'),
                            if (selectedTracks.isNotEmpty && visible.isEmpty)
                              const Text('No matching songs in this playlist.'),
                          ]
                        ]);
                  }));
        });
  }

  Widget _collectionArtwork(List<Map<String, dynamic>> tracks, double size,
      {bool liked = false}) {
    if (tracks.isEmpty)
      return Container(
          width: size,
          height: size,
          decoration: const BoxDecoration(
              gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [Color(0xff71856d), Color(0xff293b32)])),
          child: Icon(
              liked ? Icons.favorite_rounded : Icons.library_music_rounded,
              size: size * .32,
              color: NeatieDesign.accent));
    return HomeArtwork(
        api: widget.player.api,
        item: {'tracks': tracks.take(4).toList()},
        kind: 'mix',
        size: size);
  }

  Widget _libraryHeading(WebLibrary repo) =>
      LayoutBuilder(builder: (context, bounds) {
        final narrow = bounds.maxWidth < 640;
        return Padding(
            padding: const EdgeInsets.only(top: 26, bottom: 32),
            child:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                const Text('NEATIE / LIBRARY', style: NeatieDesign.eyebrow),
                const Spacer(),
                IconButton(
                    tooltip: 'Refresh library',
                    onPressed:
                        repo.loading ? null : () => unawaited(repo.refresh()),
                    icon: const Icon(Icons.refresh_rounded, size: 20))
              ]),
              const SizedBox(height: 16),
              Text('Your collection.',
                  style: NeatieDesign.display(narrow ? 48 : 76)),
              const SizedBox(height: 18),
              Text(
                  '${repo.likes.length} liked songs  /  ${repo.playlists.length} playlists',
                  style:
                      const TextStyle(fontSize: 12, color: NeatieDesign.muted)),
            ]));
      });

  Widget _likedCollection(WebLibrary repo) => ArtworkSurface(
      artwork: repo.likes.isEmpty
          ? null
          : (size) => _collectionArtwork(repo.likes, size, liked: true),
      child: Material(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(4),
          child: InkWell(
              onTap: () => setState(() => _showLikes = true),
              hoverColor: BrowsePalette.ink.withValues(alpha: .07),
              borderRadius: BorderRadius.circular(4),
              child: Padding(
                  padding: const EdgeInsets.all(28),
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Row(children: [
                          Icon(Icons.favorite_rounded,
                              size: 22, color: BrowsePalette.ink),
                          Spacer(),
                          Text('SAVED BY YOU',
                              style: TextStyle(
                                  fontSize: 9,
                                  letterSpacing: 2,
                                  color: BrowsePalette.muted))
                        ]),
                        const SizedBox(height: 32),
                        Text('Liked songs',
                            style: NeatieDesign.display(44)
                                .copyWith(color: BrowsePalette.ink)),
                        const SizedBox(height: 14),
                        Row(children: [
                          Expanded(
                              child: Text(
                                  '${repo.likes.length} favourites worth keeping.',
                                  style: const TextStyle(
                                      fontSize: 12,
                                      color: BrowsePalette.muted))),
                          const Icon(Icons.arrow_forward_rounded,
                              size: 28, color: BrowsePalette.ink)
                        ]),
                      ])))));

  void _openPlaylist(Map<String, dynamic> playlist) => setState(() {
        if (_selectedPlaylist != playlist['id']?.toString())
          _playlistSearch.clear();
        _selectedPlaylist = playlist['id']?.toString();
        widget.player.api.preloadCachedMedia(List<Map<String, dynamic>>.from(
            playlist['tracks'] as List? ?? const []));
      });

  Widget _playlistRow(Map<String, dynamic> playlist, {required int index}) =>
      ArtworkSurface(
          artwork: (size) => _collectionArtwork(
              List<Map<String, dynamic>>.from(
                  playlist['tracks'] as List? ?? const []),
              size),
          child: Material(
              color: Colors.transparent,
              borderRadius: BorderRadius.circular(4),
              child: InkWell(
                  borderRadius: BorderRadius.circular(4),
                  hoverColor: BrowsePalette.ink.withValues(alpha: .08),
                  onTap: () => _openPlaylist(playlist),
                  child: Semantics(
                      label:
                          'Open playlist: ${playlist['name'] ?? 'Untitled playlist'}',
                      excludeSemantics: true,
                      child: Padding(
                          padding: const EdgeInsets.all(14),
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                LayoutBuilder(
                                    builder: (_, bounds) => _collectionArtwork(
                                        List<Map<String, dynamic>>.from(
                                            playlist['tracks'] as List? ??
                                                const []),
                                        bounds.maxWidth)),
                                const SizedBox(height: 20),
                                Text(
                                    'COLLECTION / ${(index + 1).toString().padLeft(2, '0')}',
                                    style: const TextStyle(
                                        fontSize: 8,
                                        color: BrowsePalette.muted,
                                        letterSpacing: 1.8)),
                                const SizedBox(height: 10),
                                Text(
                                    playlist['name']?.toString() ??
                                        'Untitled playlist',
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                        fontSize: 18,
                                        fontWeight: FontWeight.w600,
                                        color: BrowsePalette.ink)),
                                const SizedBox(height: 4),
                                Text(
                                    _songCount((playlist['tracks'] as List? ??
                                            const [])
                                        .length),
                                    style: const TextStyle(
                                        fontSize: 11,
                                        color: BrowsePalette.muted)),
                                if (_createdLabel(playlist).isNotEmpty)
                                  Padding(
                                      padding: const EdgeInsets.only(top: 4),
                                      child: Text(_createdLabel(playlist),
                                          style: const TextStyle(
                                              fontSize: 10,
                                              color: BrowsePalette.muted))),
                              ]))))));

  Widget? repoActions(Map<String, dynamic> playlist) => widget
          .library!.playlistWritesEnabled
      ? Row(mainAxisSize: MainAxisSize.min, children: [
          IconButton(
              tooltip: 'Rename playlist',
              onPressed: _writing
                  ? null
                  : () async {
                      final owner = widget.library;
                      final name = await _nameDialog(
                          initial: playlist['name']?.toString(),
                          title: 'Rename playlist');
                      if (name != null)
                        await _write(
                            (r) => r.renamePlaylist(
                                playlist['id'].toString(), name,
                                expectedRevision: playlist['revision'] as int?),
                            owner: owner);
                    },
              icon: const Icon(Icons.edit_outlined)),
          IconButton(
              tooltip: 'Delete playlist',
              onPressed: _writing
                  ? null
                  : () async {
                      final owner = widget.library;
                      if (await _confirm('Delete playlist?',
                          'This will remove the playlist and its tracks.'))
                        await _write(
                            (r) => r.deletePlaylist(playlist['id'].toString(),
                                expectedRevision: playlist['revision'] as int?),
                            owner: owner);
                    },
              icon: const Icon(Icons.delete_outline))
        ])
      : null;
}

class _PlaylistNameDialog extends StatefulWidget {
  const _PlaylistNameDialog({required this.initial, required this.title});
  final String initial, title;
  @override
  State<_PlaylistNameDialog> createState() => _PlaylistNameDialogState();
}

class _PlaylistNameDialogState extends State<_PlaylistNameDialog> {
  late final _name = TextEditingController(text: widget.initial);
  void _save() {
    if (_name.text.trim().isNotEmpty) Navigator.pop(context, _name.text.trim());
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
          title: Text(widget.title),
          content: TextField(
              controller: _name,
              autofocus: true,
              maxLength: 120,
              decoration: const InputDecoration(labelText: 'Playlist name'),
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _save()),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel')),
            FilledButton(
                onPressed: _name.text.trim().isEmpty ? null : _save,
                child: const Text('Save')),
          ]);
  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }
}
