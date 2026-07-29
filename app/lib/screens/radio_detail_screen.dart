import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/collection_likes_provider.dart';
import '../logic/details_provider.dart';
import '../logic/download_provider.dart';
import '../logic/playlist_provider.dart';
import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';
import '../widgets/app_artwork.dart';
import '../widgets/home/track_menu_button.dart';
import '../widgets/playlist/add_to_playlist_dialog.dart';

class RadioDetailScreen extends ConsumerWidget {
  const RadioDetailScreen({
    super.key,
    required this.radio,
    required this.tracks,
    required this.playerScreenBuilder,
    required this.trackDetailsScreenBuilder,
    this.onPrimeTrack,
  });

  final Map<String, dynamic> radio;
  final List<Map<String, dynamic>> tracks;
  final WidgetBuilder playerScreenBuilder;
  final Widget Function(Map<String, dynamic> track) trackDetailsScreenBuilder;
  final void Function(String videoId)? onPrimeTrack;

  Color _parseAccent(String? rawColor) {
    final value = rawColor?.trim() ?? '';
    final hex = value.replaceFirst('#', '');
    if (hex.length == 6) {
      final parsed = int.tryParse('FF$hex', radix: 16);
      if (parsed != null) return Color(parsed);
    }
    if (hex.length == 8) {
      final parsed = int.tryParse(hex, radix: 16);
      if (parsed != null) return Color(parsed);
    }
    return const Color(0xFFD6784C);
  }

  String _radioTitle() {
    final explicit = radio['radio_title']?.toString().trim();
    if (explicit != null && explicit.isNotEmpty) return explicit;
    final artist = radio['artist_name']?.toString().trim();
    if (artist != null && artist.isNotEmpty) return '$artist Radio';
    final title = radio['title']?.toString().trim();
    if (title != null && title.isNotEmpty) return '$title Radio';
    return 'Popular Radio';
  }

  String _subtitle() {
    final subtitle = radio['subtitle']?.toString().trim();
    if (subtitle != null && subtitle.isNotEmpty) return subtitle;
    final related = _relatedArtists().take(4).join(', ');
    if (related.isNotEmpty) return 'With $related and more';
    return 'Artist radio shaped by your listening';
  }

  List<String> _collageImages() {
    final raw = radio['collage_images'];
    final Iterable<String> images = raw is List
        ? raw
            .map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
        : const <String>[];
    final thumbnail = radio['thumbnail']?.toString().trim();
    final trackImages = tracks
        .map((track) => track['artist_thumbnail']?.toString().trim() ?? '')
        .where((item) => item.isNotEmpty);
    return <String>{
      if (thumbnail != null && thumbnail.isNotEmpty) thumbnail,
      ...images,
      ...trackImages,
    }.take(6).toList(growable: false);
  }

  List<String> _relatedArtists() {
    final raw = radio['related_artists'];
    if (raw is List) {
      return raw
          .map((item) => item.toString().trim())
          .where((item) => item.isNotEmpty)
          .take(6)
          .toList(growable: false);
    }
    final artist = radio['artist_name']?.toString().trim();
    if (artist != null && artist.isNotEmpty) return <String>[artist];
    return const <String>[];
  }

  String _durationLabel(dynamic rawDuration) {
    final totalSeconds = int.tryParse('${rawDuration ?? ''}') ?? 0;
    if (totalSeconds <= 0) return '--:--';
    final minutes = totalSeconds ~/ 60;
    final seconds = totalSeconds % 60;
    return '$minutes:${seconds.toString().padLeft(2, '0')}';
  }

