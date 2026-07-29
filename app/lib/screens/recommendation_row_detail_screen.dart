import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/audio_provider_recommendation.dart';
import '../logic/details_provider.dart';
import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';
import '../widgets/app_artwork.dart';
import '../widgets/home/track_menu_button.dart';
import '../widgets/playlist/add_to_playlist_dialog.dart';

class RecommendationRowDetailScreen extends ConsumerStatefulWidget {
  final RecommendationFeedRowState initialRow;
  final WidgetBuilder playerScreenBuilder;
  final Widget Function(Map<String, dynamic> track) trackDetailsScreenBuilder;
  final void Function(String videoId)? onPrimeTrack;
  final ValueChanged<Map<String, dynamic>>? onOpenAlbum;
  final ValueChanged<Map<String, dynamic>>? onOpenArtist;
  final ValueChanged<Map<String, dynamic>>? onOpenMix;
  final ValueChanged<Map<String, dynamic>>? onOpenRadio;

  const RecommendationRowDetailScreen({
    super.key,
    required this.initialRow,
    required this.playerScreenBuilder,
    required this.trackDetailsScreenBuilder,
    this.onPrimeTrack,
    this.onOpenAlbum,
    this.onOpenArtist,
    this.onOpenMix,
    this.onOpenRadio,
  });

  @override
  ConsumerState<RecommendationRowDetailScreen> createState() =>
      _RecommendationRowDetailScreenState();
}

