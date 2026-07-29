import 'package:flutter/material.dart';

import '../../logic/download_provider.dart';
import '../../logic/track_metadata.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';
import '../playlist/add_to_playlist_dialog.dart';

class ArtistHeroSection extends StatelessWidget {
  final Map<String, dynamic> artist;
  final String description;
  final List<Map<String, dynamic>> stats;
  final double radiusLarge;

  const ArtistHeroSection({
    super.key,
    required this.artist,
    required this.description,
    required this.stats,
    required this.radiusLarge,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Center(
          child: ArtistArtwork(
            thumbnail: artist['thumbnail'],
            width: 220,
            height: 220,
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
          DetailsStatsWrap(stats: stats),
        ],
      ],
    );
  }
}

class AlbumHeroSection extends StatelessWidget {
  final Map<String, dynamic> album;
  final List<dynamic> tracks;

  const AlbumHeroSection({
    super.key,
    required this.album,
    required this.tracks,
  });

  @override
  Widget build(BuildContext context) {
    final duration = formatCollectionDuration(tracks);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Center(
          child: CollectionVinylArtwork(
            thumbnail: album['thumbnail'],
            videoId: tracks.isNotEmpty ? extractTrackId(tracks.first) : null,
            size: 244,
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
            if ((album['release_year'] ?? album['year'] ?? '')
                .toString()
                .isNotEmpty)
              (album['release_year'] ?? album['year']).toString(),
            if (duration.isNotEmpty) duration,
            if (tracks.isNotEmpty) '${tracks.length} tracks',
          ].join(' / '),
          style: TextStyle(
            color: Colors.white.withValues(alpha: 0.56),
            fontSize: 14,
          ),
        ),
      ],
    );
  }
}

class CollectionVinylArtwork extends StatelessWidget {
  const CollectionVinylArtwork({
    super.key,
    required this.thumbnail,
    this.videoId,
    this.size = 220,
    this.cover,
  });

  final dynamic thumbnail;
  final String? videoId;
  final double size;
  final Widget? cover;

