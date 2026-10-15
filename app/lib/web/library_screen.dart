import 'dart:async';

import 'package:flutter/material.dart';

import '../logic/track_metadata.dart';
import 'web_library.dart';
import 'web_player.dart';
import 'design_system.dart';
import 'web_auth.dart';
import 'account_screen.dart';
import 'collection_search.dart';

class LibraryScreen extends StatefulWidget {
  const LibraryScreen(
      {super.key,
      required this.library,
      required this.player,
      this.auth,
      required this.onSignIn});

  final WebLibrary? library;
  final WebPlayer player;
  final WebAuth? auth;
  final VoidCallback onSignIn;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  String? _selectedPlaylist;
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
      _playlistSearch.clear();
      _writeGeneration++;
      _writing = false;
      _writeError = null;
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
    return ListTile(
        title: Text(_trackTitle(track),
            maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(_trackSubtitle(track),
            maxLines: 1, overflow: TextOverflow.ellipsis),
        leading: _trackArtwork(track),
        onTap: playable
            ? () => unawaited(widget.player.startQueue(queue, index, 'Library'))
            : null,
        trailing: Row(mainAxisSize: MainAxisSize.min, children: [
          IconButton(
              tooltip: 'Play',
              onPressed: playable
                  ? () => unawaited(
                      widget.player.startQueue(queue, index, 'Library'))
                  : null,
              icon: const Icon(Icons.play_arrow)),
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
        ]));
  }

