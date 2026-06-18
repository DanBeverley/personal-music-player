import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/playlist_provider.dart';
import '../main_dialogs.dart';
import '../navigation/player_navigation.dart';
import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';
import '../widgets/app_artwork.dart';

const _voidBlack = neatieInk;
const double _radiusLarge = neatieRadiusLarge;

class PlaylistDetailScreen extends ConsumerStatefulWidget {
  final String playlistId;

  const PlaylistDetailScreen({super.key, required this.playlistId});

  @override
  ConsumerState<PlaylistDetailScreen> createState() =>
      _PlaylistDetailScreenState();
}

class _PlaylistDetailScreenState extends ConsumerState<PlaylistDetailScreen> {
  final _searchCtrl = TextEditingController();
  List<dynamic> _searchResults = [];
  bool _isSearching = false;
  final Set<String> _primedPlaylistIds = <String>{};
  int _searchRequestVersion = 0;

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  void _primePlaylistTracks(Iterable<dynamic> tracks, {int limit = 8}) {
    final idsToWarm = <String>[];
    for (final track in tracks.take(limit)) {
      final videoId = (track['id'] ?? track['videoId'])?.toString();
      if (videoId == null || videoId.isEmpty) continue;
      if (_primedPlaylistIds.add(videoId)) {
        idsToWarm.add(videoId);
      }
    }
    if (idsToWarm.isEmpty) return;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(idsToWarm));
  }

  Future<void> _search(String q) async {
    if (q.isEmpty) return;
    final requestVersion = ++_searchRequestVersion;
    setState(() => _isSearching = true);
    try {
      final fetchResult = await fetchSearchPayload(
        ref.read,
        q,
        limit: 12,
        timeout: const Duration(seconds: 10),
        deferSideSurfaces: true,
      );
      if (requestVersion != _searchRequestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final results = (payload['results'] as List<dynamic>? ?? const []);
        setState(() => _searchResults = results);
        _primePlaylistTracks(results, limit: 8);
      } else {
        setState(() => _searchResults = []);
      }
    } catch (_) {
      if (requestVersion == _searchRequestVersion && mounted) {
        setState(() => _searchResults = []);
      }
    }
    if (requestVersion != _searchRequestVersion || !mounted) return;
    setState(() => _isSearching = false);
  }

  Future<void> _playPlaylistTrack(
    Playlist playlist,
    Map<String, dynamic> track,
  ) async {
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: widget.playlistId,
          playlistName: playlist.name,
          tracks: playlist.tracks,
          currentTrack: track,
        );
    if (!mounted) return;
    unawaited(openFullPlayer(context));
  }

  void _addSearchResultToPlaylist(Playlist playlist, dynamic track) {
    ref.read(playlistProvider.notifier).addTrackToPlaylist(
          widget.playlistId,
          track,
        );
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Added to ${playlist.name}'),
        duration: const Duration(seconds: 1),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final playlists = ref.watch(playlistProvider);
    final playlist = playlists.firstWhere(
      (p) => p.id == widget.playlistId,
      orElse: () => Playlist(id: '', name: 'Deleted Playlist', tracks: []),
    );
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primePlaylistTracks(playlist.tracks);
    });

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: Text(
          playlist.name,
          style: const TextStyle(color: Colors.white),
        ),
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _PlaylistHeaderCard(
              playlist: playlist,
              radiusLarge: _radiusLarge,
            ),
            const SizedBox(height: 16),
            NeatieSurface(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              color: Colors.white.withValues(alpha: 0.04),
              blur: false,
              child: TextField(
                controller: _searchCtrl,
                style: const TextStyle(color: Colors.white),
                decoration: InputDecoration(
                  hintText: "Let's find something for your playlist",
                  hintStyle: const TextStyle(color: neatieDimText),
                  border: InputBorder.none,
                  suffixIcon: IconButton(
                    icon: const Icon(Icons.search, color: Colors.white),
                    onPressed: () => _search(_searchCtrl.text),
                  ),
                ),
                onSubmitted: _search,
              ),
            ),
            const SizedBox(height: 16),
            _PlaylistSearchResultsSection(
              isSearching: _isSearching,
              searchResults: _searchResults,
              onAddTrack: (track) =>
                  _addSearchResultToPlaylist(playlist, track),
            ),
            _PlaylistTracksSection(
              playlistId: widget.playlistId,
              playlist: playlist,
              onPlayTrack: (track) => _playPlaylistTrack(playlist, track),
            ),
          ],
        ),
      ),
    );
  }
}

class _PlaylistHeaderCard extends StatelessWidget {
  final Playlist playlist;
  final double radiusLarge;

  const _PlaylistHeaderCard({
    required this.playlist,
    required this.radiusLarge,
  });

