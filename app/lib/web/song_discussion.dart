import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import '../logic/track_metadata.dart';
import 'design_system.dart';
import 'music_api.dart';
import 'web_player.dart';
import 'artwork_image.dart';
import 'playback_port.dart';
import 'home_widgets.dart' show HomeArtwork;

/// Song-scoped conversation. Playback and queue changes go through WebPlayer.
class SongDiscussion extends StatefulWidget {
  const SongDiscussion(
      {super.key,
      required this.player,
      required this.onClose,
      this.storeForScope});
  final WebPlayer player;
  final VoidCallback onClose;
  final SessionStore? Function(String scope)? storeForScope;
  @override
  State<SongDiscussion> createState() => _SongDiscussionState();
}

class _SongDiscussionState extends State<SongDiscussion>
    with WidgetsBindingObserver {
  final _input = TextEditingController();
  final _focus = FocusNode();
  final _scroll = ScrollController();
  final _turns = <Map<String, dynamic>>[];
  Completer<void>? _abort;
  late String _selection;
  late SongConversations _saved;
  late String _song;
  String? _error, _pendingQuestion;
  Map<String, dynamic>? _freshReply;
  static const _ink = Color(0xffdedfca), _muted = Color(0xff97a79f);
  bool get _reduced => MediaQuery.disableAnimationsOf(context);
  String get _identity =>
      '${widget.player.api.userScopeId}|${extractTrackId(widget.player.current)}';
  bool get _busy => _abort != null;
  String _short(Object? value, int limit) {
    final text = '${value ?? ''}';
    return text.substring(0, text.length.clamp(0, limit));
  }

  List<Map<String, String>> _history() {
    var remaining = 10000;
    final history = <Map<String, String>>[];
    for (final turn in _turns.reversed.take(8)) {
      if (remaining <= 0) break;
      final text = _short(turn['content'], remaining);
      history.add({'role': turn['role'], 'content': text});
      remaining -= text.length;
    }
    return history.reversed.toList();
  }

  @override
  void initState() {
    super.initState();
    _selection = _identity;
    final scope = widget.player.api.userScopeId;
    _saved = SongConversations(scope, widget.storeForScope?.call(scope));
    _restoreConversation();
    widget.player.addListener(_changed);
    WidgetsBinding.instance.addObserver(this);
  }

  void _restoreConversation() {
    final scope = widget.player.api.userScopeId;
    if (_saved.scope != scope) {
      _saved = SongConversations(scope, widget.storeForScope?.call(scope));
    }
    _song = extractTrackId(widget.player.current) ?? '';
    _freshReply = null;
    final saved = _saved.read(_song);
    _turns
      ..clear()
      ..addAll((saved['turns'] as List? ?? []).cast<Map<String, dynamic>>());
    _input.text = '${saved['draft'] ?? ''}';
    _toBottom();
  }

  void _saveConversation() => _saved.save(_song, _turns,
      _input.text.isEmpty ? _pendingQuestion ?? '' : _input.text);

  void _cancel({bool restore = false}) {
    final abort = _abort;
    _abort = null;
    if (restore && _input.text.isEmpty && _pendingQuestion != null) {
      _input.text = _pendingQuestion!;
    }
    _pendingQuestion = null;
    if (abort != null && !abort.isCompleted) abort.complete();
  }

  void _changed() {
    if (_selection == _identity) return; // Position ticks do no UI work.
    _cancel(restore: true);
    _saveConversation();
    _selection = _identity;
    setState(() {
      _restoreConversation();
      _error = null;
    });
  }

  @override
  void didUpdateWidget(covariant SongDiscussion oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.player != widget.player) {
      oldWidget.player.removeListener(_changed);
      widget.player.addListener(_changed);
      _changed();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed && _busy) {
      _cancel(restore: true);
      _saveConversation();
      setState(() => _error = 'Request cancelled. Your draft is kept below.');
    }
  }

  void _toBottom() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _scroll.hasClients) {
          if (_reduced) {
            _scroll.jumpTo(_scroll.position.maxScrollExtent);
            return;
          }
          _scroll.animateTo(_scroll.position.maxScrollExtent,
              duration: const Duration(milliseconds: 200),
              curve: Curves.easeOut);
        }
      });

  String _queueTitle(Object? text) => '$text'
      .toLowerCase()
      .replaceAll(RegExp(r'\s*\((?:\d{4} )?remaster(?:ed)?\)$'), '')
      .replaceAll(RegExp(r'[^\p{L}\p{N}]+', unicode: true), ' ')
      .trim();

  String? _queueCommand(String message) {
    final command = RegExp(
      r'^(?:please\s+|(?:can|could|would) you\s+)?(add|queue|put|move|remove)\s+(.+?)[.!?]*$',
      caseSensitive: false,
    ).firstMatch(message);
    if (command == null) return null;
    final verb = command[1]!.toLowerCase();
    final player = widget.player;
    final revision = player.queueRevision;
    final upcoming = player.queue.skip(player.index + 1).toList();
    final previous = _turns.reversed.firstWhere(
        (turn) => (turn['tracks'] as List?)?.isNotEmpty == true,
        orElse: () => {});
    final suggestions = (previous['tracks'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
    Map<String, dynamic>? find(
        String title, Iterable<Map<String, dynamic>> rows) {
      final wanted = _queueTitle(title);
      final matches = {
        for (final row in rows)
          if (extractTrackId(row) != null &&
              _queueTitle(row['title']) == wanted)
            extractTrackId(row): row,
      };
      if (matches.isEmpty && wanted.length >= 10) {
        for (final row in rows) {
          final id = extractTrackId(row);
          if (id != null && _queueTitle(row['title']) == '${wanted}s') {
            matches[id] = row;
          }
        }
      }
      return matches.length == 1 ? matches.values.single : null;
    }

    var target = command[2]!;
    final after = RegExp(r'\s+(?:right\s+)?after\s+(.+)$', caseSensitive: false)
        .firstMatch(target);
    String? anchorId;
    bool anchorNext = false;
    final next = after == null &&
        RegExp(r'\s+(?:to play )?next$', caseSensitive: false).hasMatch(target);
    if (after != null) {
      target = target.substring(0, after.start);
      var anchorTitle = after[1]!;
      final nextClause = RegExp(
        r'\s+and\s+(.+?)\s+(?:will be played|should play|plays?|goes?)\s+next$',
        caseSensitive: false,
      ).firstMatch(anchorTitle);
      if (nextClause != null) {
        anchorTitle = anchorTitle.substring(0, nextClause.start);
        if (_queueTitle(nextClause[1]) != _queueTitle(anchorTitle)) {
          return 'Which song should go next? Name the same song after “after”.';
        }
        anchorNext = true;
      }
      anchorId = extractTrackId(find(anchorTitle, upcoming));
      if (anchorId == null)
        return 'I couldn’t identify one upcoming “$anchorTitle”. The queue is unchanged.';
    } else {
      target = target.replaceFirst(
          RegExp(
              r'\s+(?:(?:in)?to|from)\s+(?:(?:the|my|our)\s+)?(?:current\s+)?queue$',
              caseSensitive: false),
          '');
      target = target.replaceFirst(
          RegExp(r'\s+(?:to play )?next$', caseSensitive: false), '');
    }
    final all = RegExp(
            r'^(?:(?:all of )?(?:those|these|them|the recommended)(?: songs| tracks)?|all)$',
            caseSensitive: false)
        .hasMatch(target);
    final rows = verb == 'move' || verb == 'remove'
        ? upcoming
        : [...suggestions, ...upcoming];
    var selected = all && verb != 'move' && verb != 'remove'
        ? suggestions
        : [if (find(target, rows) case final row?) row];
    if (selected.isEmpty) {
      return 'I couldn’t match “$target” to one song in the recommendations or upcoming queue. Try its exact title.';
    }
    if (verb != 'move' && verb != 'remove' && !next && after == null) {
      final queued = player.queue.map(extractTrackId).toSet();
      selected = selected
          .where((track) => !queued.contains(extractTrackId(track)))
          .toList();
      if (selected.isEmpty) return 'Those songs are already in the queue.';
    }
    final title = selected.length == 1
        ? '“${selected.single['title']}”'
        : '${selected.length} songs';
    if (verb == 'remove') {
      final removed = player.removeQueueItem(
          player.queue.indexOf(selected.single),
          revision: revision);
      return removed
          ? 'Removed $title from the queue.'
          : 'The queue changed. Try again.';
    }
    final saved = player.placeUpcoming(selected,
        revision: revision,
        after: anchorId,
        atEnd: !next && after == null,
        anchorNext: anchorNext);
    if (!saved)
      return 'I couldn’t place $title in the current queue. Try again.';
    if (after != null) {
      final anchor = player.queue
          .firstWhere((row) => extractTrackId(row) == anchorId)['title'];
      return anchorNext
          ? '“$anchor” plays next, followed by $title.'
          : 'Placed $title after “$anchor”.';
    }
    return next
        ? 'Placed $title next in the queue.'
        : 'Placed $title at the end of the queue.';
  }

  void _acceptReply(String question, Map<String, dynamic> turn) {
    final following = !_scroll.hasClients ||
        _scroll.position.maxScrollExtent - _scroll.offset < 110;
    setState(() {
      _freshReply = turn;
      _turns.addAll([
        {'role': 'user', 'content': question},
        turn
      ]);
      if (_turns.length > SongConversations.maxTurns) {
        _turns.removeRange(0, _turns.length - SongConversations.maxTurns);
      }
      _pendingQuestion = null;
      _abort = null;
    });
    _saveConversation();
    if (following) _toBottom();
  }

  Future<void> _send([String? suggestion]) async {
    final text = (suggestion ?? _input.text).trim(),
        track = widget.player.current;
    if (_busy || text.isEmpty || text.length > 2000 || track == null) return;
    final selection = _identity, api = widget.player.api;
    final queueReply = _queueCommand(text);
    if (queueReply != null) {
      _input.clear();
      _error = null;
      _acceptReply(text, {'role': 'assistant', 'content': queueReply});
      return;
    }
    final abort = Completer<void>();
    final history = _history();
    final lines = widget.player.cachedLyrics(track)?['lines'];
    var remaining = 3600;
    final contextLines = <Map<String, String>>[];
    if (lines is List) {
      for (final line in lines.whereType<Map>().take(256)) {
        if (remaining <= 0) break;
        final text = _short(line['text'], remaining.clamp(0, 500));
        if (text.isEmpty) continue;
        contextLines.add({'text': text});
        remaining -= text.length;
      }
    }
    setState(() {
      _abort = abort;
      _pendingQuestion = text;
      _error = null;
    });
    _input.clear();
    _toBottom();
    try {
      final answer = await api.post(
          '/assistant/chat',
          {
            'message': text,
            'user_scope_id': api.userScopeId,
            'conversation': history,
            'song': {
              'video_id': extractTrackId(track),
              'title': _short(track['title'], 180),
              'artist': _short(
                  [track['artist'], track['channel'], track['author']]
                      .whereType<String>()
                      .firstWhere(
                          (name) =>
                              name.trim().isNotEmpty &&
                              name.trim().toLowerCase() != 'unknown artist',
                          orElse: () => ''),
                  180),
              if (track['album'] is String)
                'album': _short(track['album'], 180),
              'duration': trackDurationSeconds(track).round().clamp(0, 86400),
              if (track['playback'] is Map &&
                  track['playback']['source_id'] is String)
                'source_id': track['playback']['source_id'],
              'research': true,
              'lines': contextLines,
            },
          },
          timeout: const Duration(seconds: 45),
          cancel: abort.future);
      if (!mounted || _abort != abort || selection != _identity) return;
      final reply = answer['reply'];
      if (reply is! String || reply.trim().isEmpty)
        throw const FormatException();
      final cited = answer['source_links'] as List? ?? const [];
      final contextOnly = cited.isEmpty;
      final sources =
          (contextOnly ? answer['context_links'] as List? ?? const [] : cited)
              .whereType<Map>()
              .where((item) {
                final uri = Uri.tryParse('${item['url'] ?? ''}');
                return uri != null &&
                    ['https', 'http'].contains(uri.scheme) &&
                    uri.host.isNotEmpty &&
                    uri.userInfo.isEmpty;
              })
              .take(5)
              .toList();
      _acceptReply(text, {
        'role': 'assistant',
        'content': reply.substring(0, reply.length.clamp(0, 4000)),
        'sources': sources,
        'contextOnly': contextOnly,
        'hasLyrics': answer['has_lyrics'] == true,
        'tracks': MusicApi.tracks(answer['tracks']).take(4).toList(),
      });
    } catch (error) {
      if (!mounted || _abort != abort || selection != _identity) return;
      if (_input.text.isEmpty) _input.text = text;
      setState(() {
        _abort = null;
        _pendingQuestion = null;
        _error = error is SongDiscussionException
            ? error.message
            : 'Could not get an answer. Your question is kept below; try again.';
      });
    }
  }

  @override
  void dispose() {
    _cancel(restore: true);
    _saveConversation();
    widget.player.removeListener(_changed);
    WidgetsBinding.instance.removeObserver(this);
    _input.dispose();
    _focus.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Widget _arrival(Widget child, Object key) => TweenAnimationBuilder<double>(
      key: ValueKey(key),
      tween: Tween(begin: _reduced ? 1 : 0, end: 1),
      duration: _reduced ? Duration.zero : const Duration(milliseconds: 240),
      curve: Curves.easeOutCubic,
      builder: (_, value, child) => Opacity(
          opacity: value,
          child: Transform.translate(
              offset: Offset(0, 10 * (1 - value)), child: child)),
      child: child);

  Widget _suggestion(String label, String question, IconData icon) => Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
          color: const Color(0xff1c2521),
          borderRadius: BorderRadius.circular(14),
          child: InkWell(
              borderRadius: BorderRadius.circular(14),
              onTap: () => unawaited(_send(question)),
              child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 15),
                  child: Row(children: [
                    Icon(icon, size: 18, color: _ink),
                    const SizedBox(width: 14),
                    Expanded(
                        child: Text(label,
                            style: const TextStyle(fontSize: 13, color: _ink))),
                    const Icon(Icons.north_east_rounded,
                        size: 15, color: _muted)
                  ])))));

  Widget _opening() => Padding(
      padding: const EdgeInsets.fromLTRB(0, 10, 0, 16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Container(
            width: 42,
            height: 42,
            decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(color: _ink.withValues(alpha: .3))),
            child:
                const Icon(Icons.auto_awesome_outlined, size: 20, color: _ink)),
        const SizedBox(height: 20),
        Text('Listen closer.',
            style:
                NeatieDesign.display(42).copyWith(color: _ink, height: 1.08)),
        const SizedBox(height: 14),
        const Text(
            'The words, the feeling, the story behind it.\nWhere shall we begin?',
            style: TextStyle(color: _muted, height: 1.6, fontSize: 13)),
        const SizedBox(height: 28),
        _suggestion(
            'Between the lines',
            'What might this song mean beyond its literal story?',
            Icons.notes_rounded),
        _suggestion(
            'The story behind it',
            'When was this song written, and what inspired it?',
            Icons.history_rounded),
        _suggestion(
            'Another way to hear it',
            'What are two different ways to interpret this song?',
            Icons.waves_rounded),
      ]));

  Widget _source(Map source, int index) {
    final uri = Uri.tryParse('${source['url'] ?? ''}');
    if (uri == null ||
        !['https', 'http'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty) return const SizedBox.shrink();
    final title = '${source['title'] ?? uri.host}';
    void open() =>
        unawaited(launchUrl(uri, mode: LaunchMode.externalApplication));
    return Semantics(
        container: true,
        label: title,
        button: true,
        onTap: open,
        child: ExcludeSemantics(
            child: Tooltip(
                message: title,
                child: TextButton(
                    onPressed: () => unawaited(
                        launchUrl(uri, mode: LaunchMode.externalApplication)),
                    style: TextButton.styleFrom(
                        foregroundColor: _muted,
                        padding: const EdgeInsets.symmetric(
                            horizontal: 9, vertical: 5),
                        minimumSize: const Size(0, 30),
                        visualDensity: VisualDensity.compact,
                        backgroundColor: Colors.white.withValues(alpha: .035),
                        shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(8))),
                    child: Text(
                        '${source['id'] ?? index + 1}  ${uri.host.replaceFirst('www.', '')}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 10))))));
  }

  Widget _trackCard(Map<String, dynamic> track) {
    final title = '${track['title'] ?? 'Song'}';
    return ListTile(
      dense: true,
      contentPadding: const EdgeInsets.symmetric(horizontal: 5),
      leading: HomeArtwork(
          api: widget.player.api, item: track, kind: 'track', size: 36),
      title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text('${track['artist'] ?? track['channel'] ?? ''}',
          maxLines: 1, overflow: TextOverflow.ellipsis),
      onTap: () => unawaited(widget.player.start(track)),
      trailing: IconButton(
        tooltip: 'Add $title to queue',
        icon: const Icon(Icons.playlist_add_rounded, size: 20),
        onPressed: () {
          final saved = widget.player.addToQueue(track);
          ScaffoldMessenger.maybeOf(context)?.showSnackBar(SnackBar(
            content: Text(saved
                ? 'Added $title to the queue.'
                : '$title is already queued or unavailable.'),
          ));
        },
      ),
    );
  }

  Widget _replyBody(Map<String, dynamic> turn) {
    final user = turn['role'] == 'user';
    final text = turn['content'] as String;
    final style = TextStyle(
        fontSize: 13,
        color: user ? _ink : const Color(0xffc2cbc5),
        height: 1.65);
    final cards = (turn['tracks'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .where((track) =>
            extractTrackId(track) != null && track['playable'] != false)
        .toList();
    final tracks = Column(children: cards.map(_trackCard).toList());
    if (!identical(turn, _freshReply) || _reduced) {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SelectableText(text, style: style),
        tracks,
      ]);
    }
    final words = RegExp(r'\S+\s*').allMatches(text).toList();
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: Duration(milliseconds: (words.length * 28).clamp(200, 720)),
      builder: (_, progress, child) {
        final count = (words.length * (progress / .83).clamp(0, 1)).ceil();
        final end = count == 0 ? 0 : words[count - 1].end;
        final fade = ((progress - .6) / .4).clamp(0.0, 1.0);
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (progress == 1)
            SelectableText(text, style: style)
          else
            ExcludeSemantics(
                child: Text.rich(TextSpan(style: style, children: [
              TextSpan(text: text.substring(0, end)),
              TextSpan(
                  text: text.substring(end),
                  style: const TextStyle(color: Colors.transparent)),
            ]))),
          IgnorePointer(
              ignoring: fade < .9,
              child: ExcludeSemantics(
                  excluding: fade < .9,
                  child: Opacity(opacity: fade, child: child))),
        ]);
      },
      child: tracks,
    );
  }

  Widget _message(Map<String, dynamic> turn, int index) {
    final user = turn['role'] == 'user';
    final sources = turn['sources'] as List? ?? [];
    final content = Padding(
        padding: const EdgeInsets.only(bottom: 24),
        child: Align(
            alignment: user ? Alignment.centerRight : Alignment.centerLeft,
            child: FractionallySizedBox(
                widthFactor: user ? .9 : 1,
                child: Container(
                    padding: user
                        ? const EdgeInsets.symmetric(
                            horizontal: 16, vertical: 13)
                        : EdgeInsets.zero,
                    decoration: user
                        ? BoxDecoration(
                            color: const Color(0xff27372f),
                            borderRadius: BorderRadius.circular(16).copyWith(
                                bottomRight: const Radius.circular(4)))
                        : null,
                    child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (!user) ...[
                            const Row(children: [
                              Icon(Icons.auto_awesome_outlined,
                                  size: 13, color: _ink),
                              SizedBox(width: 7),
                              Text('Neatie',
                                  style: TextStyle(
                                      color: _ink,
                                      fontSize: 11,
                                      fontWeight: FontWeight.w600))
                            ]),
                            const SizedBox(height: 10),
                          ],
                          _replyBody(turn),
                          if (sources.isNotEmpty) ...[
                            const SizedBox(height: 12),
                            if (turn['contextOnly'] == true)
                              const Padding(
                                padding: EdgeInsets.only(bottom: 5),
                                child: Text('Related reading',
                                    style: TextStyle(fontSize: 11)),
                              ),
                            Wrap(spacing: 5, runSpacing: 5, children: [
                              for (var i = 0; i < sources.length; i++)
                                _source(sources[i] as Map, i)
                            ]),
                          ],
                        ])))));
    return _arrival(content, '${_selection}:$index:${turn['content']}');
  }

  Widget _composer() =>
      Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (_error != null)
          Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Text(_error!,
                  style: const TextStyle(
                      color: Color(0xffe8bd9e), fontSize: 12, height: 1.4))),
        Focus(
            onFocusChange: (_) => setState(() {}),
            child: AnimatedContainer(
                duration: _reduced
                    ? Duration.zero
                    : const Duration(milliseconds: 180),
                padding: const EdgeInsets.fromLTRB(15, 5, 6, 5),
                decoration: BoxDecoration(
                    color: const Color(0xff1c2721),
                    borderRadius: BorderRadius.circular(18),
                    border: Border.all(
                        color: _focus.hasFocus
                            ? _ink.withValues(alpha: .6)
                            : const Color(0xff35453b))),
                child: ValueListenableBuilder<TextEditingValue>(
                    valueListenable: _input,
                    builder: (context, value, _) => Row(
                            crossAxisAlignment: CrossAxisAlignment.end,
                            children: [
                              Expanded(
                                  child: CallbackShortcuts(
                                      bindings: {
                                    const SingleActivator(
                                            LogicalKeyboardKey.enter):
                                        () => unawaited(_send()),
                                  },
                                      child: Semantics(
                                          label: 'Ask about this song',
                                          child: TextField(
                                              controller: _input,
                                              focusNode: _focus,
                                              minLines: 1,
                                              maxLines: 4,
                                              maxLength: 2000,
                                              textInputAction:
                                                  TextInputAction.send,
                                              onSubmitted: (_) =>
                                                  unawaited(_send()),
                                              style: const TextStyle(
                                                  color: _ink,
                                                  fontSize: 13,
                                                  height: 1.5),
                                              decoration: const InputDecoration(
                                                  hint: ExcludeSemantics(
                                                      child: Text(
                                                          'Ask about this song')),
                                                  counterText: '',
                                                  hintStyle:
                                                      TextStyle(color: _muted),
                                                  border: InputBorder.none,
                                                  enabledBorder:
                                                      InputBorder.none,
                                                  focusedBorder:
                                                      InputBorder.none,
                                                  filled: false,
                                                  contentPadding:
                                                      EdgeInsets.symmetric(
                                                          vertical: 12)))))),
                              const SizedBox(width: 8),
                              Padding(
                                  padding: const EdgeInsets.only(bottom: 3),
                                  child: IconButton.filled(
                                      tooltip:
                                          _busy ? 'Cancel' : 'Send question',
                                      onPressed: _busy
                                          ? () {
                                              _cancel(restore: true);
                                              setState(() {});
                                            }
                                          : value.text.trim().isEmpty
                                              ? null
                                              : () => unawaited(_send()),
                                      style: IconButton.styleFrom(
                                          backgroundColor: _ink,
                                          foregroundColor:
                                              const Color(0xff142119),
                                          disabledBackgroundColor:
                                              const Color(0xff34433a),
                                          disabledForegroundColor: _muted,
                                          minimumSize: const Size(38, 38),
                                          maximumSize: const Size(38, 38)),
                                      icon: Icon(
                                          _busy
                                              ? Icons.stop_rounded
                                              : Icons.arrow_upward_rounded,
                                          size: 19))),
                            ])))),
        const SizedBox(height: 9),
        const Text('Context included. Interpretations stay open.',
            textAlign: TextAlign.center,
            style: TextStyle(color: _muted, fontSize: 10)),
      ]);

  @override
  Widget build(BuildContext context) {
    final track = widget.player.current ?? {};
    return RepaintBoundary(
        child: DecoratedBox(
            decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(22),
                border: Border.all(color: const Color(0xff303d34)),
                gradient: const LinearGradient(
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                    colors: [
                      Color(0xff1c2a22),
                      Color(0xff111a16),
                      Color(0xff131916)
                    ])),
            child: Padding(
                padding: const EdgeInsets.fromLTRB(20, 14, 20, 15),
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Row(children: [
                        Expanded(
                            child: Text('Behind the lyrics',
                                style: NeatieDesign.display(23)
                                    .copyWith(color: _ink))),
                        IconButton(
                            tooltip: 'Close song conversation',
                            iconSize: 17,
                            onPressed: () {
                              _cancel(restore: true);
                              _saveConversation();
                              _focus.unfocus();
                              widget.onClose();
                            },
                            color: _muted,
                            icon: const Icon(Icons.arrow_back_rounded))
                      ]),
                      Padding(
                          padding: const EdgeInsets.only(top: 6, bottom: 16),
                          child: Row(children: [
                            ClipRRect(
                                borderRadius: BorderRadius.circular(7),
                                child: ArtworkImage(
                                    urls: widget.player.api
                                        .artworkUrls(track, 'track'),
                                    width: 34,
                                    height: 34)),
                            const SizedBox(width: 10),
                            Expanded(
                                child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                  Text('${track['title'] ?? ''}',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                          color: _ink,
                                          fontSize: 11,
                                          fontWeight: FontWeight.w600)),
                                  Text(
                                      '${track['artist'] ?? track['channel'] ?? ''}',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                          color: _muted, fontSize: 10))
                                ])),
                            AnimatedBuilder(
                                animation: widget.player,
                                builder: (_, __) => IconButton(
                                    tooltip: widget.player.playing
                                        ? 'Pause'
                                        : 'Play',
                                    iconSize: 17,
                                    color: _ink,
                                    onPressed: () => widget.player.canPause
                                        ? widget.player.pause()
                                        : unawaited(widget.player.play()),
                                    icon: Icon(widget.player.playing
                                        ? Icons.pause_rounded
                                        : Icons.play_arrow_rounded))),
                          ])),
                      const Divider(height: 1, color: Color(0xff344138)),
                      Expanded(
                          child: ListView(
                              controller: _scroll,
                              padding:
                                  const EdgeInsets.only(top: 22, bottom: 12),
                              children: [
                            if (_turns.isEmpty && !_busy)
                              _arrival(_opening(), 'opening:$_selection'),
                            for (var i = 0; i < _turns.length; i++)
                              _message(_turns[i], i),
                            if (_pendingQuestion != null)
                              _message(
                                  {'role': 'user', 'content': _pendingQuestion},
                                  _turns.length),
                            if (_busy)
                              Padding(
                                  padding: EdgeInsets.only(bottom: 20),
                                  child: Row(children: [
                                    SizedBox(
                                        width: 13,
                                        height: 13,
                                        child: _reduced
                                            ? const Icon(
                                                Icons.auto_awesome_outlined,
                                                size: 13,
                                                color: _ink)
                                            : const CircularProgressIndicator(
                                                strokeWidth: 1.5, color: _ink)),
                                    SizedBox(width: 10),
                                    Expanded(
                                        child: Text(
                                            'Considering your question…',
                                            style: TextStyle(
                                                color: _muted, fontSize: 12)))
                                  ])),
                          ])),
                      _composer(),
                    ]))));
  }
}