  @override
  Widget build(BuildContext context) {
    final coverSize = size * 0.74;
    final discSize = size * 0.68;
    return SizedBox(
      width: size,
      height: size * 0.82,
      child: Stack(
        alignment: Alignment.center,
        children: [
          Positioned(
            right: size * 0.03,
            child: Container(
              width: discSize,
              height: discSize,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: const Color(0xFF171717),
                border: Border.all(color: const Color(0xFF303030), width: 2),
                boxShadow: const [
                  BoxShadow(
                    color: Colors.black54,
                    blurRadius: 20,
                    offset: Offset(8, 10),
                  ),
                ],
              ),
              child: Stack(
                alignment: Alignment.center,
                children: [
                  for (final factor in const [0.86, 0.68, 0.50])
                    Container(
                      width: discSize * factor,
                      height: discSize * factor,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.055),
                        ),
                      ),
                    ),
                  Container(
                    width: discSize * 0.25,
                    height: discSize * 0.25,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: neatieActive.withValues(alpha: 0.72),
                    ),
                  ),
                  Container(
                    width: 6,
                    height: 6,
                    decoration: const BoxDecoration(
                      color: Colors.black,
                      shape: BoxShape.circle,
                    ),
                  ),
                ],
              ),
            ),
          ),
          Positioned(
            left: size * 0.03,
            child: DecoratedBox(
              decoration: const BoxDecoration(
                boxShadow: [
                  BoxShadow(
                    color: Colors.black87,
                    blurRadius: 22,
                    offset: Offset(0, 12),
                  ),
                ],
              ),
              child: SizedBox(
                width: coverSize,
                height: coverSize,
                child: cover ??
                    AppArtwork(
                      thumbnail: thumbnail,
                      videoId: videoId,
                      width: coverSize,
                      height: coverSize,
                      radius: 0,
                    ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class CollectionIconActionsRow extends StatelessWidget {
  const CollectionIconActionsRow({
    super.key,
    required this.onPlay,
    required this.onShuffle,
    required this.onLike,
    required this.isLiked,
    this.onPrime,
    this.onSecondary,
    this.secondaryIcon = Icons.more_horiz_rounded,
  });

  final VoidCallback? onPlay;
  final VoidCallback? onShuffle;
  final VoidCallback? onLike;
  final bool isLiked;
  final VoidCallback? onPrime;
  final VoidCallback? onSecondary;
  final IconData secondaryIcon;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        IconButton(
          tooltip: isLiked ? 'Unlike' : 'Like',
          onPressed: onLike,
          icon: Icon(
            isLiked ? Icons.favorite_rounded : Icons.favorite_border_rounded,
            color: isLiked ? const Color(0xFFE48B94) : Colors.white,
          ),
        ),
        if (onSecondary != null)
          IconButton(
            tooltip: 'More',
            onPressed: onSecondary,
            icon: Icon(secondaryIcon, color: Colors.white70),
          ),
        const Spacer(),
        IconButton(
          tooltip: 'Shuffle',
          onPressed: onShuffle,
          icon: const Icon(Icons.shuffle_rounded, color: Colors.white),
        ),
        const SizedBox(width: 10),
        Listener(
          onPointerDown: onPrime == null ? null : (_) => onPrime!(),
          child: IconButton.filled(
            tooltip: 'Play',
            style: IconButton.styleFrom(
              backgroundColor: Colors.white,
              foregroundColor: Colors.black,
              minimumSize: const Size(58, 58),
            ),
            onPressed: onPlay,
            icon: const Icon(Icons.play_arrow_rounded, size: 34),
          ),
        ),
      ],
    );
  }
}

class DetailsPlayShuffleActionsRow extends StatelessWidget {
  final String playLabel;
  final VoidCallback? onPlay;
  final VoidCallback? onShuffle;
  final VoidCallback? onPrime;
  final Color surfaceColor;

  const DetailsPlayShuffleActionsRow({
    super.key,
    required this.playLabel,
    required this.onPlay,
    required this.onShuffle,
    required this.surfaceColor,
    this.onPrime,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Listener(
            onPointerDown: onPrime == null ? null : (_) => onPrime!(),
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: surfaceColor,
                foregroundColor: Colors.white,
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
              onPressed: onPlay,
              child: Text(playLabel),
            ),
          ),
        ),
        const SizedBox(width: 12),
        OutlinedButton.icon(
          style: OutlinedButton.styleFrom(
            foregroundColor: Colors.white,
            side: BorderSide(color: Colors.white.withValues(alpha: 0.14)),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          ),
          onPressed: onShuffle,
          icon: const Icon(Icons.shuffle_rounded),
          label: const Text('Shuffle'),
        ),
      ],
    );
  }
}

class DetailsStatsWrap extends StatelessWidget {
  final List<Map<String, dynamic>> stats;

  const DetailsStatsWrap({super.key, required this.stats});

  @override
  Widget build(BuildContext context) {
    if (stats.isEmpty) return const SizedBox.shrink();
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: stats.map((stat) {
        final label = (stat['label'] ?? '').toString();
        final value = (stat['value'] ?? '').toString();
        return NeatiePill(label: '$label / $value');
      }).toList(growable: false),
    );
  }
}

class ArtistAlbumsSection extends StatelessWidget {
  final List<dynamic> albums;
  final double radiusLarge;
  final Future<void> Function(Map<String, dynamic> album) onOpenAlbum;

  const ArtistAlbumsSection({
    super.key,
    required this.albums,
    required this.radiusLarge,
    required this.onOpenAlbum,
  });

  @override
  Widget build(BuildContext context) {
    if (albums.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
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
              final album = Map<String, dynamic>.from(albums[index] as Map);
              return SizedBox(
                width: 170,
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(radiusLarge),
                    onTap: () => onOpenAlbum(album),
                    child: NeatieSurface(
                      padding: const EdgeInsets.all(12),
                      radius: radiusLarge,
                      color: Colors.white.withValues(alpha: 0.035),
                      blur: false,
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
                            album['title']?.toString() ?? 'Unknown Album',
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
                              if ((album['year'] ?? '').toString().isNotEmpty)
                                album['year'].toString(),
                              if ((album['track_count'] ?? 0) > 0)
                                '${album['track_count']} tracks',
                            ].join(' / '),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.52),
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
    );
  }
}

class RelatedArtistsSection extends StatelessWidget {
  final List<dynamic> artists;
  final Future<void> Function(Map<String, dynamic> artist) onOpenArtist;

