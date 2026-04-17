import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/details_provider.dart';
import '../ui/app_theme_tokens.dart';
import '../widgets/app_artwork.dart';
import '../widgets/home/track_menu_button.dart';
import '../widgets/playlist/add_to_playlist_dialog.dart';

class PersonalMixDetailScreen extends ConsumerWidget {
  final Map<String, dynamic> mix;
  final List<Map<String, dynamic>> tracks;
  final WidgetBuilder playerScreenBuilder;
  final Widget Function(Map<String, dynamic> track) trackDetailsScreenBuilder;
  final void Function(String videoId)? onPrimeTrack;

  const PersonalMixDetailScreen({
    super.key,
    required this.mix,
    required this.tracks,
    required this.playerScreenBuilder,
    required this.trackDetailsScreenBuilder,
    this.onPrimeTrack,
  });

  String _durationLabel(dynamic rawDuration) {
    final totalSeconds = int.tryParse('${rawDuration ?? ''}') ?? 0;
    if (totalSeconds <= 0) return '--:--';
    final minutes = totalSeconds ~/ 60;
    final seconds = totalSeconds % 60;
    return '$minutes:${seconds.toString().padLeft(2, '0')}';
  }

  Color _parseAccent(String? rawColor) {
    final value = rawColor?.trim() ?? '';
    final hex = value.replaceFirst('#', '');
    if (hex.length != 6) {
      return const Color(0xFF6C7BFF);
    }
    final parsed = int.tryParse('FF$hex', radix: 16);
    if (parsed == null) {
      return const Color(0xFF6C7BFF);
    }
    return Color(parsed);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final accent = _parseAccent(mix['accent_color']?.toString());
    final title = mix['title']?.toString().trim().isNotEmpty ?? false
        ? mix['title'].toString().trim()
        : 'Mix for you';
    final subtitle = mix['subtitle']?.toString().trim().isNotEmpty ?? false
        ? mix['subtitle'].toString().trim()
        : 'Built from your listening.';
    final description = mix['description']?.toString().trim().isNotEmpty ??
            false
        ? mix['description'].toString().trim()
        : 'A focused playlist shaped by your recent taste and repeat habits.';

    Future<void> openPlayer() async {
      if (!context.mounted) return;
      await Navigator.of(context).push(
        MaterialPageRoute(builder: playerScreenBuilder),
      );
    }

    Future<void> playTrack(Map<String, dynamic> track) async {
      await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
            playlistId: 'mix:${mix['id'] ?? title}',
            playlistName: title,
            tracks: tracks,
            currentTrack: track,
          );
      await openPlayer();
    }

    Future<void> openTrackDetails(Map<String, dynamic> track) async {
      final videoId = extractTrackId(track);
      if (videoId == null || videoId.isEmpty) return;
      onPrimeTrack?.call(videoId);
      ref.read(trackDetailsProvider.notifier).fetchDetails(videoId);
      if (!context.mounted) return;
      await Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => trackDetailsScreenBuilder(track),
        ),
      );
    }

    Future<void> addTrackToPlaylist(Map<String, dynamic> track) async {
      showAddToPlaylistDialog(
        context: context,
        track: Map<String, dynamic>.from(track),
      );
    }

    Future<void> startTrackStation(Map<String, dynamic> track) async {
      await ref.read(playbackQueueProvider.notifier).startRadioSession(track);
      await openPlayer();
    }

    return Scaffold(
      backgroundColor: appVoidBlack,
      appBar: AppBar(
        title: Text(title),
      ),
      body: CustomScrollView(
        physics: const BouncingScrollPhysics(),
        slivers: [
          SliverToBoxAdapter(
            child: Container(
              margin: const EdgeInsets.fromLTRB(16, 14, 16, 0),
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [
                    accent.withValues(alpha: 0.92),
                    accent.withValues(alpha: 0.68),
                    appSurfaceGrey,
                  ],
                ),
                borderRadius: BorderRadius.circular(28),
                border: Border.all(
                  color: Colors.white.withValues(alpha: 0.08),
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    mix['badge']?.toString() ?? 'MIX',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.72),
                      fontSize: 12,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 0.45,
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      AppArtwork(
                        thumbnail: mix['thumbnail'],
                        videoId: tracks.isNotEmpty
                            ? extractTrackId(tracks.first)
                            : null,
                        width: 116,
                        height: 116,
                        radius: 22,
                      ),
                      const SizedBox(width: 16),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              title,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 30,
                                fontWeight: FontWeight.w900,
                                height: 1.05,
                              ),
                            ),
                            const SizedBox(height: 8),
                            Text(
                              subtitle,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.78),
                                fontSize: 14,
                                height: 1.3,
                              ),
                            ),
                            const SizedBox(height: 12),
                            Text(
                              description,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.7),
                                fontSize: 12,
                                height: 1.35,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 18),
                  Row(
                    children: [
                      InkWell(
                        onTap: tracks.isEmpty
                            ? null
                            : () => playTrack(tracks.first),
                        borderRadius: BorderRadius.circular(999),
                        child: Container(
                          width: 56,
                          height: 56,
                          decoration: const BoxDecoration(
                            color: Colors.white,
                            shape: BoxShape.circle,
                          ),
                          child: Icon(
                            Icons.play_arrow_rounded,
                            color: accent,
                            size: 34,
                          ),
                        ),
                      ),
                      const SizedBox(width: 14),
                      Text(
                        '${tracks.length} tracks',
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.78),
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 18, 16, 22),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) {
                  final track = tracks[index];
                  final videoId = extractTrackId(track);
                  return Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.03),
                      borderRadius: BorderRadius.circular(22),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: 0.06),
                      ),
                    ),
                    child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                        borderRadius: BorderRadius.circular(22),
                        onTap: () => playTrack(track),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Row(
                            children: [
                              SizedBox(
                                width: 26,
                                child: Text(
                                  '${index + 1}',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.56),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                              const SizedBox(width: 8),
                              AppArtwork(
                                thumbnail: track['thumbnail'],
                                videoId: videoId,
                                width: 62,
                                height: 62,
                                radius: 18,
                              ),
                              const SizedBox(width: 14),
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      track['title']?.toString() ??
                                          'Unknown Track',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        color: Colors.white,
                                        fontSize: 15,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      (track['channel'] ??
                                                  track['author'] ??
                                                  track['artist'])
                                              ?.toString() ??
                                          'Unknown Artist',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: TextStyle(
                                        color: Colors.white
                                            .withValues(alpha: 0.68),
                                        fontSize: 12,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              const SizedBox(width: 8),
                              Text(
                                _durationLabel(track['duration']),
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.52),
                                  fontSize: 11,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              const SizedBox(width: 4),
                              TrackMenuButton(
                                track: Map<String, dynamic>.from(track),
                                onOpenDetails: () => openTrackDetails(track),
                                onAddToPlaylist: addTrackToPlaylist,
                                onStartStation: startTrackStation,
                                buttonSize: 38,
                                iconSize: 18,
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  );
                },
                childCount: tracks.length,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