  Widget _trackArtwork(Map<String, dynamic> track) {
    final uri = widget.player.api.artworkUri(track['thumbnail']);
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

  @override
  Widget build(BuildContext context) {
    if (widget.auth?.passwordRecovery == true) {
      return AccountScreen(auth: widget.auth!);
    }
    final repo = widget.library;
    if (repo == null)
      return ListView(padding: const EdgeInsets.all(32), children: [
        const EditorialHeading(
            title: 'Keep what you love.',
            eyebrow: 'YOUR LIBRARY',
            subtitle: 'A home for your favourite songs and playlists.'),
        Align(
            alignment: Alignment.centerLeft,
            child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 540),
                child: EditorialSurface(
                    child: Padding(
                        padding: const EdgeInsets.all(28),
                        child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Icon(Icons.library_music_outlined,
                                  size: 36, color: NeatieDesign.accent),
                              const SizedBox(height: 24),
                              Text('Your music, together.',
                                  style: NeatieDesign.display(30)),
                              const SizedBox(height: 12),
                              const Text(
                                  'Sign in to see your library. Your liked songs and playlists are saved to your account.'),
                              const SizedBox(height: 24),
                              FilledButton(
                                  onPressed: widget.onSignIn,
                                  child: const Text('Sign in')),
                            ])))))
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
          return ListView(padding: const EdgeInsets.all(20), children: [
            if (widget.auth case final auth?) ...[
              EditorialSurface(
                  child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: Row(children: [
                        CircleAvatar(
                            radius: 27,
                            foregroundImage: auth.avatarUrl == null
                                ? null
                                : NetworkImage(auth.avatarUrl!),
                            onForegroundImageError:
                                auth.avatarUrl == null ? null : (_, __) {},
                            child: Text(auth.initial)),
                        const SizedBox(width: 16),
                        Expanded(
                            child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                              Text(
                                  auth.displayName?.trim().isNotEmpty == true
                                      ? auth.displayName!
                                      : 'Your library',
                                  style:
                                      Theme.of(context).textTheme.titleLarge),
                              if (auth.email != null)
                                SelectableText(auth.email!),
                              if (auth.error != null) Text(auth.error!),
                            ])),
                        const SizedBox(width: 8),
                        TextButton(
                            onPressed: auth.busy ? null : auth.signOut,
                            child: const Text('Sign out')),
                      ]))),
              const SizedBox(height: 24),
            ],
            EditorialHeading(
                title: 'Keep what you love.',
                eyebrow: 'YOUR LIBRARY',
                subtitle: 'Your favourite songs and playlists, all here.',
                action: IconButton.outlined(
                    tooltip: 'Refresh library',
                    onPressed:
                        repo.loading ? null : () => unawaited(repo.refresh()),
                    icon: const Icon(Icons.refresh))),
            if (repo.loading)
              const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: NeatieLoading(
                      label: 'Opening your library', showLabel: true)),
            if (repo.error != null) ...[
              Text(repo.error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error)),
              TextButton(
                  onPressed: () => unawaited(repo.refresh()),
                  child: const Text('Retry'))
            ],
            if (_writeError != null)
              Text(_writeError!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error)),
            if (!repo.playlistWritesEnabled)
              const Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                      'Playlist editing is currently disabled on the web.')),
            _heading('Liked songs'),
            if (!repo.loading && repo.likes.isEmpty)
              const Text('No liked songs yet.'),
            ...[
              for (var i = 0; i < repo.likes.length; i++)
                _trackTile(repo.likes[i], repo.likes, i,
                    remove: () =>
                        _write((r) => r.setLiked(repo.likes[i], false)))
            ],
            Row(children: [
              Expanded(child: _heading('Playlists')),
              if (repo.playlistWritesEnabled)
                IconButton(
                    tooltip: 'Create playlist',
                    onPressed: _writing
                        ? null
                        : () async {
                            final name =
                                await _nameDialog(title: 'New playlist');
                            if (name != null)
                              await _write((r) => r.createPlaylist(name),
                                  owner: repo);
                          },
                    icon: const Icon(Icons.add))
            ]),
            if (repo.playlists.isEmpty) const Text('No playlists yet.'),
            for (final playlist in repo.playlists) _playlistRow(playlist),
            if (selected != null) ...[
              Row(children: [
                Expanded(
                    child:
                        _heading(selected['name']?.toString() ?? 'Playlist')),
                if (repo.playlistWritesEnabled)
                  TextButton.icon(
                      onPressed: _writing
                          ? null
                          : () =>
                              unawaited(_addSongs(selected['id'].toString())),
                      icon: const Icon(Icons.add),
                      label: const Text('Add songs'))
              ]),
              CollectionSearchField(
                  controller: _playlistSearch,
                  label: 'Search this playlist',
                  onChanged: (_) => setState(() {})),
              const SizedBox(height: 8),
              Text('${visible.length} of ${selectedTracks.length} songs'),
              if (selectedTracks.isEmpty) const Text('This playlist is empty.'),
              if (selectedTracks.isNotEmpty && visible.isEmpty)
                const Text('No matching songs in this playlist.'),
              ...[
                for (final i in visible)
                  _trackTile(selectedTracks[i], selectedTracks, i,
                      remove: repo.playlistWritesEnabled
                          ? () => _write((r) => r.removeTrack(selected['id'].toString(), '${selectedTracks[i]['playlist_entry_id']}', expectedRevision: selected['revision'] as int?),
                              owner: repo)
                          : null,
                      moveUp: repo.playlistWritesEnabled && i > 0
                          ? () => _write((r) => r.moveTrack(selected['id'].toString(), '${selectedTracks[i]['playlist_entry_id']}', i - 1, expectedRevision: selected['revision'] as int?),
                              owner: repo)
                          : null,
                      moveDown: repo.playlistWritesEnabled && i + 1 < selectedTracks.length
                          ? () => _write(
                              (r) => r.moveTrack(
                                  selected['id'].toString(),
                                  '${selectedTracks[i]['playlist_entry_id']}',
                                  i + 1,
                                  expectedRevision: selected['revision'] as int?),
                              owner: repo)
                          : null)
              ]
            ]
          ]);
        });
  }

  Widget _heading(String text) => Padding(
      padding: const EdgeInsets.only(top: 22, bottom: 8),
      child: Text(text, style: Theme.of(context).textTheme.titleLarge));

  Widget _playlistRow(Map<String, dynamic> playlist) => ListTile(
      title: Text(playlist['name']?.toString() ?? 'Untitled playlist'),
      subtitle:
          Text('${(playlist['tracks'] as List? ?? const []).length} songs'),
      selected: playlist['id'] == _selectedPlaylist,
      onTap: () => setState(() {
            if (_selectedPlaylist != playlist['id']?.toString())
              _playlistSearch.clear();
            _selectedPlaylist = playlist['id']?.toString();
          }),
      trailing: repoActions(playlist));

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