  const RelatedArtistsSection({
    super.key,
    required this.artists,
    required this.onOpenArtist,
  });

  @override
  Widget build(BuildContext context) {
    if (artists.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
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
            itemCount: artists.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final related = Map<String, dynamic>.from(artists[index] as Map);
              return SizedBox(
                width: 82,
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(22),
                    onTap: () => onOpenArtist(related),
                    child: Column(
                      children: [
                        ArtistArtwork(
                          thumbnail: related['thumbnail'],
                          width: 68,
                          height: 68,
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
    );
  }
}

class DetailsTrackListSection extends StatelessWidget {
  final String title;
  final List<dynamic> tracks;
  final bool isLoading;
  final String emptyMessage;
  final String fallbackSubtitle;
  final String? fallbackThumbnail;
  final Future<void> Function(Map<String, dynamic> track) onPlayTrack;
  final ValueChanged<Map<String, dynamic>>? onPrimeTrack;

  const DetailsTrackListSection({
    super.key,
    required this.title,
    required this.tracks,
    required this.isLoading,
    required this.emptyMessage,
    required this.fallbackSubtitle,
    required this.onPlayTrack,
    this.onPrimeTrack,
    this.fallbackThumbnail,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: const TextStyle(
            color: Colors.white,
            fontSize: 18,
            fontWeight: FontWeight.w700,
          ),
        ),
        const SizedBox(height: 16),
        if (isLoading && tracks.isEmpty)
          const Center(
            child: Padding(
              padding: EdgeInsets.symmetric(vertical: 32),
              child: CircularProgressIndicator(color: Color(0xFF8E8E93)),
            ),
          )
        else if (tracks.isEmpty)
          Text(
            emptyMessage,
            style: TextStyle(color: Colors.white.withValues(alpha: 0.56)),
          )
        else
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: tracks.length,
            itemBuilder: (context, index) {
              final track = Map<String, dynamic>.from(tracks[index] as Map);
              final videoId = (track['id'] ?? track['videoId'])?.toString();
              return NeatieSurface(
                margin: const EdgeInsets.only(bottom: 12),
                padding: const EdgeInsets.all(10),
                radius: neatieRadiusMedium,
                color: Colors.white.withValues(alpha: 0.035),
                blur: false,
                bordered: false,
                elevated: false,
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(neatieRadiusMedium),
                    onTap: () => onPlayTrack(track),
                    onTapDown: onPrimeTrack == null
                        ? null
                        : (_) => onPrimeTrack!(track),
                    child: Row(
                      children: [
                        AppArtwork(
                          thumbnail: track['thumbnail'] ?? fallbackThumbnail,
                          videoId: videoId,
                          width: 56,
                          height: 56,
                          radius: 14,
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                track['title'] ?? 'Unknown Track',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                track['channel'] ??
                                    track['author'] ??
                                    fallbackSubtitle,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: neatieMutedText,
                                  fontSize: 12,
                                ),
                              ),
                            ],
                          ),
                        ),
                        IconButton(
                          onPressed: () => showAddToPlaylistDialog(
                            context: context,
                            track: track,
                          ),
                          icon: const Icon(
                            Icons.playlist_add_rounded,
                            color: neatieMutedText,
                          ),
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

class TrackDetailsHeaderSection extends StatelessWidget {
  final Map<String, dynamic> track;
  final Map<String, dynamic>? details;

  const TrackDetailsHeaderSection({
    super.key,
    required this.track,
    required this.details,
  });

  @override
  Widget build(BuildContext context) {
    final videoId = (track['id'] ?? track['videoId'])?.toString();
    return Container(
      padding: const EdgeInsets.all(24),
      color: const Color(0xFF050506),
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
                const Text(
                  'Song',
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 12,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  track['title'] ?? 'Unknown',
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 32,
                    fontWeight: FontWeight.bold,
                  ),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 8),
                if (details != null &&
                    ((details!['artist'] ?? details!['author'] ?? '')
                        .toString()
                        .isNotEmpty))
                  Row(
                    children: [
                      const Icon(Icons.person, color: Colors.white70, size: 16),
                      const SizedBox(width: 4),
                      Expanded(
                        child: Builder(
                          builder: (context) {
                            String artist = (details!['artist'] == null ||
                                    details!['artist'] == 'null')
                                ? (details!['author'] ?? '')
                                : details!['artist'];
                            if (artist.isEmpty) {
                              artist = track['author'] ?? track['channel'] ?? '';
                            }
                            String releaseDate = details!['release_date'] ?? '';
                            if (releaseDate == 'null') releaseDate = '';
                            if (releaseDate.contains('T')) {
                              releaseDate = releaseDate.split('T').first;
                            }
                            final text = [artist, releaseDate]
                                .where((entry) => entry.isNotEmpty)
                                .join(' / ');
                            return Text(
                              text,
                              style: const TextStyle(
                                color: Colors.white70,
                                fontSize: 14,
                              ),
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            );
                          },
                        ),
                      ),
                    ],
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class TrackAlbumLinkCard extends StatelessWidget {
  final String albumTitle;
  final VoidCallback? onTap;
  final double radiusLarge;

  const TrackAlbumLinkCard({
    super.key,
    required this.albumTitle,
    required this.radiusLarge,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 8),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(radiusLarge),
          onTap: onTap,
          child: NeatieSurface(
            padding: const EdgeInsets.all(18),
            radius: radiusLarge,
            color: Colors.white.withValues(alpha: 0.04),
            blur: false,
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
    );
  }
}

class TrackDetailsActionsRow extends StatelessWidget {
  final Map<String, dynamic> track;
  final DownloadTask? downloadTask;
  final VoidCallback onPlay;
  final VoidCallback? onPrime;
  final VoidCallback? onDownload;
  final Color accentColor;

  const TrackDetailsActionsRow({
    super.key,
    required this.track,
    required this.downloadTask,
    required this.onPlay,
    this.onPrime,
    required this.onDownload,
    required this.accentColor,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
      child: Row(
        children: [
          Listener(
            onPointerDown: onPrime == null ? null : (_) => onPrime!(),
            child: IconButton(
              iconSize: 56,
              icon: const Icon(Icons.play_circle_fill, color: Colors.white),
              onPressed: onPlay,
            ),
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
                          ? downloadTask!.progress
                          : null,
                      strokeWidth: 2.2,
                      color: accentColor,
                    ),
                  )
                : Icon(
                    downloadTask?.phase == DownloadPhase.complete
                        ? Icons.check_circle_outline
                        : Icons.download_for_offline,
                    color: Colors.white70,
                  ),
            onPressed: onDownload,
          ),
        ],
      ),
    );
  }
}

class SimilarTracksSection extends StatelessWidget {
  final List<dynamic> tracks;
  final ValueChanged<Map<String, dynamic>> onOpenTrack;

  const SimilarTracksSection({
    super.key,
    required this.tracks,
    required this.onOpenTrack,
  });

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    return Column(
      children: [
        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 24, vertical: 8),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Text(
              'Similar Tracks',
              style: TextStyle(
                color: Colors.white,
                fontSize: 20,
                fontWeight: FontWeight.bold,
              ),
            ),
          ),
        ),
        ListView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(horizontal: 16),
          itemCount: tracks.length,
          itemBuilder: (context, index) {
            final similarTrack = Map<String, dynamic>.from(tracks[index] as Map);
            return NeatieSurface(
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(10),
              radius: neatieRadiusMedium,
              color: Colors.white.withValues(alpha: 0.035),
              blur: false,
              bordered: false,
              elevated: false,
              child: Material(
                color: Colors.transparent,
                child: InkWell(
                  borderRadius: BorderRadius.circular(neatieRadiusMedium),
                  onTap: () => onOpenTrack(similarTrack),
                  child: Row(
                    children: [
                      AppArtwork(
                        thumbnail: similarTrack['thumbnail'],
                        videoId: similarTrack['id']?.toString(),
                        width: 48,
                        height: 48,
                        radius: 10,
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              similarTrack['title'] ?? '',
                              style: const TextStyle(
                                color: Colors.white,
                                fontWeight: FontWeight.w700,
                              ),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            const SizedBox(height: 4),
                            Text(
                              similarTrack['channel'] ?? '',
                              style: const TextStyle(color: neatieMutedText),
                              maxLines: 1,
                            ),
                          ],
                        ),
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