/// Bounded device-local history using the existing browser storage adapter.
class SongConversations {
  static const maxTurns = 32;
  SongConversations(this.scope, this.store) {
    try {
      final raw = store?.read();
      if (raw == null || raw.length > 524288) return;
      final payload = jsonDecode(raw) as Map;
      if (payload['version'] != 1 || payload['scope'] != scope) return;
      for (final entry in (payload['songs'] as Map).entries.take(32)) {
        final data = Map<String, dynamic>.from(entry.value as Map);
        final turns = (data['turns'] as List).whereType<Map>().toList();
        if (turns.length > maxTurns ||
            turns.any((turn) =>
                !['user', 'assistant'].contains(turn['role']) ||
                turn['content'] is! String ||
                (turn['content'] as String).length > 4000)) continue;
        _songs['${entry.key}'] = {
          'turns': turns
              .map((turn) => <String, dynamic>{
                    'role': turn['role'],
                    'content': turn['content'],
                    'sources': turn['sources'] is List
                        ? (turn['sources'] as List)
                            .whereType<Map>()
                            .take(5)
                            .map((source) => Map<String, dynamic>.from(source))
                            .toList()
                        : [],
                    'hasLyrics': turn['hasLyrics'] == true,
                    'contextOnly': turn['contextOnly'] == true,
                    'tracks': MusicApi.tracks(turn['tracks']).take(4).toList(),
                  })
              .toList(),
          'draft': '${data['draft'] ?? ''}'
              .substring(0, '${data['draft'] ?? ''}'.length.clamp(0, 2000))
        };
      }
    } catch (_) {/* Blocked/damaged storage must not affect listening. */}
  }
  final String scope;
  final SessionStore? store;
  final _songs = <String, Map<String, dynamic>>{};
  Map<String, dynamic> read(String song) => _songs[song] ?? {};
  void save(String song, List<Map<String, dynamic>> turns, String draft) {
    if (song.isEmpty) return;
    _songs.remove(song);
    if (turns.isNotEmpty || draft.isNotEmpty) {
      _songs[song] = {
        'turns': turns.reversed
            .take(maxTurns)
            .toList()
            .reversed
            .map((turn) => Map<String, dynamic>.from(turn))
            .toList(),
        'draft': draft.substring(0, draft.length.clamp(0, 2000))
      };
    }
    String encode() =>
        jsonEncode({'version': 1, 'scope': scope, 'songs': _songs});
    while (_songs.length > 32 || encode().length > 524288) {
      _songs.remove(_songs.keys.first);
    }
    try {
      store?.write(encode());
    } catch (_) {/* Keep in-memory conversation. */}
  }
}
