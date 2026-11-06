import 'dart:async';
import 'package:flutter/material.dart';
import '../logic/track_metadata.dart';
import 'artwork_image.dart';
import 'catalog_api.dart';
import 'design_system.dart';
import 'music_api.dart';

final _apostrophes = RegExp(r"['’]");
final _separators = RegExp(r'[\s_-]+');

/// Search loaded collection metadata, preserving each original queue position.
List<int> matchingTrackIndices(
    List<Map<String, dynamic>> tracks, String query) {
  String normalize(String value) => value
      .toLowerCase()
      .replaceAll(_apostrophes, '')
      .replaceAll(_separators, ' ')
      .trim();
  final words =
      normalize(query).split(' ').where((word) => word.isNotEmpty).toList();
  return [
    for (var i = 0; i < tracks.length; i++)
      if (words.isEmpty ||
          (() {
            final track = tracks[i];
            final text = normalize([
              for (final key in [
                'title',
                'name',
                'artist',
                'channel',
                'author',
                'album',
                'album_title'
              ])
                if (track[key] != null) '${track[key]}',
              for (final artist in track['artists'] is List
                  ? track['artists'] as List
                  : const [])
                artist is Map ? '${artist['name'] ?? ''}' : '$artist',
            ].join(' '));
            final compact = text.replaceAll(' ', '');
            return words
                .every((word) => text.contains(word) || compact.contains(word));
          })())
        i
  ];
}

class CollectionSearchField extends StatelessWidget {
  const CollectionSearchField(
      {super.key,
      required this.controller,
      required this.label,
      required this.onChanged});
  final TextEditingController controller;
  final String label;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) => TextField(
        controller: controller,
        onChanged: onChanged,
        maxLength: 200,
        textInputAction: TextInputAction.search,
        decoration: InputDecoration(
          labelText: label,
          counterText: '',
          prefixIcon: const Icon(Icons.search),
          suffixIcon: controller.text.isEmpty
              ? null
              : IconButton(
                  tooltip: 'Clear collection search',
                  icon: const Icon(Icons.close),
                  onPressed: () {
                    controller.clear();
                    onChanged('');
                  }),
        ),
      );
}

/// Uses the same canonical Search as the main screen; never filters a queue.
class CatalogTrackSearch extends StatefulWidget {
  const CatalogTrackSearch(
      {super.key,
      required this.api,
      required this.onAdd,
      required this.isAdded,
      this.onPlay,
      this.active = true,
      this.bare = false,
      this.expanded = true,
      this.label = 'Find songs to add',
      this.actionLabel = 'Add to queue'});
  final MusicApi api;
  final Future<bool> Function(Map<String, dynamic>) onAdd;
  final Future<void> Function(Map<String, dynamic>)? onPlay;
  final bool Function(Map<String, dynamic>) isAdded;
  final bool active;
  final bool bare, expanded;
  final String label, actionLabel;
  @override
  State<CatalogTrackSearch> createState() => _CatalogTrackSearchState();
}

