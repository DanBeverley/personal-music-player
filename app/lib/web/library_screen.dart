import 'dart:async';

import 'package:flutter/material.dart';

import '../logic/track_metadata.dart';
import 'web_library.dart';
import 'web_player.dart';
import 'design_system.dart';

class LibraryScreen extends StatefulWidget {
  const LibraryScreen(
      {super.key,
      required this.library,
      required this.player,
      required this.onSignIn});

  final WebLibrary? library;
  final WebPlayer player;
  final VoidCallback onSignIn;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  String? _selectedPlaylist;
  String? _writeError;
  bool _writing = false;

  @override
  void didUpdateWidget(covariant LibraryScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.library, widget.library)) _selectedPlaylist = null;
  }

  Future<void> _write(Future<void> Function(WebLibrary repo) action) async {
    final repo = widget.library;
    if (repo == null || _writing) return;
    setState(() {
      _writing = true;
      _writeError = null;
    });
    try {
      await action(repo);
    } catch (_) {
      if (mounted && identical(repo, widget.library))
        setState(() =>
            _writeError = repo.error ?? 'Could not save this change. Retry.');
    } finally {
      if (mounted) setState(() => _writing = false);
    }
  }

  Future<String?> _nameDialog({String? initial, required String title}) async {
    final controller = TextEditingController(text: initial ?? '');
    final value = await showDialog<String>(
        context: context,
        builder: (context) => AlertDialog(
                title: Text(title),
                content: TextField(
                    controller: controller,
                    autofocus: true,
                    maxLength: 120,
                    decoration:
                        const InputDecoration(labelText: 'Playlist name'),
                    onSubmitted: (_) =>
                        Navigator.pop(context, controller.text)),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context),
                      child: const Text('Cancel')),
                  FilledButton(
                      onPressed: () => Navigator.pop(context, controller.text),
                      child: const Text('Save'))
                ]));
    controller.dispose();
    return value;
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
      {VoidCallback? remove}) {
    final playable = extractTrackId(track)?.isNotEmpty == true;
    return ListTile(
        title: Text(_trackTitle(track),
            maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(_trackSubtitle(track)),
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
          if (remove != null)
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
    final selected =
        repo.playlists.where((p) => p['id'] == _selectedPlaylist).firstOrNull;
    if (_selectedPlaylist != null && selected == null) _selectedPlaylist = null;
    final selectedTracks = selected == null
        ? const <Map<String, dynamic>>[]
        : List<Map<String, dynamic>>.from(
            selected['tracks'] as List? ?? const []);
    return AnimatedBuilder(
        animation: repo,
        builder: (context, _) =>
            ListView(padding: const EdgeInsets.all(20), children: [
              EditorialHeading(
                  title: 'Keep what you love.',
                  eyebrow: 'YOUR LIBRARY',
                  subtitle: 'Your favourite songs and playlists, all here.',
                  action: IconButton.outlined(
                      tooltip: 'Refresh library',
                      onPressed:
                          repo.loading ? null : () => unawaited(repo.refresh()),
                      icon: const Icon(Icons.refresh))),
              if (repo.loading) const LinearProgressIndicator(),
              if (repo.error != null) ...[
                Text(repo.error!,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
                TextButton(
                    onPressed: () => unawaited(repo.refresh()),
                    child: const Text('Retry'))
              ],
              if (_writeError != null)
                Text(_writeError!,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
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
                                await _write((r) => r.createPlaylist(name));
                            },
                      icon: const Icon(Icons.add))
              ]),
              if (repo.playlists.isEmpty) const Text('No playlists yet.'),
              for (final playlist in repo.playlists) _playlistRow(playlist),
              if (selected != null) ...[
                _heading(selected['name']?.toString() ?? 'Playlist'),
                if (selectedTracks.isEmpty)
                  const Text('This playlist is empty.'),
                ...[
                  for (var i = 0; i < selectedTracks.length; i++)
                    _trackTile(selectedTracks[i], selectedTracks, i,
                        remove: repo.playlistWritesEnabled
                            ? () => _write((r) => r.removeTrack(
                                selected['id'].toString(),
                                extractTrackId(selectedTracks[i])!))
                            : null)
                ]
              ]
            ]));
  }

  Widget _heading(String text) => Padding(
      padding: const EdgeInsets.only(top: 22, bottom: 8),
      child: Text(text, style: Theme.of(context).textTheme.titleLarge));

  Widget _playlistRow(Map<String, dynamic> playlist) => ListTile(
      title: Text(playlist['name']?.toString() ?? 'Untitled playlist'),
      subtitle:
          Text('${(playlist['tracks'] as List? ?? const []).length} songs'),
      selected: playlist['id'] == _selectedPlaylist,
      onTap: () =>
          setState(() => _selectedPlaylist = playlist['id']?.toString()),
      trailing: repoActions(playlist));

  Widget? repoActions(Map<String, dynamic> playlist) => widget
          .library!.playlistWritesEnabled
      ? Row(mainAxisSize: MainAxisSize.min, children: [
          IconButton(
              tooltip: 'Rename playlist',
              onPressed: _writing
                  ? null
                  : () async {
                      final name = await _nameDialog(
                          initial: playlist['name']?.toString(),
                          title: 'Rename playlist');
                      if (name != null)
                        await _write((r) =>
                            r.renamePlaylist(playlist['id'].toString(), name));
                    },
              icon: const Icon(Icons.edit_outlined)),
          IconButton(
              tooltip: 'Delete playlist',
              onPressed: _writing
                  ? null
                  : () async {
                      if (await _confirm('Delete playlist?',
                          'This will remove the playlist and its tracks.'))
                        await _write(
                            (r) => r.deletePlaylist(playlist['id'].toString()));
                    },
              icon: const Icon(Icons.delete_outline))
        ])
      : null;
}
