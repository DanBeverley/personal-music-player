import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';
import '../track_list_skeleton.dart';
import 'track_menu_button.dart';

class HomeAlbumList extends StatelessWidget {
  const HomeAlbumList({
    super.key,
    required this.albums,
    required this.isLoading,
    required this.onOpenAlbum,
  });

  final List<Map<String, dynamic>> albums;
  final bool isLoading;
  final ValueChanged<Map<String, dynamic>> onOpenAlbum;

  @override
  Widget build(BuildContext context) {
    if (albums.isEmpty && isLoading) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: CircularProgressIndicator(color: neatieActive),
        ),
      );
    }
    if (albums.isEmpty) {
      return const SizedBox.shrink();
    }

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: albums.length,
      itemBuilder: (context, index) {
        final album = albums[index];
        return NeatieSurface(
          margin: const EdgeInsets.only(bottom: 14),
          radius: neatieRadiusLarge,
          color: Colors.white.withValues(alpha: 0.035),
          blur: false,
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(neatieRadiusLarge),
              onTap: () => onOpenAlbum(album),
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: album['thumbnail'],
                      width: 78,
                      height: 78,
                      radius: 20,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const NeatiePill(
                            label: 'Album',
                            icon: Icons.album_outlined,
                          ),
                          const SizedBox(height: 10),
                          Text(
                            album['title'] ?? 'Unknown Album',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            album['artist'] ?? 'Unknown Artist',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.62),
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            [
                              if ((album['year'] ?? '').toString().isNotEmpty)
                                album['year'].toString(),
                              if ((album['track_count'] ?? 0) > 0)
                                '${album['track_count']} tracks',
                            ].join(' • '),
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.42),
                              fontSize: 12,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ],
                      ),
                    ),
                    Icon(
                      Icons.chevron_right_rounded,
                      color: Colors.white.withValues(alpha: 0.52),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

class HomeArtistRow extends StatelessWidget {
  const HomeArtistRow({
    super.key,
    required this.artists,
    required this.title,
    required this.onOpenArtist,
    this.showTitle = true,
    this.isLoading = false,
  });

  final List<Map<String, dynamic>> artists;
  final String title;
  final ValueChanged<Map<String, dynamic>> onOpenArtist;
  final bool showTitle;
  final bool isLoading;

  @override
  Widget build(BuildContext context) {
    if (artists.isEmpty && !isLoading) {
      return const SizedBox.shrink();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (showTitle) ...[
          Text(
            title,
            style: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
          const SizedBox(height: 14),
        ],
        SizedBox(
          height: 112,
          child: isLoading && artists.isEmpty
              ? ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: 5,
                  separatorBuilder: (_, __) => const SizedBox(width: 14),
                  itemBuilder: (_, __) => Column(
                    children: [
                      Container(
                        width: 68,
                        height: 68,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          shape: BoxShape.circle,
                        ),
                      ),
                      const SizedBox(height: 10),
                      Container(
                        width: 58,
                        height: 10,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          borderRadius: BorderRadius.circular(999),
                        ),
                      ),
                    ],
                  ),
                )
              : ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: artists.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 14),
                  itemBuilder: (context, index) {
                    final artist = artists[index];
                    return SizedBox(
                      width: 82,
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(22),
                          onTap: () => onOpenArtist(artist),
                          child: Column(
                            children: [
                              AppArtwork(
                                thumbnail: artist['thumbnail'],
                                width: 68,
                                height: 68,
                                radius: 999,
                              ),
                              const SizedBox(height: 10),
                              Text(
                                artist['name']?.toString() ?? 'Artist',
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

class HomeTrackList extends StatelessWidget {
  const HomeTrackList({
    super.key,
    required this.tracks,
    required this.isLoading,
    required this.emptyMessage,
    required this.onWarmTrack,
    required this.onPlayTrack,
    required this.onPlayDiscoveryTrack,
    required this.onOpenDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.playlistId,
    this.playlistName,
    this.playAsDiscoveryMix = false,
    this.showTrailingLoader = false,
    this.onNearEnd,
    this.nearEndLead = 2,
  });

  final List<dynamic> tracks;
  final bool isLoading;
  final String emptyMessage;
  final String? playlistId;
  final String? playlistName;
  final bool playAsDiscoveryMix;
  final bool showTrailingLoader;
  final VoidCallback? onNearEnd;
  final int nearEndLead;
  final ValueChanged<String?> onWarmTrack;
  final Future<void> Function(
    List<Map<String, dynamic>> normalizedTracks,
    Map<String, dynamic> track,
    String playlistId,
    String playlistName,
  ) onPlayTrack;
  final Future<void> Function(
    Map<String, dynamic> track,
    String sessionName,
  ) onPlayDiscoveryTrack;
  final Future<void> Function(Map<String, dynamic> track, String? videoId)
      onOpenDetails;
  final Future<void> Function(Map<String, dynamic> track) onAddToPlaylist;
  final Future<void> Function(Map<String, dynamic> track) onStartStation;

  @override
  Widget build(BuildContext context) {
    if (isLoading && tracks.isEmpty) {
      return const TrackListSkeleton(count: 5);
    }
    if (tracks.isEmpty) {
      return Center(
        child: Text(
          emptyMessage,
          style: const TextStyle(color: Colors.white54),
        ),
      );
    }

    final normalizedTracks =
        tracks.map((track) => Map<String, dynamic>.from(track)).toList(
              growable: false,
            );
    final loadingTileCount = showTrailingLoader ? 3 : 0;

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: tracks.length + loadingTileCount,
      itemBuilder: (context, index) {
        if (index >= tracks.length) {
          return const TrackListSkeleton(count: 1);
        }
        final nearEndIndex =
            math.max(0, tracks.length - math.max(1, nearEndLead));
        if (onNearEnd != null && index >= nearEndIndex) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            onNearEnd!();
          });
        }
        final rawTrack = Map<String, dynamic>.from(tracks[index] as Map);
        final videoId = (rawTrack['id'] ?? rawTrack['videoId'])?.toString();
        return NeatieSurface(
          margin: const EdgeInsets.only(bottom: 14),
          radius: neatieRadiusLarge,
          color: Colors.white.withValues(alpha: 0.035),
          blur: false,
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(neatieRadiusLarge),
              onTapDown: (_) => onWarmTrack(videoId),
              onTap: () {
                if (playAsDiscoveryMix) {
                  unawaited(
                    onPlayDiscoveryTrack(
                      rawTrack,
                      playlistName ?? 'Discovery mix',
                    ),
                  );
                  return;
                }
                unawaited(
                  onPlayTrack(
                    normalizedTracks,
                    rawTrack,
                    playlistId ??
                        'list:${emptyMessage.hashCode}:${tracks.length}',
                    playlistName ?? 'Track list',
                  ),
                );
              },
              child: Padding(
                padding: const EdgeInsets.all(12.0),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: rawTrack['thumbnail'],
                      videoId: videoId,
                      width: 74,
                      height: 74,
                      radius: neatieRadiusMedium,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            rawTrack['title'] ?? 'Unknown',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            rawTrack['channel'] ?? '',
                            maxLines: 1,
                            style: const TextStyle(
                              color: neatieDimText,
                              fontSize: 13,
                            ),
                          ),
                        ],
                      ),
                    ),
                    TrackMenuButton(
                      track: Map<String, dynamic>.from(rawTrack),
                      onOpenDetails: () => onOpenDetails(
                        Map<String, dynamic>.from(rawTrack),
                        videoId,
                      ),
                      onAddToPlaylist: onAddToPlaylist,
                      onStartStation: onStartStation,
                      buttonSize: 42,
                      iconSize: 20,
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}
