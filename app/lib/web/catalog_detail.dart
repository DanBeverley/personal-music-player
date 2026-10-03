import 'dart:async';

import 'package:flutter/material.dart';

import 'catalog_api.dart';
import 'music_api.dart';
import 'web_player.dart';

class CatalogDetail extends StatefulWidget {
  const CatalogDetail({
    super.key,
    required this.api,
    required this.id,
    required this.kind,
    required this.title,
    required this.player,
    required this.onLike,
    required this.onOpen,
    required this.onBack,
  });

  final CatalogApi api;
  final String id;
  final String kind;
  final String title;
  final WebPlayer player;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final void Function(String kind, String id, String title) onOpen;
  final VoidCallback onBack;

  @override
  State<CatalogDetail> createState() => _CatalogDetailState();
}

class _CatalogDetailState extends State<CatalogDetail> {
  Map<String, dynamic>? _detail;
  Object? _error;
  var _loading = true;
  var _requestVersion = 0;

  bool get _isArtist => widget.kind.toLowerCase() == 'artist';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _requestVersion++;
    super.dispose();
  }

  Future<void> _load() async {
    final requestVersion = ++_requestVersion;
    setState(() {
      _loading = true;
      _error = null;
      _detail = null;
    });
    try {
      final detail = _isArtist
          ? await widget.api.artist(widget.id)
          : await widget.api.album(widget.id);
      if (!mounted || requestVersion != _requestVersion) return;
      setState(() {
        _detail = detail;
        _loading = false;
      });
    } catch (error) {
      if (!mounted || requestVersion != _requestVersion) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    }
  }

  String _text(dynamic value) => value?.toString().trim() ?? '';

  String get _title {
    final detail = _detail;
    final value = _isArtist
        ? (detail?['name'] ?? detail?['title'])
        : (detail?['title'] ?? detail?['name']);
    final resolved = _text(value);
    return resolved.isEmpty ? widget.title : resolved;
  }

  String get _artist {
    final detail = _detail;
    final value = _isArtist
        ? (detail?['artist'] ?? detail?['author'])
        : (detail?['artist'] ?? detail?['author'] ?? detail?['channel']);
    return _text(value);
  }

  String get _thumbnail => _text(_detail?['thumbnail']);

  String get _biography {
    final full = _text(_detail?['full_description']);
    return full.isNotEmpty ? full : _text(_detail?['description']);
  }

  List<Map<String, dynamic>> get _tracks => MusicApi.tracks(
        _isArtist ? (_detail?['top_songs']) : (_detail?['tracks']),
      );

  List<Map<String, dynamic>> get _albums {
    final raw = _detail?['albums'];
    return raw is List
        ? raw.whereType<Map>().map(Map<String, dynamic>.from).toList()
        : const [];
  }

  Future<void> _playAlbum(int selected) async {
    final tracks = _tracks;
    if (tracks.isEmpty) return;
    await widget.player.startQueue(tracks, selected, _title);
  }

  Future<void> _playArtistTrack(Map<String, dynamic> track) =>
      widget.player.start(track);

  @override
  Widget build(BuildContext context) {
    final body = _loading
        ? const Center(child: CircularProgressIndicator())
        : _error != null
            ? _Failure(onRetry: _load)
            : _detail == null
                ? const _EmptyDetail(message: 'No detail data is available.')
                : _content();
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Back',
          icon: const Icon(Icons.arrow_back),
          onPressed: widget.onBack,
        ),
        title: Text(_isArtist ? 'Artist' : 'Album'),
      ),
      body: body,
    );
  }

  Widget _content() {
    final tracks = _tracks;
    return ListView(
      padding: const EdgeInsets.all(20),
      children: [
        _Header(
          title: _title,
          artist: _artist,
          thumbnail: widget.api.api.artworkUri(_thumbnail)?.toString() ?? '',
        ),
        const SizedBox(height: 20),
        if (_isArtist && _biography.isNotEmpty) ...[
          Card(
            child: ExpansionTile(
              title: const Text('About'),
              childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
              expandedAlignment: Alignment.centerLeft,
              children: [
                SelectableText(
                  _biography,
                  style: Theme.of(context).textTheme.bodyLarge,
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
        ],
        if (!_isArtist)
          FilledButton.icon(
            onPressed: tracks.isEmpty ? null : () => unawaited(_playAlbum(0)),
            icon: const Icon(Icons.play_arrow),
            label: const Text('Play album'),
          ),
        if (!_isArtist) const SizedBox(height: 20),
        if (_isArtist && _albums.isNotEmpty) ...[
          Text('Albums', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          for (final album in _albums) _albumCard(album),
          const SizedBox(height: 20),
        ],
        Text(
          _isArtist ? 'Top songs' : 'Tracks',
          style: Theme.of(context).textTheme.titleLarge,
        ),
        const SizedBox(height: 8),
        if (tracks.isEmpty)
          _EmptyDetail(
            message: _isArtist
                ? 'No playable top songs are available for this artist.'
                : 'No playable tracks are available for this album.',
          )
        else
          for (var index = 0; index < tracks.length; index++)
            _trackTile(tracks[index], index),
      ],
    );
  }

  Widget _albumCard(Map<String, dynamic> album) {
    final id = _text(album['id'] ?? album['browseId'] ?? album['album_id']);
    final title = _text(album['title'] ?? album['name']);
    final artist =
        _text(album['artist'] ?? album['author'] ?? album['channel']);
    return Card(
      child: ListTile(
        enabled: id.isNotEmpty,
        leading: _Cover(
          url: widget.api.api.artworkUri(album['thumbnail'])?.toString() ?? '',
          size: 48,
        ),
        title: Text(title.isEmpty ? 'Untitled album' : title),
        subtitle: artist.isEmpty ? null : Text(artist),
        trailing: const Icon(Icons.chevron_right),
        onTap: id.isEmpty ? null : () => widget.onOpen('album', id, title),
      ),
    );
  }

  Widget _trackTile(Map<String, dynamic> track, int index) {
    final title = _text(track['title'] ?? track['name']);
    final artist =
        _text(track['channel'] ?? track['artist'] ?? track['author']);
    return ListTile(
      leading: _Cover(
        url: widget.api.api.artworkUri(track['thumbnail'])?.toString() ?? '',
        size: 48,
      ),
      title: Text(title.isEmpty ? 'Untitled track' : title),
      subtitle: artist.isEmpty ? null : Text(artist),
      trailing: IconButton(tooltip: 'Like $title', icon: const Icon(Icons.favorite_border),
        onPressed: () => unawaited(widget.onLike(track))),
      onTap: () => unawaited(
        _isArtist ? _playArtistTrack(track) : _playAlbum(index),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({
    required this.title,
    required this.artist,
    required this.thumbnail,
  });

  final String title;
  final String artist;
  final String thumbnail;

  @override
  Widget build(BuildContext context) => Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _Cover(url: thumbnail, size: 112),
          const SizedBox(width: 16),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: Theme.of(context).textTheme.headlineSmall),
                if (artist.isNotEmpty) ...[
                  const SizedBox(height: 6),
                  Text(artist, style: Theme.of(context).textTheme.titleMedium),
                ],
              ],
            ),
          ),
        ],
      );
}

class _Cover extends StatelessWidget {
  const _Cover({required this.url, required this.size});

  final String url;
  final double size;

  @override
  Widget build(BuildContext context) {
    final fallback = SizedBox(
      width: size,
      height: size,
      child: const ColoredBox(
        color: Color(0xff2a2d32),
        child: Icon(Icons.album_outlined),
      ),
    );
    if (url.isEmpty) return fallback;
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: Image.network(
        url,
        width: size,
        height: size,
        fit: BoxFit.cover,
        errorBuilder: (_, __, ___) => fallback,
      ),
    );
  }
}

class _Failure extends StatelessWidget {
  const _Failure({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('This detail could not be loaded.'),
            const SizedBox(height: 12),
            FilledButton(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      );
}

class _EmptyDetail extends StatelessWidget {
  const _EmptyDetail({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 24),
        child: Text(message),
      );
}