  Future<void> _playFrom(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> track, {
    bool shuffle = false,
  }) async {
    final navigator = Navigator.of(context);
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'radio:${radio['id'] ?? _radioTitle()}',
          playlistName: _radioTitle(),
          tracks: tracks,
          currentTrack: track,
          shuffle: shuffle,
        );
    if (!context.mounted) return;
    await navigator.push(
      MaterialPageRoute(builder: playerScreenBuilder),
    );
  }

  Future<void> _openTrackDetails(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> track,
  ) async {
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

  Future<void> _addTrackToPlaylist(
    BuildContext context,
    Map<String, dynamic> track,
  ) async {
    showAddToPlaylistDialog(
      context: context,
      track: Map<String, dynamic>.from(track),
    );
  }

  Future<void> _startTrackStation(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> track,
  ) async {
    final navigator = Navigator.of(context);
    await ref.read(playbackQueueProvider.notifier).startRadioSession(track);
    if (!context.mounted) return;
    await navigator.push(
      MaterialPageRoute(builder: playerScreenBuilder),
    );
  }

  void _showFeedback(BuildContext context, String message) {
    final messenger = ScaffoldMessenger.maybeOf(context);
    if (messenger == null) return;
    messenger
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(
          content: Text(message),
          behavior: SnackBarBehavior.floating,
        ),
      );
  }

  Future<void> _saveRadio(BuildContext context, WidgetRef ref) async {
    final title = _radioTitle();
    final playlists = ref.read(playlistProvider);
    final existing = playlists.where((playlist) => playlist.name == title);
    final playlist = existing.isEmpty
        ? ref.read(playlistProvider.notifier).createPlaylist(title)
        : existing.first;
    for (final track in tracks) {
      ref.read(playlistProvider.notifier).addTrackToPlaylist(playlist.id, track);
    }
    if (context.mounted) {
      _showFeedback(context, 'Saved $title');
    }
  }

  Future<void> _downloadRadio(BuildContext context, WidgetRef ref) async {
    unawaited(ref.read(downloadCenterProvider.notifier).downloadTracks(tracks));
    _showFeedback(context, 'Downloading ${tracks.length} radio tracks');
  }

  Future<void> _showRadioActions(BuildContext context, WidgetRef ref) async {
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: Colors.transparent,
      builder: (sheetContext) {
        Future<void> closeAndRun(Future<void> Function() action) async {
          Navigator.of(sheetContext).pop();
          await action();
        }

        return NeatieBottomSheetSurface(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const NeatieSheetHandle(),
              _RadioActionTile(
                icon: Icons.bookmark_add_rounded,
                label: 'Save radio as playlist',
                onTap: () => closeAndRun(() => _saveRadio(context, ref)),
              ),
              _RadioActionTile(
                icon: Icons.download_rounded,
                label: 'Download radio tracks',
                onTap: () => closeAndRun(() => _downloadRadio(context, ref)),
              ),
              _RadioActionTile(
                icon: Icons.shuffle_rounded,
                label: 'Shuffle play',
                onTap: tracks.isEmpty
                    ? null
                    : () => closeAndRun(
                          () => _playFrom(
                            context,
                            ref,
                            tracks.first,
                            shuffle: true,
                          ),
                        ),
              ),
              const SizedBox(height: 10),
            ],
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final accent = _parseAccent(radio['accent_color']?.toString());
    final title = _radioTitle();
    final subtitle = _subtitle();
    final related = _relatedArtists();
    final images = _collageImages();
    final primaryThumbnail = images.isNotEmpty ? images.first : null;
    final likeKey =
        'radio:${radio['id'] ?? radio['artist_name'] ?? radio['title']}';
    final isLiked = ref.watch(collectionLikesProvider).contains(likeKey);

    return Scaffold(
      backgroundColor: neatieInk,
      body: NeatieBackground(
        child: CustomScrollView(
          physics: const BouncingScrollPhysics(),
          slivers: [
            SliverAppBar(
              pinned: true,
              stretch: true,
              expandedHeight: 388,
              backgroundColor: Color.lerp(accent, Colors.black, 0.68),
              leading: IconButton(
                icon: const Icon(Icons.arrow_back_rounded),
                onPressed: () => Navigator.of(context).maybePop(),
              ),
              actions: [
                IconButton(
                  tooltip: 'Radio actions',
                  icon: const Icon(Icons.more_horiz_rounded),
                  onPressed: () => unawaited(_showRadioActions(context, ref)),
                ),
              ],
              flexibleSpace: FlexibleSpaceBar(
                background: _RadioHeroHeader(
                  accent: accent,
                  title: title,
                  subtitle: subtitle,
                  relatedArtists: related,
                  trackCount: tracks.length,
                  images: images,
                ),
              ),
            ),
            SliverToBoxAdapter(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(18, 16, 18, 8),
                child: Row(
                  children: [
                    _RoundRadioAction(
                      icon: Icons.play_arrow_rounded,
                      size: 64,
                      foreground: Colors.black,
                      background: neatieActive,
                      onPrime: tracks.isEmpty
                          ? null
                          : () {
                              final trackId = extractTrackId(tracks.first);
                              if (trackId != null && trackId.isNotEmpty) {
                                onPrimeTrack?.call(trackId);
                              }
                            },
                      onTap: tracks.isEmpty
                          ? null
                          : () => unawaited(_playFrom(context, ref, tracks.first)),
                    ),
                    const SizedBox(width: 14),
                    _SmallRadioAction(
                      icon: Icons.shuffle_rounded,
                      label: 'Shuffle',
                      onTap: tracks.isEmpty
                          ? null
                          : () => unawaited(
                                _playFrom(
                                  context,
                                  ref,
                                  tracks.first,
                                  shuffle: true,
                                ),
                              ),
                    ),
                    _SmallRadioAction(
                      icon: Icons.bookmark_add_outlined,
                      label: 'Save',
                      onTap: () => unawaited(_saveRadio(context, ref)),
                    ),
                    _SmallRadioAction(
                      icon: isLiked
                          ? Icons.favorite_rounded
                          : Icons.favorite_border_rounded,
                      label: isLiked ? 'Liked' : 'Like',
                      onTap: () => unawaited(
                        ref
                            .read(collectionLikesProvider.notifier)
                            .toggle(likeKey),
                      ),
                    ),
                    const Spacer(),
                    AppArtwork(
                      thumbnail: primaryThumbnail,
                      videoId: tracks.isNotEmpty ? extractTrackId(tracks.first) : null,
                      width: 48,
                      height: 48,
                      radius: 14,
                    ),
                  ],
                ),
              ),
            ),
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(18, 12, 18, 30),
              sliver: SliverList(
                delegate: SliverChildBuilderDelegate(
                  (context, visualIndex) {
                    if (visualIndex.isOdd) {
                      return Divider(
                        color: Colors.white.withValues(alpha: 0.06),
                        height: 18,
                      );
                    }
                    final index = visualIndex ~/ 2;
                    final track = tracks[index];
                    final videoId = extractTrackId(track);
                    return _RadioTrackTile(
                      index: index,
                      track: track,
                      duration: _durationLabel(track['duration']),
                      videoId: videoId,
                      onPrime: videoId == null || videoId.isEmpty
                          ? null
                          : () => onPrimeTrack?.call(videoId),
                      onTap: () => unawaited(_playFrom(context, ref, track)),
                      onOpenDetails: () => unawaited(
                        _openTrackDetails(context, ref, track),
                      ),
                      onAddToPlaylist: (track) =>
                          _addTrackToPlaylist(context, track),
                      onStartStation: (track) =>
                          _startTrackStation(context, ref, track),
                    );
                  },
                  childCount: tracks.isEmpty ? 0 : (tracks.length * 2) - 1,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RadioHeroHeader extends StatelessWidget {
  const _RadioHeroHeader({
    required this.accent,
    required this.title,
    required this.subtitle,
    required this.relatedArtists,
    required this.trackCount,
    required this.images,
  });

  final Color accent;
  final String title;
  final String subtitle;
  final List<String> relatedArtists;
  final int trackCount;
  final List<String> images;

  @override
  Widget build(BuildContext context) {
    final relatedText = relatedArtists.isEmpty
        ? 'Personal artist station'
        : 'With ${relatedArtists.take(4).join(', ')} and more';
    return Stack(
      fit: StackFit.expand,
      children: [
        DecoratedBox(
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topCenter,
              end: Alignment.bottomCenter,
              colors: [
                accent.withValues(alpha: 0.96),
                Color.lerp(accent, Colors.black, 0.38) ?? accent,
                neatieInk,
              ],
              stops: const [0, 0.66, 1],
            ),
          ),
        ),
        Positioned(
          right: -70,
          top: 28,
          child: Icon(
            Icons.radio_rounded,
            color: Colors.black.withValues(alpha: 0.10),
            size: 280,
          ),
        ),
        Positioned(
          left: 22,
          right: 22,
          bottom: 30,
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              _RadioCollageArtwork(
                images: images,
                accent: accent,
                size: 164,
              ),
              const SizedBox(width: 18),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      'PUBLIC RADIO',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.66),
                        fontSize: 11,
                        fontWeight: FontWeight.w900,
                        letterSpacing: 1.3,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      title,
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 34,
                        fontWeight: FontWeight.w900,
                        letterSpacing: -1.25,
                        height: 0.94,
                      ),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      relatedText,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.78),
                        fontSize: 13,
                        height: 1.25,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      '$trackCount tracks - $subtitle',
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.58),
                        fontSize: 11,
                        height: 1.25,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _RadioCollageArtwork extends StatelessWidget {
  const _RadioCollageArtwork({
    required this.images,
    required this.accent,
    required this.size,
  });

  final List<String> images;
  final Color accent;
  final double size;

  Widget _circle(String? thumbnail, double diameter, {bool main = false}) {
    return Container(
      width: diameter,
      height: diameter,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(
          color: main
              ? Colors.black.withValues(alpha: 0.18)
              : Colors.white.withValues(alpha: 0.28),
          width: main ? 5 : 2,
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: main ? 0.35 : 0.20),
            blurRadius: main ? 22 : 12,
            offset: const Offset(0, 10),
          ),
        ],
      ),
      child: ClipOval(
        child: thumbnail == null || thumbnail.isEmpty
            ? ColoredBox(
                color: Color.lerp(accent, Colors.black, 0.22) ?? accent,
                child: Icon(
                  Icons.graphic_eq_rounded,
                  color: Colors.white.withValues(alpha: 0.86),
                  size: diameter * 0.42,
                ),
              )
            : AppArtwork(
                thumbnail: thumbnail,
                width: diameter,
                height: diameter,
                radius: diameter / 2,
              ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final artworkLayers = images.length <= 1
        ? <Widget>[
            Positioned(
              left: (size - 104) / 2,
              top: 32,
              child: _circle(images.isEmpty ? null : images[0], 104, main: true),
            ),
          ]
        : images.length == 2
            ? <Widget>[
                Positioned(left: 10, top: 48, child: _circle(images[0], 76)),
                Positioned(right: 10, top: 48, child: _circle(images[1], 76)),
              ]
            : <Widget>[
                Positioned(left: 12, top: 58, child: _circle(images[1], 54)),
                Positioned(right: 10, top: 54, child: _circle(images[2], 52)),
                Positioned(
                  left: 46,
                  top: 35,
                  child: _circle(images[0], 88, main: true),
                ),
              ];
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(10),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            accent.withValues(alpha: 0.92),
            Color.lerp(accent, Colors.black, 0.24) ?? accent,
          ],
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.10)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.30),
            blurRadius: 26,
            offset: const Offset(0, 14),
          ),
        ],
      ),
      child: Stack(
        children: [
          Positioned(
            top: 10,
            right: 12,
            child: Text(
              'RADIO',
              style: TextStyle(
                color: Colors.black.withValues(alpha: 0.82),
                fontSize: 11,
                fontWeight: FontWeight.w900,
                letterSpacing: 1.9,
              ),
            ),
          ),
          ...artworkLayers,
          Positioned(
            left: 12,
            bottom: 10,
            child: Icon(
              Icons.radio_rounded,
              color: Colors.black.withValues(alpha: 0.72),
              size: 18,
            ),
          ),
        ],
      ),
    );
  }
}

