part of 'main.dart';

class ArtistDetailsScreen extends ConsumerStatefulWidget {
  final String artistId;
  final Map<String, dynamic> fallbackArtist;

  const ArtistDetailsScreen({
    super.key,
    required this.artistId,
    required this.fallbackArtist,
  });

  @override
  ConsumerState<ArtistDetailsScreen> createState() =>
      _ArtistDetailsScreenState();
}

class _ArtistDetailsScreenState extends ConsumerState<ArtistDetailsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(artistDetailsProvider.notifier).fetchArtist(widget.artistId),
      );
    });
  }

  Future<void> _playArtistTracks(
    Map<String, dynamic> artist,
    List<dynamic> tracks, {
    bool shuffle = false,
  }) async {
    if (tracks.isEmpty) return;
    final navigator = Navigator.of(context);
    final currentTrack = shuffle
        ? tracks[DateTime.now().microsecondsSinceEpoch % tracks.length]
        : tracks.first;
    final artistName = artist['name']?.toString() ?? 'Artist Radio';
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'artist:${widget.artistId}:top-songs',
          playlistName: '$artistName Top Songs',
          tracks: tracks,
          currentTrack: currentTrack,
          shuffle: shuffle,
        );
    if (!mounted) return;
    navigator.push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  Future<void> _openAlbum(Map<String, dynamic> album) async {
    final albumId = album['id']?.toString();
    if (albumId == null || albumId.isEmpty) return;
    unawaited(ref.read(albumDetailsProvider.notifier).fetchAlbum(albumId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => AlbumDetailsScreen(
          albumId: albumId,
          fallbackAlbum: album,
        ),
      ),
    );
  }

  Future<void> _openRelatedArtist(Map<String, dynamic> artist) async {
    final artistId = artist['id']?.toString();
    if (artistId == null || artistId.isEmpty) return;
    unawaited(ref.read(artistDetailsProvider.notifier).fetchArtist(artistId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: artist,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final artistState = ref.watch(artistDetailsProvider);
    final loadedArtist = artistState.artist;
    final artist = loadedArtist != null &&
            loadedArtist['id']?.toString() == widget.artistId
        ? loadedArtist
        : widget.fallbackArtist;
    final topSongs =
        (loadedArtist?['top_songs'] as List<dynamic>? ?? const <dynamic>[]);
    final albums =
        (loadedArtist?['albums'] as List<dynamic>? ?? const <dynamic>[]);
    final relatedArtists =
        (loadedArtist?['related_artists'] as List<dynamic>? ??
            const <dynamic>[]);
    final stats =
        (loadedArtist?['stats'] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map>()
            .map((entry) => Map<String, dynamic>.from(entry))
            .toList(growable: false);
    final description = (artist['description'] ?? '').toString().trim();

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: Text(artist['name']?.toString() ?? 'Artist'),
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(18, 18, 18, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.03),
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                  child: AppArtwork(
                    thumbnail: artist['thumbnail'],
                    width: 220,
                    height: 220,
                    radius: 999,
                  ),
                ),
              ),
              const SizedBox(height: 24),
              Text(
                artist['name']?.toString() ?? 'Unknown Artist',
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 28,
                  fontWeight: FontWeight.w800,
                ),
              ),
              if (description.isNotEmpty) ...[
                const SizedBox(height: 10),
                Text(
                  description,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.62),
                    fontSize: 14,
                    height: 1.45,
                  ),
                ),
              ],
              if (stats.isNotEmpty) ...[
                const SizedBox(height: 16),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: stats.map((stat) {
                    final label = (stat['label'] ?? '').toString();
                    final value = (stat['value'] ?? '').toString();
                    return Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.05),
                        borderRadius: BorderRadius.circular(999),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.08),
                        ),
                      ),
                      child: Text(
                        '$label • $value',
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.82),
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    );
                  }).toList(growable: false),
                ),
              ],
              const SizedBox(height: 22),
              Row(
                children: [
                  Expanded(
                    child: FilledButton(
                      style: FilledButton.styleFrom(
                        backgroundColor: _surfaceGreyAlt,
                        foregroundColor: Colors.white,
                        padding: const EdgeInsets.symmetric(vertical: 14),
                      ),
                      onPressed: topSongs.isEmpty
                          ? null
                          : () => _playArtistTracks(artist, topSongs),
                      child: const Text('Play top songs'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.white,
                      side: BorderSide(
                        color: Colors.white.withValues(alpha: 0.14),
                      ),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 14,
                      ),
                    ),
                    onPressed: topSongs.isEmpty
                        ? null
                        : () => _playArtistTracks(
                              artist,
                              topSongs,
                              shuffle: true,
                            ),
                    icon: const Icon(Icons.shuffle_rounded),
                    label: const Text('Shuffle'),
                  ),
                ],
              ),
              if (albums.isNotEmpty) ...[
                const SizedBox(height: 28),
                const Text(
                  'Albums',
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 16),
                SizedBox(
                  height: 246,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    itemCount: albums.length,
                    separatorBuilder: (_, __) => const SizedBox(width: 14),
                    itemBuilder: (context, index) {
                      final album =
                          Map<String, dynamic>.from(albums[index] as Map);
                      return SizedBox(
                        width: 170,
                        child: Material(
                          color: Colors.transparent,
                          child: InkWell(
                            borderRadius: BorderRadius.circular(_radiusLarge),
                            onTap: () => _openAlbum(album),
                            child: Ink(
                              padding: const EdgeInsets.all(12),
                              decoration: BoxDecoration(
                                color: Colors.white.withValues(alpha: 0.03),
                                borderRadius:
                                    BorderRadius.circular(_radiusLarge),
                                border: Border.all(
                                  color: Colors.white.withValues(alpha: 0.08),
                                ),
                              ),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  AppArtwork(
                                    thumbnail: album['thumbnail'],
                                    width: 140,
                                    height: 140,
                                    radius: 18,
                                  ),
                                  const SizedBox(height: 10),
                                  Text(
                                    album['title']?.toString() ??
                                        'Unknown Album',
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      color: Colors.white,
                                      fontSize: 14,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 6),
                                  Text(
                                    [
                                      if ((album['year'] ?? '')
                                          .toString()
                                          .isNotEmpty)
                                        album['year'].toString(),
                                      if ((album['track_count'] ?? 0) > 0)
                                        '${album['track_count']} tracks',
                                    ].join(' • '),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.52),
                                      fontSize: 12,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                ),
              ],
              const SizedBox(height: 28),
              const Text(
                'Top songs',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 16),
              if (artistState.isLoading && topSongs.isEmpty)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.symmetric(vertical: 32),
                    child: CircularProgressIndicator(color: _accentGrey),
                  ),
                )
              else if (topSongs.isEmpty)
                Text(
                  artistState.error ??
                      'No top-song data available for this artist.',
                  style: TextStyle(color: Colors.white.withValues(alpha: 0.56)),
                )
              else
                ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: topSongs.length,
                  itemBuilder: (context, index) {
                    final track =
                        Map<String, dynamic>.from(topSongs[index] as Map);
                    final videoId =
                        (track['id'] ?? track['videoId'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.06),
                        ),
                      ),
                      child: ListTile(
                        onTap: () async {
                          final navigator = Navigator.of(context);
                          await ref
                              .read(playbackQueueProvider.notifier)
                              .startPlaylistSession(
                                playlistId:
                                    'artist:${widget.artistId}:top-songs',
                                playlistName:
                                    '${artist['name']?.toString() ?? 'Artist'} Top Songs',
                                tracks: topSongs,
                                currentTrack: track,
                              );
                          if (!mounted) return;
                          navigator.push(
                            MaterialPageRoute(
                              builder: (_) => const FullPlayerScreen(),
                            ),
                          );
                        },
                        leading: AppArtwork(
                          thumbnail: track['thumbnail'],
                          videoId: videoId,
                          width: 56,
                          height: 56,
                          radius: 14,
                        ),
                        title: Text(
                          track['title'] ?? 'Unknown Track',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        subtitle: Text(
                          track['channel'] ??
                              artist['name']?.toString() ??
                              'Unknown Artist',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.52),
                            fontSize: 12,
                          ),
                        ),
                        trailing: IconButton(
                          onPressed: () => showAddToPlaylistDialog(
                            context: context,
                            track: track,
                          ),
                          icon: Icon(
                            Icons.playlist_add_rounded,
                            color: Colors.white.withValues(alpha: 0.68),
                          ),
                        ),
                      ),
                    );
                  },
                ),
              if (relatedArtists.isNotEmpty) ...[
                const SizedBox(height: 28),
                const Text(
                  'Similar artists',
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 14),
                SizedBox(
                  height: 112,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    itemCount: relatedArtists.length,
                    separatorBuilder: (_, __) => const SizedBox(width: 14),
                    itemBuilder: (context, index) {
                      final related = Map<String, dynamic>.from(
                          relatedArtists[index] as Map);
                      return SizedBox(
                        width: 82,
                        child: Material(
                          color: Colors.transparent,
                          child: InkWell(
                            borderRadius: BorderRadius.circular(22),
                            onTap: () => _openRelatedArtist(related),
                            child: Column(
                              children: [
                                AppArtwork(
                                  thumbnail: related['thumbnail'],
                                  width: 68,
                                  height: 68,
                                  radius: 999,
                                ),
                                const SizedBox(height: 10),
                                Text(
                                  related['name']?.toString() ?? 'Artist',
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.88),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w600,
                                    height: 1.2,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class AlbumDetailsScreen extends ConsumerStatefulWidget {
  final String albumId;
  final Map<String, dynamic> fallbackAlbum;

  const AlbumDetailsScreen({
    super.key,
    required this.albumId,
    required this.fallbackAlbum,
  });

  @override
  ConsumerState<AlbumDetailsScreen> createState() => _AlbumDetailsScreenState();
}

class _AlbumDetailsScreenState extends ConsumerState<AlbumDetailsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(albumDetailsProvider.notifier).fetchAlbum(widget.albumId),
      );
    });
  }

  Future<void> _playAlbum(
    List<dynamic> tracks, {
    bool shuffle = false,
  }) async {
    if (tracks.isEmpty) return;
    final navigator = Navigator.of(context);
    final currentTrack = shuffle
        ? tracks[DateTime.now().microsecondsSinceEpoch % tracks.length]
        : tracks.first;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'album:${widget.albumId}',
          playlistName:
              widget.fallbackAlbum['title']?.toString() ?? 'Album Queue',
          tracks: tracks,
          currentTrack: currentTrack,
          shuffle: shuffle,
        );
    if (!mounted) return;
    navigator.push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  void _saveAlbumAsPlaylist(Map<String, dynamic> album, List<dynamic> tracks) {
    if (tracks.isEmpty) return;
    final title = album['title']?.toString() ?? 'Saved Album';
    final playlist = ref.read(playlistProvider.notifier).createPlaylist(title);
    for (final track in tracks) {
      ref
          .read(playlistProvider.notifier)
          .addTrackToPlaylist(playlist.id, track);
    }
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Saved "$title" as a playlist.'),
        backgroundColor: Colors.grey[900],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final albumState = ref.watch(albumDetailsProvider);
    final loadedAlbum = albumState.album;
    final album =
        loadedAlbum != null && loadedAlbum['id']?.toString() == widget.albumId
            ? loadedAlbum
            : widget.fallbackAlbum;
    final tracks = (loadedAlbum?['tracks'] as List<dynamic>?) ?? const [];

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: const Text('Album'),
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.03),
                    borderRadius: BorderRadius.circular(_radiusLarge),
                    border: Border.all(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                  child: AppArtwork(
                    thumbnail: album['thumbnail'],
                    width: 240,
                    height: 240,
                    radius: 28,
                  ),
                ),
              ),
              const SizedBox(height: 24),
              Text(
                album['title']?.toString() ?? 'Unknown Album',
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 28,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                [
                  album['artist']?.toString() ?? 'Unknown Artist',
                  if ((album['year'] ?? '').toString().isNotEmpty)
                    album['year'].toString(),
                  if (tracks.isNotEmpty) '${tracks.length} tracks',
                ].join(' • '),
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.56),
                  fontSize: 14,
                ),
              ),
              const SizedBox(height: 22),
              Row(
                children: [
                  Expanded(
                    child: FilledButton(
                      style: FilledButton.styleFrom(
                        backgroundColor: _surfaceGreyAlt,
                        foregroundColor: Colors.white,
                        padding: const EdgeInsets.symmetric(vertical: 14),
                      ),
                      onPressed:
                          tracks.isEmpty ? null : () => _playAlbum(tracks),
                      child: const Text('Play Album'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.white,
                      side: BorderSide(
                        color: Colors.white.withValues(alpha: 0.14),
                      ),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 14,
                      ),
                    ),
                    onPressed: tracks.isEmpty
                        ? null
                        : () => _playAlbum(tracks, shuffle: true),
                    icon: const Icon(Icons.shuffle_rounded),
                    label: const Text('Shuffle'),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
                  side: BorderSide(color: Colors.white.withValues(alpha: 0.14)),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 14,
                  ),
                ),
                onPressed: tracks.isEmpty
                    ? null
                    : () => _saveAlbumAsPlaylist(album, tracks),
                icon: const Icon(Icons.library_add_rounded),
                label: const Text('Save Album As Playlist'),
              ),
              const SizedBox(height: 24),
              const Text(
                'Tracks',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 16),
              if (albumState.isLoading && tracks.isEmpty)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.symmetric(vertical: 32),
                    child: CircularProgressIndicator(color: _accentGrey),
                  ),
                )
              else if (tracks.isEmpty)
                Text(
                  albumState.error ?? 'No track data available for this album.',
                  style: TextStyle(color: Colors.white.withValues(alpha: 0.56)),
                )
              else
                ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: tracks.length,
                  itemBuilder: (context, index) {
                    final track = tracks[index];
                    final videoId =
                        (track['id'] ?? track['videoId'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.06),
                        ),
                      ),
                      child: ListTile(
                        onTap: () async {
                          final navigator = Navigator.of(context);
                          await ref
                              .read(playbackQueueProvider.notifier)
                              .startPlaylistSession(
                                playlistId: 'album:${widget.albumId}',
                                playlistName:
                                    album['title']?.toString() ?? 'Album Queue',
                                tracks: tracks,
                                currentTrack: track,
                              );
                          if (!mounted) return;
                          navigator.push(
                            MaterialPageRoute(
                              builder: (_) => const FullPlayerScreen(),
                            ),
                          );
                        },
                        leading: AppArtwork(
                          thumbnail: track['thumbnail'] ?? album['thumbnail'],
                          videoId: videoId,
                          width: 56,
                          height: 56,
                          radius: 14,
                        ),
                        title: Text(
                          track['title'] ?? 'Unknown Track',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        subtitle: Text(
                          track['channel'] ??
                              track['author'] ??
                              album['artist']?.toString() ??
                              '',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.52),
                            fontSize: 12,
                          ),
                        ),
                        trailing: IconButton(
                          onPressed: () => showAddToPlaylistDialog(
                            context: context,
                            track: track,
                          ),
                          icon: Icon(
                            Icons.playlist_add_rounded,
                            color: Colors.white.withValues(alpha: 0.68),
                          ),
                        ),
                      ),
                    );
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class TrackDetailsScreen extends ConsumerWidget {
  final Map<String, dynamic> track;
  const TrackDetailsScreen({super.key, required this.track});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final details = ref.watch(trackDetailsProvider);
    final videoId = (track['id'] ?? track['videoId'])?.toString();
    final downloadTask =
        videoId == null ? null : ref.watch(downloadTaskProvider(videoId));
    final albumTitleValue = (details?['album_title'] ??
            details?['album'] ??
            track['album_title'] ??
            track['album'])
        ?.toString()
        .trim();
    final albumTitle = albumTitleValue == null || albumTitleValue.isEmpty
        ? null
        : albumTitleValue;
    final albumIdValue =
        (details?['album_id'] ?? track['album_id'])?.toString().trim();
    final albumId =
        albumIdValue == null || albumIdValue.isEmpty ? null : albumIdValue;
    final albumArtist =
        (details?['author'] ?? track['channel'] ?? track['author'] ?? '')
            .toString();

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: const Text(''),
        backgroundColor: Colors.transparent,
        elevation: 0,
      ),
      body: SingleChildScrollView(
        child: Column(
          children: [
            // Top Section: Track Info
            Container(
              padding: const EdgeInsets.all(24),
              color: _voidBlack,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  AppArtwork(
                    thumbnail: track['thumbnail'],
                    videoId: videoId,
                    width: 140,
                    height: 140,
                    radius: 18,
                  ),
                  const SizedBox(width: 24),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Song',
                            style: TextStyle(
                                color: Colors.white,
                                fontSize: 12,
                                fontWeight: FontWeight.bold)),
                        const SizedBox(height: 8),
                        Text(track['title'] ?? 'Unknown',
                            style: const TextStyle(
                                color: Colors.white,
                                fontSize: 32,
                                fontWeight: FontWeight.bold),
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis),
                        const SizedBox(height: 8),
                        if (details != null &&
                            ((details['artist'] ?? details['author'] ?? '')
                                .toString()
                                .isNotEmpty))
                          Row(
                            children: [
                              const Icon(Icons.person,
                                  color: Colors.white70, size: 16),
                              const SizedBox(width: 4),
                              Expanded(
                                child: Builder(builder: (context) {
                                  String a = (details['artist'] == null ||
                                          details['artist'] == 'null')
                                      ? (details['author'] ?? '')
                                      : details['artist'];
                                  if (a.isEmpty) {
                                    a = track['author'] ??
                                        track['channel'] ??
                                        '';
                                  }
                                  String d = details['release_date'] ?? '';
                                  if (d == 'null') d = '';
                                  if (d.contains('T')) d = d.split('T').first;
                                  String text = [a, d]
                                      .where((e) => e.isNotEmpty)
                                      .join(' • ');
                                  return Text(text,
                                      style: const TextStyle(
                                          color: Colors.white70, fontSize: 14),
                                      maxLines: 2,
                                      overflow: TextOverflow.ellipsis);
                                }),
                              ),
                            ],
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),

            if (albumTitle != null)
              Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 24, vertical: 8),
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(_radiusLarge),
                    onTap: albumId == null
                        ? null
                        : () {
                            final resolvedAlbumId = albumId;
                            unawaited(
                              ref
                                  .read(albumDetailsProvider.notifier)
                                  .fetchAlbum(resolvedAlbumId),
                            );
                            Navigator.of(context).push(
                              MaterialPageRoute(
                                builder: (_) => AlbumDetailsScreen(
                                  albumId: resolvedAlbumId,
                                  fallbackAlbum: {
                                    'id': resolvedAlbumId,
                                    'title': albumTitle,
                                    'artist': albumArtist,
                                    'thumbnail': track['thumbnail'],
                                    'year': '',
                                  },
                                ),
                              ),
                            );
                          },
                    child: Ink(
                      padding: const EdgeInsets.all(18),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.04),
                        borderRadius: BorderRadius.circular(_radiusLarge),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.07),
                        ),
                      ),
                      child: Row(
                        children: [
                          Container(
                            width: 46,
                            height: 46,
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.06),
                              borderRadius: BorderRadius.circular(16),
                            ),
                            child: Icon(
                              Icons.album_rounded,
                              color: Colors.white.withValues(alpha: 0.78),
                            ),
                          ),
                          const SizedBox(width: 14),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  'From the album',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.48),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  albumTitle,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Colors.white,
                                    fontSize: 16,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          Icon(
                            Icons.chevron_right_rounded,
                            color: Colors.white.withValues(alpha: 0.42),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),

            // Play & Download Buttons
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
              child: Row(
                children: [
                  IconButton(
                    iconSize: 56,
                    icon:
                        const Icon(Icons.play_circle_fill, color: Colors.white),
                    onPressed: () {
                      unawaited(ref
                          .read(playbackQueueProvider.notifier)
                          .startRadioSession(track));
                      Navigator.of(context).push(MaterialPageRoute(
                          builder: (_) => const FullPlayerScreen()));
                    },
                  ),
                  const SizedBox(width: 16),
                  IconButton(
                    iconSize: 36,
                    icon: const Icon(Icons.playlist_add, color: Colors.white70),
                    onPressed: () {
                      showAddToPlaylistDialog(
                        context: context,
                        track: {
                          'id': track['id'] ?? track['videoId'],
                          'videoId': track['id'] ?? track['videoId'],
                          'title': track['title'],
                          'thumbnail': track['thumbnail'],
                          'channel': track['channel'] ?? track['author'],
                          'duration': track['duration'],
                        },
                      );
                    },
                  ),
                  const SizedBox(width: 8),
                  IconButton(
                    iconSize: 36,
                    icon: downloadTask?.phase == DownloadPhase.active
                        ? SizedBox(
                            width: 34,
                            height: 34,
                            child: CircularProgressIndicator(
                              value: downloadTask!.progress > 0
                                  ? downloadTask.progress
                                  : null,
                              strokeWidth: 2.2,
                              color: _accentGrey,
                            ),
                          )
                        : Icon(
                            downloadTask?.phase == DownloadPhase.complete
                                ? Icons.check_circle_outline
                                : Icons.download_for_offline,
                            color: Colors.white70,
                          ),
                    onPressed: videoId == null
                        ? null
                        : () {
                            ref
                                .read(downloadCenterProvider.notifier)
                                .downloadTrack(track);
                          },
                  ),
                ],
              ),
            ),

            // Similar Tracks
            if (details == null)
              const Center(
                  child: Padding(
                      padding: EdgeInsets.all(32),
                      child: CircularProgressIndicator(color: Colors.white)))
            else if (details['similar_tracks'] != null &&
                (details['similar_tracks'] as List).isNotEmpty) ...[
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 24, vertical: 8),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Text('Similar Tracks',
                      style: TextStyle(
                          color: Colors.white,
                          fontSize: 20,
                          fontWeight: FontWeight.bold)),
                ),
              ),
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                padding: const EdgeInsets.symmetric(horizontal: 16),
                itemCount: (details['similar_tracks'] as List).length,
                itemBuilder: (context, i) {
                  final st = details['similar_tracks'][i];
                  return ListTile(
                    onTap: () {
                      ref
                          .read(trackDetailsProvider.notifier)
                          .fetchDetails(st['id']);
                      Navigator.pushReplacement(
                          context,
                          MaterialPageRoute(
                              builder: (_) => TrackDetailsScreen(track: st)));
                    },
                    leading: ClipRRect(
                      borderRadius: BorderRadius.circular(4),
                      child: AppArtwork(
                        thumbnail: st['thumbnail'],
                        videoId: st['id']?.toString(),
                        width: 48,
                        height: 48,
                        radius: 10,
                      ),
                    ),
                    title: Text(st['title'] ?? '',
                        style: const TextStyle(color: Colors.white),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                    subtitle: Text(st['channel'] ?? '',
                        style: const TextStyle(color: Colors.white54),
                        maxLines: 1),
                  );
                },
              ),
            ],
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}