class _RecommendationRowDetailScreenState
    extends ConsumerState<RecommendationRowDetailScreen> {
  final ScrollController _scrollController = ScrollController();
  bool _loggedInitialPagingState = false;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_handleScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _handleScroll();
    });
  }

  @override
  void dispose() {
    _scrollController
      ..removeListener(_handleScroll)
      ..dispose();
    super.dispose();
  }

  RecommendationFeedRowState _resolvedRow(RecommendationFeedState state) {
    for (final row in state.rows) {
      if (row.id == widget.initialRow.id) {
        return row;
      }
    }
    return widget.initialRow;
  }

  void _handleScroll() {
    if (!_scrollController.hasClients) return;
    final row = _resolvedRow(ref.read(recommendationProvider));
    if (!row.hasMore ||
        ref.read(recommendationProvider.notifier).isRowPaginating(row.id) ||
        _scrollController.position.extentAfter > 700) {
      return;
    }
    unawaited(ref.read(recommendationProvider.notifier).loadMoreRow(row.id));
  }

  Future<void> _openPlayer() async {
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(builder: widget.playerScreenBuilder),
    );
  }

  Future<void> _playTrack(
    RecommendationFeedRowState row,
    Map<String, dynamic> track,
  ) async {
    if (row.items.isEmpty) return;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'row_detail:${row.id}',
          playlistName: row.title,
          tracks: row.items,
          currentTrack: track,
        );
    await _openPlayer();
  }

  void _openItem(
    RecommendationFeedRowState row,
    Map<String, dynamic> item,
  ) {
    if (row.itemType == 'album') {
      widget.onOpenAlbum?.call(item);
      return;
    }
    if (row.itemType == 'artist') {
      widget.onOpenArtist?.call(item);
      return;
    }
    if (row.itemType == 'mix') {
      widget.onOpenMix?.call(item);
      return;
    }
    if (row.itemType == 'radio') {
      widget.onOpenRadio?.call(item);
      return;
    }
    unawaited(_playTrack(row, item));
  }

  Future<void> _openTrackDetails(Map<String, dynamic> track) async {
    final videoId = extractTrackId(track);
    if (videoId == null || videoId.isEmpty) return;
    widget.onPrimeTrack?.call(videoId);
    ref.read(trackDetailsProvider.notifier).fetchDetails(videoId);
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => widget.trackDetailsScreenBuilder(track),
      ),
    );
  }

  Future<void> _addTrackToPlaylist(Map<String, dynamic> track) async {
    showAddToPlaylistDialog(
      context: context,
      track: Map<String, dynamic>.from(track),
    );
  }

  Future<void> _startTrackStation(Map<String, dynamic> track) async {
    await ref.read(playbackQueueProvider.notifier).startRadioSession(track);
    await _openPlayer();
  }

  String _durationLabel(dynamic rawDuration) {
    final totalSeconds = int.tryParse('${rawDuration ?? ''}') ?? 0;
    if (totalSeconds <= 0) return '--:--';
    final minutes = totalSeconds ~/ 60;
    final seconds = totalSeconds % 60;
    return '$minutes:${seconds.toString().padLeft(2, '0')}';
  }

  Color _rowAccentColor(String seed) {
    final palette = <Color>[
      const Color(0xFFC5372B),
      const Color(0xFFB84722),
      const Color(0xFF6E3CC9),
      const Color(0xFF245E8C),
      const Color(0xFF1E7A66),
    ];
    final hash = seed.codeUnits.fold<int>(0, (sum, code) => sum + code);
    return palette[hash % palette.length];
  }

  @override
  Widget build(BuildContext context) {
    final recState = ref.watch(recommendationProvider);
    final row = _resolvedRow(recState);
    final isPaginating =
        ref.watch(recommendationProvider.notifier).isRowPaginating(row.id);
    if (!_loggedInitialPagingState) {
      _loggedInitialPagingState = true;
      debugProxyLog(
        'recommend',
        'row detail open row=${row.id} items=${row.items.length} hasMore=${row.hasMore} preparedCount=${row.meta['prepared_count']} reserveCount=${row.meta['reserve_count']} nextOffset=${row.nextOffset}',
      );
    }
    if (row.hasMore && !isPaginating) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _handleScroll();
      });
    }
    final heroTrack =
        row.items.isNotEmpty ? row.items.first : const <String, dynamic>{};
    final accent = _rowAccentColor(row.id);

    return Scaffold(
      backgroundColor: neatieInk,
      appBar: AppBar(
        title: Text(row.title),
      ),
      body: NeatieBackground(
        child: CustomScrollView(
          controller: _scrollController,
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
                      accent.withValues(alpha: 0.95),
                      accent.withValues(alpha: 0.72),
                      neatieRaised,
                    ],
                  ),
                  borderRadius: BorderRadius.circular(28),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.08),
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: accent.withValues(alpha: 0.22),
                      blurRadius: 24,
                      offset: const Offset(0, 12),
                    ),
                  ],
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Made for your listening right now',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.78),
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.35,
                      ),
                    ),
                    const SizedBox(height: 16),
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        Container(
                          padding: const EdgeInsets.all(6),
                          decoration: BoxDecoration(
                            color: Colors.black.withValues(alpha: 0.14),
                            borderRadius: BorderRadius.circular(22),
                            border: Border.all(
                              color: Colors.white.withValues(alpha: 0.12),
                            ),
                          ),
                          child: AppArtwork(
                            thumbnail: heroTrack['thumbnail'],
                            videoId: extractPlaybackSourceId(heroTrack),
                            width: 112,
                            height: 112,
                            radius: 18,
                          ),
                        ),
                        const SizedBox(width: 18),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                row.title,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 31,
                                  fontWeight: FontWeight.w900,
                                  height: 1.04,
                                ),
                              ),
                              const SizedBox(height: 10),
                              Text(
                                row.hasMore
                                    ? '${row.items.length}+ ${row.itemType}s in this lane'
                                    : '${row.items.length} ${row.itemType}s in this lane',
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.78),
                                  fontSize: 13,
                                  fontWeight: FontWeight.w600,
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
                          onTap: row.items.isEmpty || row.itemType != 'track'
                              ? null
                              : () => _playTrack(row, row.items.first),
                          onTapDown: row.items.isEmpty ||
                                  row.itemType != 'track' ||
                                  extractTrackId(row.items.first) == null
                              ? null
                              : (_) => widget.onPrimeTrack?.call(
                                    extractTrackId(row.items.first)!,
                                  ),
                          borderRadius: BorderRadius.circular(999),
                          child: Container(
                            width: 54,
                            height: 54,
                            decoration: const BoxDecoration(
                              color: Colors.white,
                              shape: BoxShape.circle,
                            ),
                            child: Icon(
                              Icons.play_arrow_rounded,
                              color: accent,
                              size: 32,
                            ),
                          ),
                        ),
                        const SizedBox(width: 14),
                        Expanded(
                          child: Text(
                            'A deeper pass through ${row.title.toLowerCase()}.',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.8),
                              fontSize: 13,
                              height: 1.35,
                            ),
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
                    if (index >= row.items.length) {
                      return Padding(
                        padding: const EdgeInsets.symmetric(vertical: 18),
                        child: Center(
                          child: isPaginating
                              ? const SizedBox(
                                  width: 26,
                                  height: 26,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2.2,
                                    color: Colors.white70,
                                  ),
                                )
                              : Text(
                                  'Keep scrolling for more',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.58),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                        ),
                      );
                    }

                    final track = row.items[index];
                    final videoId = extractTrackId(track);
                    final itemTitle =
                        (track['title'] ?? track['name'])?.toString() ??
                            'Unknown ${row.itemType}';
                    final itemSubtitle = row.itemType == 'track'
                        ? formatTrackSubtitle(track)
                        : (track['channel'] ??
                                    track['author'] ??
                                    track['artist'] ??
                                    track['subtitle'])
                                ?.toString() ??
                            (row.itemType == 'artist' ? 'Artist' : '');
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(22),
                      ),
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(22),
                          onTap: () => _openItem(row, track),
                          onTapDown: row.itemType != 'track' ||
                                  videoId == null ||
                                  videoId.isEmpty
                              ? null
                              : (_) => widget.onPrimeTrack?.call(videoId),
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: Row(
                              children: [
                                SizedBox(
                                  width: 26,
                                  child: Text(
                                    '${index + 1}',
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.56),
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
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                      Text(
                                        itemTitle,
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
                                        itemSubtitle,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: TextStyle(
                                          color: Colors.white
                                              .withValues(alpha: 0.68),
                                          fontSize: 12,
                                        ),
                                      ),
                                      if ((track['album']
                                              ?.toString()
                                              .trim()
                                              .isNotEmpty ??
                                          false)) ...[
                                        const SizedBox(height: 4),
                                        Text(
                                          track['album']!.toString(),
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                          style: TextStyle(
                                            color: Colors.white
                                                .withValues(alpha: 0.44),
                                            fontSize: 11,
                                          ),
                                        ),
                                      ],
                                    ],
                                  ),
                                ),
                                const SizedBox(width: 8),
                                if (row.itemType == 'track') ...[
                                  Text(
                                    _durationLabel(track['duration']),
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.52),
                                      fontSize: 11,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                  const SizedBox(width: 4),
                                  TrackMenuButton(
                                    track: Map<String, dynamic>.from(track),
                                    onOpenDetails: () =>
                                        _openTrackDetails(track),
                                    onAddToPlaylist: _addTrackToPlaylist,
                                    onStartStation: _startTrackStation,
                                    buttonSize: 38,
                                    iconSize: 18,
                                  ),
                                ] else
                                  const Icon(
                                    Icons.chevron_right_rounded,
                                    color: neatieMutedText,
                                  ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    );
                  },
                  childCount:
                      row.items.length + ((row.hasMore || isPaginating) ? 1 : 0),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