class _RoundRadioAction extends StatelessWidget {
  const _RoundRadioAction({
    required this.icon,
    required this.size,
    required this.foreground,
    required this.background,
    required this.onTap,
    this.onPrime,
  });

  final IconData icon;
  final double size;
  final Color foreground;
  final Color background;
  final VoidCallback? onTap;
  final VoidCallback? onPrime;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      onTapDown: onPrime == null ? null : (_) => onPrime!(),
      borderRadius: BorderRadius.circular(size),
      child: Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          color: onTap == null
              ? background.withValues(alpha: 0.36)
              : background,
          shape: BoxShape.circle,
          boxShadow: [
            BoxShadow(
              color: background.withValues(alpha: 0.18),
              blurRadius: 18,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Icon(icon, color: foreground, size: size * 0.55),
      ),
    );
  }
}

class _SmallRadioAction extends StatelessWidget {
  const _SmallRadioAction({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 6),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(18),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                icon,
                color: Colors.white.withValues(alpha: onTap == null ? 0.32 : 0.78),
                size: 23,
              ),
              const SizedBox(height: 4),
              Text(
                label,
                style: TextStyle(
                  color: Colors.white.withValues(alpha: onTap == null ? 0.28 : 0.52),
                  fontSize: 9.5,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RadioTrackTile extends StatelessWidget {
  const _RadioTrackTile({
    required this.index,
    required this.track,
    required this.duration,
    required this.videoId,
    required this.onTap,
    this.onPrime,
    required this.onOpenDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final int index;
  final Map<String, dynamic> track;
  final String duration;
  final String? videoId;
  final VoidCallback onTap;
  final VoidCallback? onPrime;
  final VoidCallback onOpenDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  String get _title => track['title']?.toString() ?? 'Unknown Track';

  String get _artist =>
      (track['channel'] ?? track['author'] ?? track['artist'])?.toString() ??
      'Unknown Artist';

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        onTapDown: onPrime == null ? null : (_) => onPrime!(),
        borderRadius: BorderRadius.circular(18),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 5),
          child: Row(
            children: [
              SizedBox(
                width: 25,
                child: Text(
                  '${index + 1}',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.46),
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              AppArtwork(
                thumbnail: track['thumbnail'],
                videoId: videoId,
                width: 56,
                height: 56,
                radius: 12,
              ),
              const SizedBox(width: 13),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 15,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      _artist,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.56),
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Text(
                duration,
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.42),
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                ),
              ),
              TrackMenuButton(
                track: Map<String, dynamic>.from(track),
                onOpenDetails: onOpenDetails,
                onAddToPlaylist: onAddToPlaylist,
                onStartStation: onStartStation,
                buttonSize: 38,
                iconSize: 18,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RadioActionTile extends StatelessWidget {
  const _RadioActionTile({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      enabled: onTap != null,
      leading: Icon(
        icon,
        color: Colors.white.withValues(alpha: onTap == null ? 0.28 : 0.82),
      ),
      title: Text(
        label,
        style: TextStyle(
          color: Colors.white.withValues(alpha: onTap == null ? 0.32 : 0.90),
          fontWeight: FontWeight.w800,
        ),
      ),
      onTap: onTap,
    );
  }
}
