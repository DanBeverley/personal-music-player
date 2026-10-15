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
      this.active = true,
      this.label = 'Find songs to add',
      this.actionLabel = 'Add to queue'});
  final MusicApi api;
  final Future<bool> Function(Map<String, dynamic>) onAdd;
  final bool Function(Map<String, dynamic>) isAdded;
  final bool active;
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
    _input = _field();
  }

  final _query = TextEditingController();
  late Widget _input;
  Timer? _debounce;
  Completer<void>? _cancel;
  int _generation = 0;
  bool _busy = false, _submitted = false;
  String? _error;
  List<Map<String, dynamic>> _tracks = [];
  final _adding = <String>{};

  void _invalidate() {
    _generation++;
    _debounce?.cancel();
    if (_cancel?.isCompleted == false) _cancel!.complete();
  }

  @override
  void didUpdateWidget(covariant CatalogTrackSearch oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.label != widget.label) _input = _field();
    if (!identical(oldWidget.api, widget.api) || !widget.active) {
      _invalidate();
      _busy = false;
      if (!identical(oldWidget.api, widget.api)) {
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
      _busy = value.trim().isNotEmpty;
      _submitted = false;
    });
    if (_busy && widget.active) {
      _debounce = Timer(const Duration(milliseconds: 220), _search);
    }
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
    });
    try {
      final data = await CatalogApi(api).search(value,
          preview: true,
          continuation: {'result_type': 'tracks'},
          cancel: cancel.future);
      if (!mounted || generation != _generation || scope != api.userScopeId)
        return;
      final top = data['top_result'];
      final candidates = [
        if (top is Map &&
            (top['entity_type'] ?? 'track') == 'track' &&
            top['item'] is Map)
          Map<String, dynamic>.from(top['item'] as Map),
        ...MusicApi.tracks(data['tracks'] ?? data['results']),
      ];
      final seen = <String>{};
      setState(() {
        _tracks = candidates
            .where((track) {
              final id = extractTrackId(track);
              return id != null && track['playable'] != false && seen.add(id);
            })
            .take(6)
            .toList();
      });
    } catch (_) {
      if (mounted && generation == _generation) {
        setState(() => _error = 'Could not find songs. Please retry.');
      }
    } finally {
      if (mounted && generation == _generation) setState(() => _busy = false);
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

  Widget _field() => TextField(
      controller: _query,
      maxLength: 200,
      onChanged: _changed,
      onSubmitted: (_) => unawaited(_search()),
      textInputAction: TextInputAction.search,
      decoration: InputDecoration(
          labelText: widget.label,
          counterText: '',
          prefixIcon: const Icon(Icons.search),
          suffixIcon: ValueListenableBuilder<TextEditingValue>(
              valueListenable: _query,
              builder: (context, value, _) => IconButton(
                  tooltip: 'Clear song search',
                  icon: const Icon(Icons.close),
                  onPressed: value.text.isEmpty
                      ? null
                      : () {
                          _query.clear();
                          _changed('');
                        }))));

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
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
      if (!_busy && _error == null && _submitted && _tracks.isEmpty)
        const Padding(
            padding: EdgeInsets.all(12), child: Text('No songs found.')),
      if (_tracks.isNotEmpty) ...[
        const Padding(
            padding: EdgeInsets.only(top: 16, bottom: 8),
            child: Text('Top song results')),
        for (final track in _tracks)
          Builder(builder: (context) {
            final added = widget.isAdded(track), id = extractTrackId(track)!;
            return ListTile(
                leading: ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: ArtworkImage(
                        urls: widget.api.artworkUrls(track, 'track'),
                        width: 44,
                        height: 44)),
                title: Text('${track['title'] ?? 'Untitled song'}',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text('${track['artist'] ?? track['channel'] ?? ''}',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                onTap: added || _adding.contains(id)
                    ? null
                    : () => unawaited(_add(track)),
                trailing: IconButton(
                    tooltip: added
                        ? 'Already added'
                        : '${widget.actionLabel}: ${track['title']}',
                    onPressed: added || _adding.contains(id)
                        ? null
                        : () => unawaited(_add(track)),
                    icon:
                        Icon(added ? Icons.check_rounded : Icons.add_rounded)));
          }),
      ],
    ]);
  }

  @override
  void dispose() {
    _invalidate();
    _query.dispose();
    super.dispose();
  }
}
