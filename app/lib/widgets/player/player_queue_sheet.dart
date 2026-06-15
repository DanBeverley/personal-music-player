import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider_queue.dart';
import '../../logic/playlist_provider.dart';
import '../../logic/track_metadata.dart';
import '../playlist/add_to_playlist_dialog.dart';
import 'player_queue_widgets.dart';

class PlayerQueueSheet extends ConsumerWidget {
  final PlaybackQueueState queueState;
  final PlaybackQueueNotifier queueNotifier;
  final ScrollController scrollController;
  final VoidCallback onDismiss;
  final TextEditingController searchController;
  final bool isQueueSearchLoading;
  final List<Map<String, dynamic>> queueSearchResults;
  final VoidCallback onClearSearch;
  final Set<String> removingQueueTrackIds;
  final Future<void> Function(String trackId, VoidCallback onRemove)
      onAnimateQueueRemoval;
  final double radiusLarge;
  final double radiusMedium;
  final Color accentColor;

  const PlayerQueueSheet({
    super.key,
    required this.queueState,
    required this.queueNotifier,
    required this.scrollController,
    required this.onDismiss,
    required this.searchController,
    required this.isQueueSearchLoading,
    required this.queueSearchResults,
    required this.onClearSearch,
    required this.removingQueueTrackIds,
    required this.onAnimateQueueRemoval,
    required this.radiusLarge,
    required this.radiusMedium,
    required this.accentColor,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final queueTitle = queueState.mode == PlaybackQueueMode.playlist
        ? (queueState.playlistName?.isNotEmpty == true
            ? queueState.playlistName!
            : 'Queue')
        : 'Queue';
    final resolvedCurrentIndex = playerResolvedQueueIndex(queueState);
    final currentTrack = playerCurrentQueueTrack(queueState);
    final earlierQueue = resolvedCurrentIndex > 0
        ? queueState.queue
            .take(resolvedCurrentIndex)
            .where((track) {
              final trackId = extractTrackId(track);
              if (trackId == null || trackId == queueState.currentTrackId) {
                return false;
              }
              return !queueState.playedTrackIds.contains(trackId);
            })
            .toList(growable: false)
        : const <Map<String, dynamic>>[];
    final playedQueue = queueState.queue
        .where((track) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId == queueState.currentTrackId) {
            return false;
          }
          return queueState.playedTrackIds.contains(trackId);
        })
        .toList(growable: false);
    final upcomingQueue = resolvedCurrentIndex >= 0 &&
            resolvedCurrentIndex + 1 < queueState.queue.length
        ? queueState.queue.sublist(resolvedCurrentIndex + 1)
        : const <Map<String, dynamic>>[];

    final queueChildren = <Widget>[
      _QueueSearchCard(
        queueState: queueState,
        queueNotifier: queueNotifier,
        searchController: searchController,
        isQueueSearchLoading: isQueueSearchLoading,
        queueSearchResults: queueSearchResults,
        onClearSearch: onClearSearch,
        radiusLarge: radiusLarge,
        radiusMedium: radiusMedium,
        accentColor: accentColor,
      ),
    ];

    void addQueueSection(String label, List<Map<String, dynamic>> tracks,
        {bool played = false}) {
      if (tracks.isEmpty) return;
      queueChildren.add(_QueueSectionLabel(label: label));
      for (final track in tracks) {
        final videoId = extractTrackId(track);
        final queueIndex = videoId == null
            ? -1
            : queueState.queue.indexWhere(
                (entry) => extractTrackId(entry) == videoId,
              );
        queueChildren.add(
          PlayerQueueTile(
            track: track,
            isActive: false,
            isDimmed: played,
            onTap: queueIndex >= 0
                ? () => unawaited(queueNotifier.playQueueIndex(queueIndex))
                : () {},
            radiusLarge: radiusLarge,
            radiusMedium: radiusMedium,
          ),
        );
      }
    }

    addQueueSection('Queued earlier', earlierQueue);
    addQueueSection('Played', playedQueue, played: true);

