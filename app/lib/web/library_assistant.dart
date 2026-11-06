import 'dart:async';

import 'package:flutter/material.dart';

import '../logic/assistant_messages.dart';
import '../logic/track_metadata.dart';
import 'design_system.dart';
import 'home_widgets.dart' show HomeArtwork;
import 'web_library.dart';
import 'web_player.dart';

/// Uses the same assistant payload and playlist editor as the other clients.
class LibraryAssistant extends StatefulWidget {
  const LibraryAssistant(
      {super.key, required this.library, required this.player});

  final WebLibrary library;
  final WebPlayer player;

  @override
  State<LibraryAssistant> createState() => _LibraryAssistantState();
}

class _LibraryAssistantState extends State<LibraryAssistant> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _messages = <AssistantMessage>[];
  final _createdDrafts = <String>{};
  late final _scope = widget.player.api.userScopeId;
  String? _session;
  String? _notice;
  bool _busy = false;
  int _nextId = 0;
  Completer<void>? _request;
  WebLibrary get _library => widget.library;
  bool get _current => mounted && widget.player.api.userScopeId == _scope;

  @override
  void dispose() {
    _request?.complete();
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _send([String? shortcut]) async {
    final text = (shortcut ?? _input.text).trim();
    if (!_current || _busy || text.isEmpty) return;
    final last = _messages.where((turn) => turn.role == 'assistant').lastOrNull;
    setState(() {
      _busy = true;
      _notice = null;
      _messages
          .add(AssistantMessage(id: '${_nextId++}', role: 'user', text: text));
    });
    _input.clear();
    _toBottom();
    try {
      final edit = _parseEdit(text);
      if (edit != null) {
        await _confirmEdit(edit);
        return;
      }
      final cancel = _request = Completer<void>();
      final payload = await widget.player.api.post(
          '/assistant/chat',
          {
            'message': text,
            'user_scope_id': _scope,
            'session_id': _session,
            'thinking_mode': false,
            'playlist_summaries': [
              for (final row in _library.playlists.take(100))
                AssistantPlaylistSummary(
                  id: '${row['id']}',
                  name: '${row['name']}',
                  trackCount: (row['tracks'] as List? ?? const []).length,
                ).toJson(),
            ],
            'last_assistant_tracks': last?.tracks
                    .take(10)
                    .map((card) => card.toContextJson())
                    .toList() ??
                [],
            'last_playlist_draft_tracks': last?.playlistDraft?.tracks
                    .take(10)
                    .map((card) => card.toContextJson())
                    .toList() ??
                [],
          },
          timeout: const Duration(seconds: 60),
          cancel: cancel.future);
      if (!_current) return;
      final answer =
          AssistantMessage.fromPayload(id: '${_nextId++}', payload: payload);
      setState(() {
        _session = payload['session_id'] as String?;
        _messages.add(answer);
        if (_messages.length > 24)
          _messages.removeRange(0, _messages.length - 24);
      });
    } catch (_) {
      if (_current) {
        setState(() => _notice = 'Could not complete that request. Try again.');
        if (_input.text.isEmpty) _input.text = text;
      }
    } finally {
      _request = null;
      if (_current) {
        setState(() => _busy = false);
        _toBottom();
      }
    }
  }

  void _toBottom() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _scroll.hasClients)
          _scroll.jumpTo(_scroll.position.maxScrollExtent);
      });

  Future<bool> _confirm(String title, String details) async =>
      await showDialog<bool>(
            context: context,
            builder: (context) => AlertDialog(
              title: Text(title),
              content: Text(details),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel')),
                FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('Save')),
              ],
            ),
          ) ==
          true &&
      _current;

  Map<String, dynamic>? _playlist(String id) =>
      _library.playlists.where((row) => row['id'] == id).firstOrNull;

  Future<void> _saveTracks(AssistantMessage answer) async {
    if (!_current || _busy || !_library.playlistWritesEnabled) return;
    final draft = answer.playlistDraft;
    final target = answer.targetPlaylist;
    final playlist = target == null ? null : _playlist(target.id);
    if (draft == null && playlist == null) return;
    final existing = {
      for (final track in (playlist?['tracks'] as List? ?? const [])
          .whereType<Map<String, dynamic>>())
        extractTrackId(track),
    };
    final tracks = [
      for (final card in (draft?.tracks ?? answer.tracks).take(20))
        if (extractTrackId(card.track) case final String id
            when existing.add(id))
          card.track,
    ];
    if (tracks.isEmpty) {
      setState(() => _notice = 'Those songs are already in this playlist.');
      return;
    }
    final name = draft?.name ?? '${playlist!['name']}';
    final initialRevision = (playlist?['revision'] as num?)?.toInt();
    setState(() {
      _busy = true;
      _notice = null;
    });
    var added = 0;
    try {
      if (!await _confirm(draft != null ? 'Create $name?' : 'Add to $name?',
          tracks.map((track) => '${track['title']}').join('\n'))) return;
      final id = draft == null
          ? target!.id
          : await _library.createPlaylist(name, initialTrack: tracks.first);
      if (draft != null) {
        _createdDrafts.add(answer.id);
        added = 1;
      }
      var expected = draft != null
          ? (_playlist(id)?['revision'] as num?)?.toInt()
          : initialRevision;
      for (final track in tracks.skip(added)) {
        if (!_current || expected == null) break;
        await _library.addTrack(id, track, expectedRevision: expected);
        expected++;
        added++;
      }
      if (_current)
        setState(() => _notice = added == tracks.length
            ? 'Saved $added ${added == 1 ? 'song' : 'songs'} to $name.'
            : 'Saved $added of ${tracks.length} songs. Open the playlist to review.');
    } catch (_) {
      if (_current)
        setState(() => _notice =
            'Saved $added of ${tracks.length} songs before the edit stopped. Refresh the playlist to verify.');
    } finally {
      if (_current) setState(() => _busy = false);
    }
  }

  // Resolve explicit edit commands against the loaded account, never model text
  // or a model-supplied entry ID. Ambiguous names cannot produce a write.
  (String, String, String, String)? _parseEdit(String input) {
    for (final (verb, pattern) in [
      ('rename', r'^rename (?:playlist )?"([^"]+)" to "([^"]+)"$'),
      ('remove', r'^remove "([^"]+)" from "([^"]+)"$'),
      ('move', r'^move "([^"]+)" in "([^"]+)" to (?:position )?(\d+)$'),
    ]) {
      final match = RegExp(pattern, caseSensitive: false).firstMatch(input);
      if (match != null)
        return (
          verb,
          match[1]!,
          match[2]!,
          match.groupCount == 3 ? match[3]! : ''
        );
    }
    return null;
  }

  Future<void> _confirmEdit((String, String, String, String) edit) async {
    final (verb, first, second, position) = edit;
    final name = verb == 'rename' ? first : second;
    final matches = _library.playlists
        .where((row) =>
            '${row['name']}'.trim().toLowerCase() == name.trim().toLowerCase())
        .toList();
    if (!_library.playlistWritesEnabled || matches.length != 1) {
      setState(
          () => _notice = 'Choose an editable playlist with a unique name.');
      return;
    }
    final playlist = matches.single;
    final id = '${playlist['id']}';
    final revision = (playlist['revision'] as num?)?.toInt();
    if (revision == null) {
      setState(() => _notice = 'Refresh the playlist before editing.');
      return;
    }
    if (verb == 'rename') {
      if (!await _confirm('Rename $name?', 'New name: $second')) return;
      await _library.renamePlaylist(id, second, expectedRevision: revision);
    } else {
      final tracks =
          (playlist['tracks'] as List? ?? const []).whereType<Map>().toList();
      final matches = tracks
          .where((track) =>
              '${track['title']}'.trim().toLowerCase() ==
              first.trim().toLowerCase())
          .toList();
      final entry =
          matches.length == 1 ? matches.single['playlist_entry_id'] : null;
      final index = (int.tryParse(position) ?? 0) - 1;
      if (entry is! String ||
          (verb == 'move' && (index < 0 || index >= tracks.length))) {
        setState(() =>
            _notice = 'Use a unique song title and a valid playlist position.');
        return;
      }
      if (!await _confirm(
          verb == 'remove' ? 'Remove $first?' : 'Move $first?',
          verb == 'remove'
              ? 'Remove from $name.'
              : 'Move to position $position in $name.')) return;
      if (verb == 'remove') {
        await _library.removeTrack(id, entry, expectedRevision: revision);
      } else {
        await _library.moveTrack(id, entry, index, expectedRevision: revision);
      }
    }
    if (_current) setState(() => _notice = 'Playlist updated.');
  }

  Widget _answer(AssistantMessage turn) {
    final draft = turn.playlistDraft;
    final target = turn.targetPlaylist;
    final cards = draft?.tracks ?? turn.tracks;
    final canSave = _library.playlistWritesEnabled &&
        cards.isNotEmpty &&
        !_createdDrafts.contains(turn.id) &&
        (draft != null ||
            turn.actionType == 'add_to_playlist' && target != null);
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(turn.role == 'user' ? 'You' : 'Neatie', style: NeatieDesign.eyebrow),
      const SizedBox(height: 6),
      SelectableText(turn.text),
      if (draft != null) ...[
        const SizedBox(height: 8),
        Text(draft.name, style: const TextStyle(fontWeight: FontWeight.bold)),
      ],
      for (final card in cards.take(20))
        if (extractTrackId(card.track) != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: HomeArtwork(
                api: widget.player.api,
                item: card.track,
                kind: 'track',
                size: 38),
            title: Text('${card.track['title']}'),
            subtitle:
                Text('${card.track['artist'] ?? card.track['channel'] ?? ''}'),
            trailing: const Icon(Icons.play_arrow_rounded),
            onTap: () {
              if (_current) unawaited(widget.player.start(card.track));
            },
          ),
      if (canSave)
        FilledButton(
            onPressed: _busy ? null : () => _saveTracks(turn),
            child:
                Text(draft != null ? 'Review playlist' : 'Review additions')),
      if (turn.actionType == 'needs_playlist_choice')
        for (final option in turn.playlistOptions)
          TextButton(
              onPressed: _busy
                  ? null
                  : () => _send('Add those songs to "${option.name}"'),
              child: Text(option.name)),
    ]);
  }

  @override
  Widget build(BuildContext context) => SizedBox(
        width: 620,
        height: MediaQuery.sizeOf(context).height * .82,
        child: Padding(
            padding: const EdgeInsets.all(22),
            child: Column(children: [
              Row(children: [
                Expanded(
                    child: Text('Ask Neatie', style: NeatieDesign.display(30))),
                IconButton(
                    tooltip: 'Close chat',
                    onPressed: () => Navigator.pop(context),
                    icon: const Icon(Icons.close)),
              ]),
              Expanded(
                  child: ListView.separated(
                controller: _scroll,
                itemCount: _messages.length + 1,
                separatorBuilder: (_, __) => const SizedBox(height: 18),
                itemBuilder: (_, index) => index == 0
                    ? const Text(
                        'Ask for songs or a playlist. To edit one, try Rename "Old name" to "New name", Remove "Song" from "Playlist", or Move "Song" in "Playlist" to 2.')
                    : _answer(_messages[index - 1]),
              )),
              if (_busy) const LinearProgressIndicator(),
              if (_notice != null)
                Semantics(
                    liveRegion: true,
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 8),
                      child: Text(_notice!),
                    )),
              TextField(
                  controller: _input,
                  maxLength: 2000,
                  onSubmitted: (_) => _send(),
                  decoration: InputDecoration(
                      hintText: 'Ask for music or a playlist change',
                      counterText: '',
                      suffixIcon: IconButton(
                          tooltip: 'Send message',
                          onPressed: _busy ? null : () => _send(),
                          icon: const Icon(Icons.arrow_upward)))),
            ])),
      );
}