  @override
  Widget build(BuildContext context) {
    final hiddenCount = playlist.tracks.where(isTrackHidden).length;
    return NeatieSurface(
      padding: const EdgeInsets.all(18),
      radius: radiusLarge,
      color: Colors.white.withValues(alpha: 0.035),
      blur: false,
      child: Row(
        children: [
          PlaylistArtworkView(
            playlist: playlist,
            size: 96,
            radius: 22,
            onTap: () => showPlaylistArtworkDialog(
              context: context,
              playlist: playlist,
            ),
          ),
          const SizedBox(width: 18),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  playlist.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 24,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  hiddenCount > 0
                      ? '${playlist.tracks.length} tracks - $hiddenCount skipped'
                      : '${playlist.tracks.length} tracks',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.56),
                    fontSize: 13,
                  ),
                ),
                const SizedBox(height: 14),
                TextButton.icon(
                  onPressed: () => showPlaylistArtworkDialog(
                    context: context,
                    playlist: playlist,
                  ),
                  icon: const Icon(Icons.photo_outlined, size: 18),
                  label: const Text('Edit artwork'),
                  style: TextButton.styleFrom(
                    foregroundColor: Colors.white,
                    backgroundColor: Colors.white.withValues(alpha: 0.05),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 12,
                    ),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(18),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _PlaylistSearchResultsSection extends StatelessWidget {
  final bool isSearching;
  final List<dynamic> searchResults;
  final ValueChanged<dynamic> onAddTrack;

  const _PlaylistSearchResultsSection({
    required this.isSearching,
    required this.searchResults,
    required this.onAddTrack,
  });

  @override
  Widget build(BuildContext context) {
    if (isSearching) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 8),
          child: CircularProgressIndicator(color: Colors.white),
        ),
      );
    }
    if (searchResults.isEmpty) {
      return const SizedBox.shrink();
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Search Results',
          style: TextStyle(
            color: Colors.white,
            fontSize: 18,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 8),
        ListView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: searchResults.length,
          itemBuilder: (context, index) {
            final track = searchResults[index];
            final videoId = (track['id'] ?? track['videoId'])?.toString();
            return NeatieSurface(
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(10),
              radius: neatieRadiusMedium,
              color: Colors.white.withValues(alpha: 0.035),
              blur: false,
              child: Row(
                children: [
                  AppArtwork(
                    thumbnail: track['thumbnail'],
                    videoId: videoId,
                    width: 50,
                    height: 50,
                    radius: 14,
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          track['title'] ?? 'Unknown',
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w700,
                          ),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        const SizedBox(height: 4),
                        Text(
                          track['channel'] ?? '',
                          style: const TextStyle(color: neatieMutedText),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                    icon: const Icon(
                      Icons.add_circle_outline,
                      color: Colors.white,
                    ),
                    onPressed: () => onAddTrack(track),
                  ),
                ],
              ),
            );
          },
        ),
        const Divider(color: Colors.white24, height: 32),
      ],
    );
  }
}

class _PlaylistTracksSection extends ConsumerWidget {
  final String playlistId;
  final Playlist playlist;
  final ValueChanged<Map<String, dynamic>> onPlayTrack;

  const _PlaylistTracksSection({
    required this.playlistId,
    required this.playlist,
    required this.onPlayTrack,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Playlist Tracks',
          style: TextStyle(
            color: Colors.white,
            fontSize: 18,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 8),
        if (playlist.tracks.isEmpty)
          const Text(
            'No tracks added yet.',
            style: TextStyle(color: Colors.white54),
          ),
        ListView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: playlist.tracks.length,
          itemBuilder: (context, index) {
            final track = playlist.tracks[index];
            final videoId = (track['id'] ?? track['videoId'])?.toString();
            return NeatieSurface(
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(10),
              radius: neatieRadiusMedium,
              color: Colors.white.withValues(alpha: 0.035),
              blur: false,
              child: Material(
                color: Colors.transparent,
                child: InkWell(
                  borderRadius: BorderRadius.circular(neatieRadiusMedium),
                  onTap: () => onPlayTrack(track),
                  child: Row(
                    children: [
                      AppArtwork(
                        thumbnail: track['thumbnail'],
                        videoId: videoId,
                        width: 50,
                        height: 50,
                        radius: 14,
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              track['title'] ?? 'Unknown',
                              style: const TextStyle(
                                color: Colors.white,
                                fontWeight: FontWeight.w700,
                              ),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            const SizedBox(height: 4),
                            Text(
                              track['channel'] ?? track['author'] ?? '',
                              style: const TextStyle(color: neatieMutedText),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                          ],
                        ),
                      ),
                      PopupMenuButton<String>(
                        icon: const Icon(
                          Icons.more_vert,
                          color: neatieMutedText,
                        ),
                        color: neatieRaised,
                        onSelected: (value) {
                          if (value == 'remove') {
                            if (videoId == null || videoId.isEmpty) return;
                            ref
                                .read(playlistProvider.notifier)
                                .removeTrackFromPlaylist(playlistId, videoId);
                          }
                        },
                        itemBuilder: (_) => const [
                          PopupMenuItem(
                            value: 'remove',
                            child: Text(
                              'Remove from Playlist',
                              style: TextStyle(color: Colors.red),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            );
          },
        ),
      ],
    );
  }
}