    if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: PlayerQueueTile(
            track: currentTrack,
            isActive: true,
            onTap: () => unawaited(
              queueNotifier.playQueueIndex(resolvedCurrentIndex),
            ),
            trailingLabel: 'Playing',
            radiusLarge: radiusLarge,
            radiusMedium: radiusMedium,
          ),
        ),
      );
    }

    if (upcomingQueue.isNotEmpty) {
      queueChildren.add(
        const _QueueSectionLabel(label: 'Up next', emphasis: true),
      );
      queueChildren.add(
        ReorderableListView.builder(
          shrinkWrap: true,
          buildDefaultDragHandles: false,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: upcomingQueue.length,
          onReorderItem: (oldIndex, newIndex) {
            final legacyNewIndex =
                newIndex >= oldIndex ? newIndex + 1 : newIndex;
            queueNotifier.reorderUpcomingQueue(oldIndex, legacyNewIndex);
          },
          proxyDecorator: (child, index, animation) {
            return AnimatedBuilder(
              animation: animation,
              builder: (context, _) {
                final elevation =
                    Tween<double>(begin: 0, end: 16).transform(animation.value);
                return Material(
                  color: Colors.transparent,
                  elevation: elevation,
                  child: child,
                );
              },
            );
          },
          itemBuilder: (context, index) {
            final track = upcomingQueue[index];
            final videoId = extractTrackId(track);
            final isHidden = isTrackHidden(track);
            final isRemoving =
                videoId != null && removingQueueTrackIds.contains(videoId);
            final actualQueueIndex = resolvedCurrentIndex + 1 + index;
            return ReorderableDelayedDragStartListener(
              key: ValueKey(
                'queue-upcoming-${videoId ?? '$actualQueueIndex-${track['title']}'}',
              ),
              index: index,
              child: PlayerQueueTile(
                track: track,
                isActive: false,
                isDimmed: isHidden,
                isRemoving: isRemoving,
                onTap: () =>
                    unawaited(queueNotifier.playQueueIndex(actualQueueIndex)),
                trailing: videoId == null
                    ? null
                    : Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          IconButton(
                            onPressed: () {
                              if (queueState.mode ==
                                      PlaybackQueueMode.playlist &&
                                  queueState.playlistId != null) {
                                ref.read(playlistProvider.notifier).setTrackHidden(
                                      queueState.playlistId!,
                                      videoId,
                                      !isHidden,
                                    );
                              }
                              queueNotifier.setQueueTrackHidden(
                                videoId,
                                !isHidden,
                              );
                            },
                            icon: AnimatedRotation(
                              duration: const Duration(milliseconds: 220),
                              turns: isHidden ? 0.08 : 0,
                              child: Icon(
                                isHidden
                                    ? Icons.visibility_off_rounded
                                    : Icons.visibility_rounded,
                                color: Colors.white.withValues(
                                  alpha: isHidden ? 0.38 : 0.7,
                                ),
                              ),
                            ),
                          ),
                          if (queueState.mode == PlaybackQueueMode.radio)
                            IconButton(
                              onPressed: () => showAddToPlaylistDialog(
                                context: context,
                                track: track,
                              ),
                              icon: Icon(
                                Icons.playlist_add_rounded,
                                color: Colors.white.withValues(alpha: 0.72),
                              ),
                            ),
                          IconButton(
                            onPressed: () {
                              unawaited(
                                onAnimateQueueRemoval(videoId, () {
                                  if (queueState.mode ==
                                          PlaybackQueueMode.playlist &&
                                      queueState.playlistId != null) {
                                    ref
                                        .read(playlistProvider.notifier)
                                        .removeTrackFromPlaylist(
                                          queueState.playlistId!,
                                          videoId,
                                        );
                                    queueNotifier.removePlaylistTrack(videoId);
                                    return;
                                  }
                                  queueNotifier.removeFromQueue(videoId);
                                }),
                              );
                            },
                            icon: Icon(
                              queueState.mode == PlaybackQueueMode.playlist
                                  ? Icons.delete_outline_rounded
                                  : Icons.remove_circle_outline_rounded,
                              color: Colors.white.withValues(alpha: 0.58),
                            ),
                          ),
                        ],
                      ),
                radiusLarge: radiusLarge,
                radiusMedium: radiusMedium,
              ),
            );
          },
        ),
      );
    } else if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 4, 6, 18),
          child: Text(
            queueState.mode == PlaybackQueueMode.radio
                ? 'This queue will refill after the current song finishes.'
                : 'No upcoming songs are queued after the current track yet.',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.46),
              fontSize: 12.5,
              height: 1.5,
            ),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.radio && queueState.isLoadingQueue) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 18),
          child: Center(
            child: CircularProgressIndicator(color: accentColor),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.playlist &&
        (queueState.recommendations.isNotEmpty ||
            queueState.isLoadingRecommendations)) {
      queueChildren.add(
        const Padding(
          padding: EdgeInsets.fromLTRB(6, 18, 6, 12),
          child: Text(
            'Based on this song',
            style: TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
        ),
      );
      if (queueState.recommendations.isEmpty &&
          queueState.isLoadingRecommendations) {
        queueChildren.add(
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: accentColor),
            ),
          ),
        );
      } else {
        for (final track in queueState.recommendations) {
          queueChildren.add(
            PlayerQueueTile(
              track: track,
              isActive: false,
              onTap: () => unawaited(queueNotifier.startRadioSession(track)),
              onAddToPlaylist: () => showAddToPlaylistDialog(
                context: context,
                track: track,
              ),
              radiusLarge: radiusLarge,
              radiusMedium: radiusMedium,
            ),
          );
        }
      }
      if (queueState.isLoadingRecommendations &&
          queueState.recommendations.isNotEmpty) {
        queueChildren.add(
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: accentColor),
            ),
          ),
        );
      }
    }
    if (queueState.mode == PlaybackQueueMode.playlist &&
        !queueState.hasMoreRecommendations &&
        !queueState.isLoadingRecommendations) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 18, 6, 8),
          child: Text(
            'No more related songs are available for this session.',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.46),
              fontSize: 12.5,
            ),
          ),
        ),
      );
    }

    return Container(
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.92),
        borderRadius: BorderRadius.vertical(top: Radius.circular(radiusLarge)),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.38),
            blurRadius: 28,
            offset: const Offset(0, -8),
          ),
        ],
      ),
      child: Column(
        children: [
          _QueueSheetHeader(
            queueTitle: queueTitle,
            onDismiss: onDismiss,
            radiusLarge: radiusLarge,
          ),
          Expanded(
            child: queueState.mode == PlaybackQueueMode.none
                ? const Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 28),
                      child: Text(
                        'Start a song from search, recommendations, or a playlist and the live queue will appear here.',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white54,
                          fontSize: 15,
                          height: 1.45,
                        ),
                      ),
                    ),
                  )
                : NotificationListener<ScrollNotification>(
                    onNotification: (notification) {
                      if (notification.metrics.pixels >=
                          notification.metrics.maxScrollExtent - 180) {
                        unawaited(queueNotifier.loadMore());
                      }
                      return false;
                    },
                    child: ListView(
                      controller: scrollController,
                      physics: const BouncingScrollPhysics(
                        parent: AlwaysScrollableScrollPhysics(),
                      ),
                      padding: EdgeInsets.fromLTRB(
                        16,
                        8,
                        16,
                        math.max(
                          140,
                          MediaQuery.of(context).padding.bottom + 120,
                        ).toDouble(),
                      ),
                      children: queueChildren,
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}

class _QueueSheetHeader extends StatelessWidget {
  final String queueTitle;
  final VoidCallback onDismiss;
  final double radiusLarge;

  const _QueueSheetHeader({
    required this.queueTitle,
    required this.onDismiss,
    required this.radiusLarge,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onDismiss,
      child: Container(
        padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.96),
          borderRadius: BorderRadius.vertical(top: Radius.circular(radiusLarge)),
          border: Border(
            bottom: BorderSide(color: Colors.white.withValues(alpha: 0.06)),
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 42,
              height: 4,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.16),
                borderRadius: BorderRadius.circular(999),
              ),
            ),
            const SizedBox(height: 14),
            Row(
              children: [
                Text(
                  queueTitle,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _QueueSearchCard extends StatelessWidget {
  final PlaybackQueueState queueState;
  final PlaybackQueueNotifier queueNotifier;
  final TextEditingController searchController;
  final bool isQueueSearchLoading;
  final List<Map<String, dynamic>> queueSearchResults;
  final VoidCallback onClearSearch;
  final double radiusLarge;
  final double radiusMedium;
  final Color accentColor;

  const _QueueSearchCard({
    required this.queueState,
    required this.queueNotifier,
    required this.searchController,
    required this.isQueueSearchLoading,
    required this.queueSearchResults,
    required this.onClearSearch,
    required this.radiusLarge,
    required this.radiusMedium,
    required this.accentColor,
  });

  @override
  Widget build(BuildContext context) {
    final searchQuery = searchController.text.trim();
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 12),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(radiusLarge),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          TextField(
            controller: searchController,
            style: const TextStyle(color: Colors.white),
            decoration: InputDecoration(
              hintText: '',
              hintStyle: TextStyle(color: Colors.white.withValues(alpha: 0.34)),
              filled: true,
              fillColor: Colors.white.withValues(alpha: 0.04),
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 16,
                vertical: 12,
              ),
              prefixIcon: Icon(
                Icons.search_rounded,
                color: Colors.white.withValues(alpha: 0.48),
              ),
              suffixIcon: searchController.text.trim().isEmpty
                  ? (isQueueSearchLoading
                      ? Padding(
                          padding: const EdgeInsets.all(14),
                          child: SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                              color: accentColor,
                            ),
                          ),
                        )
                      : null)
                  : IconButton(
                      onPressed: onClearSearch,
                      icon: Icon(
                        Icons.close_rounded,
                        color: Colors.white.withValues(alpha: 0.5),
                      ),
                    ),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(radiusMedium),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          if (searchQuery.isNotEmpty ||
              isQueueSearchLoading ||
              queueSearchResults.isNotEmpty) ...[
            const SizedBox(height: 12),
            if (queueSearchResults.isEmpty && isQueueSearchLoading)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Center(
                  child: CircularProgressIndicator(color: accentColor),
                ),
              )
            else if (queueSearchResults.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(
                  'No songs found for "$searchQuery".',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.46),
                    fontSize: 13,
                  ),
                ),
              )
            else
              ...queueSearchResults.take(6).map((track) {
                final videoId = extractTrackId(track);
                final queueIndex = videoId == null
                    ? -1
                    : queueState.queue.indexWhere(
                        (entry) => extractTrackId(entry) == videoId,
                      );
                final isQueued = playerIsTrackInQueue(queueState, videoId);
                final isCurrent =
                    videoId != null && videoId == queueState.currentTrackId;
                return PlayerQueueTile(
                  track: track,
                  isActive: isCurrent,
                  onTap: () {
                    if (isQueued) {
                      unawaited(queueNotifier.playQueueIndex(queueIndex));
                      return;
                    }
                    unawaited(queueNotifier.enqueueTrack(track));
                  },
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (isQueued)
                        Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 10,
                            vertical: 6,
                          ),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(999),
                          ),
                          child: Text(
                            isCurrent ? 'Playing' : 'In Queue',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.8),
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        )
                      else
                        IconButton(
                          onPressed: () =>
                              unawaited(queueNotifier.enqueueTrack(track)),
                          icon: Icon(
                            Icons.add_circle_outline_rounded,
                            color: Colors.white.withValues(alpha: 0.74),
                          ),
                        ),
                    ],
                  ),
                  radiusLarge: radiusLarge,
                  radiusMedium: radiusMedium,
                );
              }),
          ],
        ],
      ),
    );
  }
}

class _QueueSectionLabel extends StatelessWidget {
  final String label;
  final bool emphasis;

  const _QueueSectionLabel({required this.label, this.emphasis = false});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
      child: Text(
        label,
        style: TextStyle(
          color: Colors.white.withValues(alpha: emphasis ? 0.72 : 0.54),
          fontSize: emphasis ? 13 : 12,
          fontWeight: FontWeight.w700,
          letterSpacing: emphasis ? 0.45 : 0.4,
        ),
      ),
    );
  }
}