class _CatalogTrackSearchState extends State<CatalogTrackSearch>
    with AutomaticKeepAliveClientMixin {
  @override
  bool get wantKeepAlive => true;
  @override
  void initState() {
    super.initState();
    _boundScope = widget.api.userScopeId;
    _input = _field();
  }

  final _query = TextEditingController();
  final _focus = FocusNode();
  late Widget _input;
  late String _boundScope;
  Completer<void>? _cancel;
  Timer? _poll;
  int _pollEpoch = 0;
  int _generation = 0;
  int _revision = 0;
  bool _busy = false, _submitted = false;
  bool _pending = false, _failed = false;
  Map<String, dynamic> _page = {};
  String? _error;
  List<Map<String, dynamic>> _tracks = [];
  final _adding = <String>{};

  void _invalidate() {
    _generation++;
    _pollEpoch++;
    _poll?.cancel();
    if (_cancel?.isCompleted == false) _cancel!.complete();
  }

  @override
  void didUpdateWidget(covariant CatalogTrackSearch oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.label != widget.label || oldWidget.bare != widget.bare)
      _input = _field();
    if (widget.expanded && !oldWidget.expanded) _focus.requestFocus();
    final scopeChanged = _boundScope != widget.api.userScopeId;
    _boundScope = widget.api.userScopeId;
    if (!identical(oldWidget.api, widget.api) ||
        scopeChanged ||
        !widget.active) {
      _invalidate();
      _busy = false;
      if (!identical(oldWidget.api, widget.api) || scopeChanged) {
        _query.clear();
        _tracks = [];
        _submitted = false;
      }
    }
    if (widget.active && !oldWidget.active && _query.text.trim().isNotEmpty) {
      _changed(_query.text);
    }
  }

  void _changed(String value) {
    _invalidate();
    setState(() {
      _tracks = [];
      _error = null;
      _busy = false;
      _submitted = false;
      _pending = false;
      _failed = false;
      _page = {};
      _revision = 0;
    });
  }

  Future<void> _search() async {
    _invalidate();
    final value = _query.text.trim(), api = widget.api;
    if (value.isEmpty || !widget.active) return;
    final generation = _generation, scope = api.userScopeId;
    final cancel = _cancel = Completer<void>();
    setState(() {
      _busy = true;
      _error = null;
      _submitted = true;
      _pending = false;
      _failed = false;
      _page = {};
      _revision = 0;
    });
    try {
      final data = await CatalogApi(api).search(value,
          preview: false,
          continuation: {'result_type': 'tracks'},
          cancel: cancel.future);
      if (!mounted || generation != _generation || scope != api.userScopeId)
        return;
      _accept(data);
      _schedulePoll(value, generation, scope, cancel, 0);
    } catch (_) {
      if (mounted && generation == _generation && scope == api.userScopeId) {
        setState(() => _error = 'Could not find songs. Please retry.');
      }
    } finally {
      if (mounted && generation == _generation) setState(() => _busy = false);
    }
  }

  void _accept(Map<String, dynamic> data, {bool append = false}) {
    if (data['not_modified'] == true) return;
    final diagnostics = data['diagnostics'] as Map? ?? const {};
    final incomingRevision =
        (diagnostics['search_snapshot_revision'] as num?)?.toInt();
    if (!append && incomingRevision != null && incomingRevision < _revision)
      return;
    final top = data['top_result'];
    final candidates = [
      if (append) ..._tracks,
      if (top is Map &&
          (top['entity_type'] ?? 'track') == 'track' &&
          top['item'] is Map)
        Map<String, dynamic>.from(top['item'] as Map),
      ...MusicApi.tracks(data['tracks'] ?? data['results']),
    ];
    final seen = <String>{};
    setState(() {
      _tracks = candidates.where((track) {
        final id = extractTrackId(track);
        return id != null && track['playable'] != false && seen.add(id);
      }).toList();
      if (!append) {
        _revision = incomingRevision ?? 0;
        _pending = (diagnostics['search_pending_surfaces'] as List? ?? const [])
            .contains('tracks');
        _failed = (diagnostics['search_failed_sources'] as List? ?? const [])
                .isNotEmpty ||
            diagnostics['search_outcome'] == 'provider_unavailable';
      }
      final page = (data['pagination'] as Map?)?['tracks'];
      if (page is Map) _page = Map<String, dynamic>.from(page);
    });
  }

  void _schedulePoll(String query, int generation, String scope,
      Completer<void> cancel, int count) {
    if (!_pending || _revision <= 0) return;
    if (count >= 8) {
      setState(() {
        _pending = false;
        _failed = true;
      });
      return;
    }
    final epoch = _pollEpoch;
    _poll = Timer(const Duration(milliseconds: 700), () async {
      if (!mounted ||
          generation != _generation ||
          scope != widget.api.userScopeId ||
          epoch != _pollEpoch ||
          !widget.active) return;
      try {
        final data = await CatalogApi(widget.api).search(query,
            preview: false,
            continuation: {
              'result_type': 'tracks',
              'revision': _revision,
              'revision_wait_ms': 1000
            },
            cancel: cancel.future);
        if (!mounted ||
            generation != _generation ||
            scope != widget.api.userScopeId ||
            epoch != _pollEpoch ||
            !widget.active) return;
        _accept(data);
      } catch (_) {
        if (!mounted ||
            generation != _generation ||
            scope != widget.api.userScopeId ||
            epoch != _pollEpoch) return;
        setState(() => _failed = true);
      }
      _schedulePoll(query, generation, scope, cancel, count + 1);
    });
  }

  Future<void> _more() async {
    if (_busy || _page['has_more'] != true) return;
    _poll?.cancel();
    _pollEpoch++;
    final generation = _generation, scope = widget.api.userScopeId;
    final query = _query.text.trim();
    setState(() => _busy = true);
    try {
      final data = await CatalogApi(widget.api).search(query,
          preview: false,
          offset: (_page['next_offset'] as num? ?? 0).toInt(),
          continuation: {'result_type': 'tracks', 'cursor': _page['cursor']},
          cancel: _cancel?.future);
      if (!mounted ||
          generation != _generation ||
          scope != widget.api.userScopeId) return;
      if (data['pagination_expired'] == true) {
        setState(
            () => _error = 'Results refreshed. Search again to load more.');
      } else {
        _accept(data, append: true);
      }
    } catch (_) {
      if (mounted &&
          generation == _generation &&
          scope == widget.api.userScopeId) {
        setState(() => _error = 'Could not load more songs. Please retry.');
      }
    } finally {
      if (mounted &&
          generation == _generation &&
          scope == widget.api.userScopeId) {
        setState(() => _busy = false);
        if (_cancel != null)
          _schedulePoll(query, generation, scope, _cancel!, 0);
      }
    }
  }

  Future<void> _add(Map<String, dynamic> track) async {
    final id = extractTrackId(track)!;
    if (_adding.contains(id) || widget.isAdded(track)) return;
    final scope = widget.api.userScopeId;
    setState(() => _adding.add(id));
    try {
      final added = await widget.onAdd(track);
      if (mounted && scope == widget.api.userScopeId && !added) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not add this song. Please retry.')));
      }
    } catch (_) {
      if (mounted && scope == widget.api.userScopeId) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not add this song. Please retry.')));
      }
    } finally {
      if (mounted) setState(() => _adding.remove(id));
    }
  }

  Widget _field() => Semantics(
      label: widget.bare ? widget.label : null,
      child: TextField(
          controller: _query,
          focusNode: _focus,
          maxLength: 200,
          onChanged: _changed,
          onSubmitted: (_) => unawaited(_search()),
          textInputAction: TextInputAction.search,
          decoration: widget.bare
              ? InputDecoration(
                  isDense: true,
                  filled: false,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  counterText: '',
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 12),
                  suffixIcon: ValueListenableBuilder<TextEditingValue>(
                      valueListenable: _query,
                      builder: (context, value, _) => value.text.isEmpty
                          ? const SizedBox.shrink()
                          : IconButton(
                              tooltip: 'Clear song search',
                              iconSize: 16,
                              icon: const Icon(Icons.close),
                              onPressed: () {
                                _query.clear();
                                _changed('');
                              })))
              : InputDecoration(
                  labelText: widget.label,
                  counterText: '',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: ValueListenableBuilder<TextEditingValue>(
                      valueListenable: _query,
                      builder: (context, value, _) =>
                          Row(mainAxisSize: MainAxisSize.min, children: [
                            IconButton(
                                tooltip: 'Search songs',
                                icon: const Icon(Icons.arrow_forward_rounded),
                                onPressed: value.text.trim().isEmpty
                                    ? null
                                    : () => unawaited(_search())),
                            IconButton(
                                tooltip: 'Clear song search',
                                icon: const Icon(Icons.close),
                                onPressed: value.text.isEmpty
                                    ? null
                                    : () {
                                        _query.clear();
                                        _changed('');
                                      })
                          ])))));

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return Offstage(
        offstage: !widget.expanded,
        child:
            Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          _input,
          if (_busy)
            const Padding(
                padding: EdgeInsets.all(12),
                child: NeatieLoading(label: 'Finding songs', showLabel: true)),
          if (_error != null)
            Row(children: [
              Expanded(child: Text(_error!)),
              TextButton(onPressed: _search, child: const Text('Retry'))
            ]),
          if (!_busy &&
              _error == null &&
              _submitted &&
              _tracks.isEmpty &&
              !_pending &&
              !_failed)
            const Padding(
                padding: EdgeInsets.all(12), child: Text('No songs found.')),
          if (!_busy &&
              _error == null &&
              _submitted &&
              _tracks.isEmpty &&
              (_pending || _failed))
            Row(children: [
              Expanded(
                  child: Text(_failed
                      ? 'Song search is unavailable. Please retry.'
                      : 'Still finding songs...')),
              TextButton(onPressed: _search, child: const Text('Retry')),
            ]),
          if (_tracks.isNotEmpty) ...[
            const Padding(
                padding: EdgeInsets.only(top: 16, bottom: 8),
                child: Text('Top song results')),
            for (final track in _tracks)
              Builder(builder: (context) {
                final added = widget.isAdded(track),
                    id = extractTrackId(track)!;
                return ListTile(
                    leading: ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: ArtworkImage(
                            urls: widget.api.artworkUrls(track, 'track'),
                            width: 44,
                            height: 44)),
                    title: Text('${track['title'] ?? 'Untitled song'}',
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                    subtitle: Text(
                        '${track['artist'] ?? track['channel'] ?? ''}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                    onTap: widget.onPlay != null
                        ? () => unawaited(widget.onPlay!(track))
                        : added || _adding.contains(id)
                            ? null
                            : () => unawaited(_add(track)),
                    trailing: IconButton(
                        tooltip: added
                            ? 'Already added'
                            : '${widget.actionLabel}: ${track['title']}',
                        onPressed: added || _adding.contains(id)
                            ? null
                            : () => unawaited(_add(track)),
                        icon: Icon(
                            added ? Icons.check_rounded : Icons.add_rounded)));
              }),
            if (_page['has_more'] == true)
              TextButton(
                  onPressed: _busy ? null : _more,
                  child: const Text('More songs')),
          ],
        ]));
  }

  @override
  void dispose() {
    _invalidate();
    _query.dispose();
    _focus.dispose();
    super.dispose();
  }
}
