import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart' show audioPlayerProvider;
import 'audio_provider_history.dart';
import 'audio_provider_request_builder.dart';
import 'audio_service_bridge.dart';
import 'history_manager.dart';
import 'interaction_events.dart';
import 'playlist_provider.dart';
import 'proxy_runtime.dart';
import 'recommendation_api_client.dart';
import 'track_metadata.dart';

enum PlaybackQueueMode { none, radio, playlist }

class PlaybackQueueState {
  final PlaybackQueueMode mode;
  final List<Map<String, dynamic>> queue;
  final int currentIndex;
  final String? currentTrackId;
  final List<String> playedTrackIds;
  final String? playlistId;
  final String? playlistName;
  final List<Map<String, dynamic>> recommendations;
  final bool isLoadingQueue;
  final bool isLoadingRecommendations;
  final bool hasMoreRecommendations;

  const PlaybackQueueState({
    this.mode = PlaybackQueueMode.none,
    this.queue = const [],
    this.currentIndex = 0,
    this.currentTrackId,
    this.playedTrackIds = const [],
    this.playlistId,
    this.playlistName,
    this.recommendations = const [],
    this.isLoadingQueue = false,
    this.isLoadingRecommendations = false,
    this.hasMoreRecommendations = true,
  });

  PlaybackQueueState copyWith({
    PlaybackQueueMode? mode,
    List<Map<String, dynamic>>? queue,
    int? currentIndex,
    String? currentTrackId,
    List<String>? playedTrackIds,
    String? playlistId,
    String? playlistName,
    List<Map<String, dynamic>>? recommendations,
    bool? isLoadingQueue,
    bool? isLoadingRecommendations,
    bool? hasMoreRecommendations,
    bool clearPlaylist = false,
    bool clearRecommendations = false,
  }) {
    return PlaybackQueueState(
      mode: mode ?? this.mode,
      queue: queue ?? this.queue,
      currentIndex: currentIndex ?? this.currentIndex,
      currentTrackId: currentTrackId ?? this.currentTrackId,
      playedTrackIds: playedTrackIds ?? this.playedTrackIds,
      playlistId: clearPlaylist ? null : playlistId ?? this.playlistId,
      playlistName: clearPlaylist ? null : playlistName ?? this.playlistName,
      recommendations: clearRecommendations
          ? const []
          : recommendations ?? this.recommendations,
      isLoadingQueue: isLoadingQueue ?? this.isLoadingQueue,
      isLoadingRecommendations:
          isLoadingRecommendations ?? this.isLoadingRecommendations,
      hasMoreRecommendations:
          hasMoreRecommendations ?? this.hasMoreRecommendations,
    );
  }
}

class PlaybackQueueNotifier extends StateNotifier<PlaybackQueueState> {
  final Ref ref;
  final Random _random = Random();
  static const int _playedTrackMinSeconds = 12;
  static const double _playedTrackMinRatio = 0.35;

  PlaybackQueueNotifier(this.ref) : super(const PlaybackQueueState());

  void _primeTracks(
    Iterable<dynamic> tracks, {
    int limit = 8,
  }) {
    final ids = <String>[];
    for (final track in tracks) {
      if (isTrackHidden(track)) continue;
      final id = extractTrackId(track);
      if (id == null || id.isEmpty) continue;
      ids.add(id);
      if (ids.length >= limit) break;
    }
    if (ids.isEmpty) return;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(ids));
  }

  void _primeUpcomingQueue({int lookahead = 10}) {
    final upcoming = <Map<String, dynamic>>[];
    final resolvedCurrentIndex = _resolvedCurrentIndex();
    for (var i = resolvedCurrentIndex + 1; i < state.queue.length; i++) {
      final track = state.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      upcoming.add(track);
      if (upcoming.length >= lookahead) {
        break;
      }
    }
    if (upcoming.isNotEmpty) {
      _primeTracks(upcoming, limit: lookahead);
    }
    if (state.recommendations.isNotEmpty) {
      _primeTracks(state.recommendations, limit: 6);
    }
    if (state.mode == PlaybackQueueMode.playlist) {
      unawaited(
        ref.read(audioPlayerProvider.notifier).prefetchFixedQueueTracks(
              state.queue,
              currentIndex: resolvedCurrentIndex,
              count: 2,
            ),
      );
    }
  }

  int _resolvedCurrentIndex() {
    if (state.queue.isEmpty) return 0;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final managedIndex = audioNotifier.currentManagedQueueIndex;
    if (audioNotifier.hasManagedQueue &&
        managedIndex != null &&
        managedIndex >= 0 &&
        managedIndex < state.queue.length) {
      return managedIndex;
    }

    final activeTrackId = state.currentTrackId;
    if (activeTrackId != null && activeTrackId.isNotEmpty) {
      final matchingIndex = state.queue
          .indexWhere((track) => extractTrackId(track) == activeTrackId);
      if (matchingIndex >= 0) {
        return matchingIndex;
      }
    }

    return state.currentIndex.clamp(0, state.queue.length - 1).toInt();
  }

  int _nextPlayableIndex(int fromIndex) {
    for (var i = fromIndex + 1; i < state.queue.length; i++) {
      final track = state.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      return i;
    }
    return -1;
  }

  List<String> _withPlayedTrack(String? trackId) {
    if (trackId == null || trackId.isEmpty) return state.playedTrackIds;
    if (state.playedTrackIds.contains(trackId)) return state.playedTrackIds;
    return [...state.playedTrackIds, trackId];
  }

  bool _shouldCountTrackAsPlayed(String? trackId) {
    if (trackId == null || trackId.isEmpty) return false;
    final playerState = ref.read(audioPlayerProvider);
    final activeTrackId = playerState.videoId?.trim();
    if (activeTrackId == null ||
        activeTrackId.isEmpty ||
        activeTrackId != trackId) {
      return false;
    }
    final durationSeconds = playerState.duration;
    final positionSeconds = playerState.currentPosition;
    if (durationSeconds <= 0) {
      return positionSeconds >= _playedTrackMinSeconds;
    }
    final requiredSeconds = max(
      8,
      min(
        20,
        (durationSeconds * _playedTrackMinRatio).round(),
      ),
    );
    return positionSeconds >= requiredSeconds ||
        positionSeconds >= durationSeconds - 1;
  }

  List<String> _playedTrackIdsAfterCurrentProgress(String? trackId) {
    if (!_shouldCountTrackAsPlayed(trackId)) {
      return state.playedTrackIds;
    }
    return _withPlayedTrack(trackId);
  }

  void _markCurrentTrackPlayedIfEligible() {
    final nextPlayedTrackIds = _playedTrackIdsAfterCurrentProgress(
      state.currentTrackId,
    );
    if (!listEquals(nextPlayedTrackIds, state.playedTrackIds)) {
      state = state.copyWith(playedTrackIds: nextPlayedTrackIds);
    }
  }

  List<Map<String, dynamic>> _uniqueTracks(
    Iterable<dynamic> tracks, {
    Set<String>? excludedIds,
  }) {
    final seen = <String>{...?excludedIds};
    final normalized = <Map<String, dynamic>>[];
    for (final rawTrack in tracks) {
      final track = normalizeTrack(rawTrack);
      final id = extractTrackId(track);
      if (id == null || !seen.add(id)) continue;
      normalized.add(track);
    }
    return normalized;
  }

  void _refreshHistorySignalConsumers() {
    unawaited(
        ref.read(lastPlayedProvider.notifier).loadTracks(forceRefresh: true));
    unawaited(
      ref
          .read(frequentlyPlayedProvider.notifier)
          .loadTracks(forceRefresh: true),
    );
  }

  Future<void> _recordCurrentQueueInteraction(String eventType) async {
    final trackId = state.currentTrackId;
    if (trackId == null || trackId.isEmpty) return;
    final currentIndex = _resolvedCurrentIndex();
    final rawTrack = currentIndex >= 0 && currentIndex < state.queue.length
        ? state.queue[currentIndex]
        : null;
    await recordProxyInteractionEvent(
      eventType,
      trackId: trackId,
      rawTrack: rawTrack,
    );
  }

  Future<List<Map<String, dynamic>>> _fetchRecommendations(
    String seedId, {
    int limit = 12,
  }) async {
    final body = await buildRecommendationRequestBody(
      ref,
      seedId,
      limit: limit,
      avoidIds: state.queue
          .map((track) => extractTrackId(track))
          .whereType<String>()
          .toSet(),
    );
    final res = await postRecommendation(body);

    if (res.statusCode != 200) {
      throw Exception('Recommendation lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks =
        payload['recommendations'] as List<dynamic>? ?? const <dynamic>[];
    return _uniqueTracks(rawTracks, excludedIds: {seedId});
  }

  Future<List<Map<String, dynamic>>> _fetchSimilarTracks(
    String seedId, {
    int limit = 12,
  }) async {
    final res = await appHttpClient
        .post(
          buildProxyUri('/track_details'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'video_id': seedId}),
        )
        .timeout(const Duration(seconds: 12));

    if (res.statusCode != 200) {
      throw Exception('Track detail lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks =
        payload['similar_tracks'] as List<dynamic>? ?? const <dynamic>[];
    return _uniqueTracks(rawTracks, excludedIds: {seedId})
        .take(limit)
        .toList(growable: false);
  }

  Future<void> startRadioSession(dynamic track) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null) return;

    state = PlaybackQueueState(
      mode: PlaybackQueueMode.radio,
      queue: [normalizedTrack],
      currentIndex: 0,
      currentTrackId: videoId,
      playedTrackIds: const [],
    );

    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
      [normalizedTrack],
      initialIndex: 0,
    );
    _refreshHistorySignalConsumers();
    _primeUpcomingQueue();
    unawaited(_appendRadioRecommendations(seedId: videoId));
  }

  Future<void> startDiscoverySession(
    dynamic track, {
    String? sessionName,
  }) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null || videoId.isEmpty) return;

    final frequentSnapshots =
        await HistoryManager.getFrequentlyPlayedTrackSnapshots(
      limit: 2,
    );
    List<Map<String, dynamic>> similarTracks = const [];
    List<Map<String, dynamic>> fillerTracks = const [];
    try {
      similarTracks = await _fetchSimilarTracks(videoId, limit: 10);
    } catch (_) {
      similarTracks = const [];
    }
    if (similarTracks.length < 6) {
      try {
        fillerTracks = await _fetchRecommendations(
          videoId,
          limit: 8 - similarTracks.length,
        );
      } catch (_) {
        fillerTracks = const [];
      }
    }

    final queue = _uniqueTracks(
      [
        normalizedTrack,
        ...similarTracks,
        if (similarTracks.isNotEmpty) ...frequentSnapshots,
        ...fillerTracks,
      ],
      excludedIds: const {},
    );

    await startPlaylistSession(
      playlistId: 'discovery:$videoId',
      playlistName: sessionName ??
          'Inspired by ${normalizedTrack['title']?.toString() ?? 'this track'}',
      tracks: queue,
      currentTrack: normalizedTrack,
    );
  }

  Future<bool> startLocalSession({
    required dynamic track,
    required String path,
  }) async {
    final normalizedTrack = normalizeTrack(track);
    final localTrack = {
      ...normalizedTrack,
      'local_path': path,
    };
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final loaded =
        localTrack['thumbnail'] != null || extractTrackId(localTrack) != null
            ? await audioNotifier.loadLocalWithMeta(path, localTrack)
            : await audioNotifier.loadLocalFile(
                path,
                localTrack['title']?.toString() ?? 'Unknown Track',
              );
    if (!loaded) {
      clearSession();
      return false;
    }

    final videoId = extractTrackId(localTrack);
    if (videoId != null) {
      state = PlaybackQueueState(
        mode: PlaybackQueueMode.radio,
        queue: [localTrack],
        currentIndex: 0,
        currentTrackId: videoId,
        playedTrackIds: const [],
      );
      await audioNotifier.configureManagedQueue(
        [localTrack],
        initialIndex: 0,
      );
      _refreshHistorySignalConsumers();
      _primeTracks([localTrack], limit: 1);
      unawaited(_appendRadioRecommendations(seedId: videoId));
    } else {
      clearSession();
    }

    return true;
  }

  Future<void> startPlaylistSession({
    required String playlistId,
    required String playlistName,
    required Iterable<dynamic> tracks,
    required dynamic currentTrack,
    bool shuffle = false,
  }) async {
    final normalizedQueue = _uniqueTracks(tracks);
    final currentTrackMap = normalizeTrack(currentTrack);
    final currentTrackId = extractTrackId(currentTrackMap);
    if (currentTrackId == null) return;

    if (shuffle && normalizedQueue.length > 1) {
      normalizedQueue.shuffle(_random);
    }

    var currentIndex = normalizedQueue
        .indexWhere((track) => extractTrackId(track) == currentTrackId);
    if (currentIndex < 0) {
      normalizedQueue.insert(0, currentTrackMap);
      currentIndex = 0;
    }

    state = PlaybackQueueState(
      mode: PlaybackQueueMode.playlist,
      queue: normalizedQueue,
      currentIndex: currentIndex,
      currentTrackId: currentTrackId,
      playedTrackIds: const [],
      playlistId: playlistId,
      playlistName: playlistName,
    );

    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
          normalizedQueue,
          initialIndex: currentIndex,
        );
    _refreshHistorySignalConsumers();
    _primeUpcomingQueue();
    unawaited(_refreshPlaylistRecommendations(currentTrackId));
  }

  void clearSession() {
    state = const PlaybackQueueState();
  }

  Future<void> playShuffled() async {
    if (state.mode == PlaybackQueueMode.none) return;

    final playableIndexes = <int>[];
    for (var i = 0; i < state.queue.length; i++) {
      if (isTrackHidden(state.queue[i])) continue;
      playableIndexes.add(i);
    }

    if (playableIndexes.isEmpty) {
      if (state.mode == PlaybackQueueMode.radio &&
          state.recommendations.isNotEmpty) {
        final track = state
            .recommendations[_random.nextInt(state.recommendations.length)];
        await startRadioSession(track);
      }
      return;
    }

    if (playableIndexes.length > 1) {
      playableIndexes.remove(_resolvedCurrentIndex());
    }
    if (playableIndexes.isEmpty) {
      playableIndexes.add(_resolvedCurrentIndex());
    }

    final index = playableIndexes[_random.nextInt(playableIndexes.length)];
    await playQueueIndex(index);
  }

  Future<void> handleTrackChanged(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    if (state.mode == PlaybackQueueMode.none) return;

    final previousTrackId = state.currentTrackId;
    final nextPlayedTrackIds =
        previousTrackId != null && previousTrackId != videoId
            ? _playedTrackIdsAfterCurrentProgress(previousTrackId)
            : state.playedTrackIds;

    final matchingIndex =
        state.queue.indexWhere((track) => extractTrackId(track) == videoId);
    if (matchingIndex >= 0) {
      state = state.copyWith(
        currentIndex: matchingIndex,
        currentTrackId: videoId,
        playedTrackIds: nextPlayedTrackIds,
      );
      if (isTrackHidden(state.queue[matchingIndex])) {
        await playNext();
        return;
      }
    } else {
      state = state.copyWith(
        currentTrackId: videoId,
        playedTrackIds: nextPlayedTrackIds,
      );
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: videoId);
    } else if (state.mode == PlaybackQueueMode.playlist) {
      await _refreshPlaylistRecommendations(videoId);
    }
    _primeUpcomingQueue();
  }

  Future<void> playNext({bool logCurrentSkip = true}) async {
    if (state.mode == PlaybackQueueMode.none) return;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);

    if (audioNotifier.hasManagedQueue) {
      final resolvedCurrentIndex = _resolvedCurrentIndex();
      final nextIndex = _nextPlayableIndex(resolvedCurrentIndex);
      if (nextIndex >= 0) {
        if (logCurrentSkip) {
          unawaited(_recordCurrentQueueInteraction('skip'));
        }
        if (nextIndex == resolvedCurrentIndex + 1 &&
            !isTrackHidden(state.queue[nextIndex])) {
          await audioNotifier.skipManagedQueueNext();
        } else {
          await audioNotifier.playManagedQueueIndex(nextIndex);
        }
        return;
      }
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    if (resolvedCurrentIndex != state.currentIndex) {
      state = state.copyWith(currentIndex: resolvedCurrentIndex);
    }

    final nextIndex = _nextPlayableIndex(resolvedCurrentIndex);
    if (nextIndex >= 0) {
      if (logCurrentSkip) {
        unawaited(_recordCurrentQueueInteraction('skip'));
      }
      await playQueueIndex(nextIndex);
      return;
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      final refreshedNextIndex = _nextPlayableIndex(_resolvedCurrentIndex());
      if (refreshedNextIndex >= 0) {
        if (logCurrentSkip) {
          unawaited(_recordCurrentQueueInteraction('skip'));
        }
        await playQueueIndex(refreshedNextIndex);
      }
    } else if (state.mode == PlaybackQueueMode.playlist &&
        state.hasMoreRecommendations) {
      await _refreshPlaylistRecommendations(
        state.currentTrackId,
        append: true,
      );
      final refreshedNextIndex = _nextPlayableIndex(_resolvedCurrentIndex());
      if (refreshedNextIndex >= 0) {
        if (logCurrentSkip) {
          unawaited(_recordCurrentQueueInteraction('skip'));
        }
        await playQueueIndex(refreshedNextIndex);
      }
    }
  }

  Future<void> playPrevious({bool logCurrentSkip = true}) async {
    if (state.mode == PlaybackQueueMode.none) return;
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (audioNotifier.hasManagedQueue) {
      if (logCurrentSkip && _resolvedCurrentIndex() > 0) {
        unawaited(_recordCurrentQueueInteraction('skip'));
      }
      await audioNotifier.skipManagedQueuePrevious();
      return;
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    final previousIndex = max(0, resolvedCurrentIndex - 1);
    if (logCurrentSkip && previousIndex != resolvedCurrentIndex) {
      unawaited(_recordCurrentQueueInteraction('skip'));
    }
    await playQueueIndex(previousIndex);
  }

  Future<void> playQueueIndex(int index) async {
    if (index < 0 || index >= state.queue.length) return;
    final track = state.queue[index];
    final videoId = extractTrackId(track);
    if (videoId == null) return;

    final previousTrackId = state.currentTrackId;
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (audioNotifier.hasManagedQueue) {
      await audioNotifier.playManagedQueueIndex(index);
    } else {
      final localPath = track['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          File(localPath).existsSync()) {
        final loaded = await audioNotifier.loadLocalWithMeta(localPath, track);
        if (!loaded) return;
        await audioNotifier.play();
      } else {
        await audioNotifier.streamYoutube(videoId, track);
      }
    }
    state = state.copyWith(
      currentIndex: index,
      currentTrackId: videoId,
      playedTrackIds: previousTrackId != null && previousTrackId != videoId
          ? _playedTrackIdsAfterCurrentProgress(previousTrackId)
          : state.playedTrackIds,
    );
    _primeUpcomingQueue();
  }

  void removeFromQueue(String videoId) {
    if (state.mode != PlaybackQueueMode.radio) return;

    final index =
        state.queue.indexWhere((track) => extractTrackId(track) == videoId);
    if (index < 0) return;

    final updatedQueue = [...state.queue]..removeAt(index);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    if (index == resolvedCurrentIndex) {
      final replacementIndex = index.clamp(0, updatedQueue.length - 1).toInt();
      state = state.copyWith(
        queue: updatedQueue,
        currentIndex: replacementIndex,
        currentTrackId: extractTrackId(updatedQueue[replacementIndex]),
        playedTrackIds:
            state.playedTrackIds.where((id) => id != videoId).toList(),
      );
      unawaited(audioNotifier.removeManagedQueueItem(index));
      unawaited(playQueueIndex(replacementIndex));
      return;
    }

    var nextCurrentIndex = resolvedCurrentIndex;
    if (index < nextCurrentIndex) {
      nextCurrentIndex -= 1;
    }
    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex,
      playedTrackIds:
          state.playedTrackIds.where((id) => id != videoId).toList(),
    );
    unawaited(audioNotifier.removeManagedQueueItem(index));
    _primeUpcomingQueue();
  }

  void setQueueTrackHidden(String trackId, bool isHidden) {
    if (state.mode == PlaybackQueueMode.none) return;
    final updatedQueue = state.queue.map((track) {
      final id = extractTrackId(track);
      if (id != trackId) return track;
      return copyTrackWithHidden(track, isHidden);
    }).toList(growable: false);
    state = state.copyWith(queue: updatedQueue);
    _primeUpcomingQueue();
  }

  void removePlaylistTrack(String trackId) {
    if (state.mode != PlaybackQueueMode.playlist) return;
    final index =
        state.queue.indexWhere((track) => extractTrackId(track) == trackId);
    if (index < 0) return;

    final updatedQueue = [...state.queue]..removeAt(index);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    var nextCurrentIndex = _resolvedCurrentIndex();
    if (index < nextCurrentIndex) {
      nextCurrentIndex -= 1;
    }
    nextCurrentIndex =
        nextCurrentIndex.clamp(0, updatedQueue.length - 1).toInt();
    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex,
      currentTrackId: extractTrackId(updatedQueue[nextCurrentIndex]),
      playedTrackIds:
          state.playedTrackIds.where((id) => id != trackId).toList(),
    );
    unawaited(audioNotifier.removeManagedQueueItem(index));
    _primeUpcomingQueue();
  }

  Future<void> enqueueTrack(dynamic track, {bool playNext = true}) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null) return;

    if (state.mode == PlaybackQueueMode.none || state.queue.isEmpty) {
      await startRadioSession(normalizedTrack);
      return;
    }

    if (state.queue.any((entry) => extractTrackId(entry) == videoId)) {
      return;
    }

    final nextRecommendations = state.recommendations
        .where((entry) => extractTrackId(entry) != videoId)
        .toList(growable: false);
    final resolvedCurrentIndex = _resolvedCurrentIndex();
    final insertIndex = playNext
        ? (resolvedCurrentIndex + 1).clamp(0, state.queue.length).toInt()
        : state.queue.length;
    final updatedQueue = [...state.queue];
    updatedQueue.insert(insertIndex, normalizedTrack);
    state = state.copyWith(
      queue: updatedQueue,
      recommendations: nextRecommendations,
    );
    unawaited(ref.read(audioPlayerProvider.notifier).insertManagedQueueTracks(
      insertIndex,
      [normalizedTrack],
    ));
    _primeTracks([normalizedTrack], limit: 1);
  }

  void reorderQueue(int oldIndex, int newIndex) {
    if (oldIndex < 0 || oldIndex >= state.queue.length) return;
    final normalizedNewIndex = newIndex.clamp(0, state.queue.length);
    final updatedQueue = [...state.queue];
    final movedTrack = updatedQueue.removeAt(oldIndex);
    var targetIndex = normalizedNewIndex;
    if (targetIndex > oldIndex) {
      targetIndex -= 1;
    }
    targetIndex = targetIndex.clamp(0, updatedQueue.length);
    updatedQueue.insert(targetIndex, movedTrack);

    final activeTrackId = state.currentTrackId;
    final nextCurrentIndex = activeTrackId == null
        ? state.currentIndex.clamp(0, updatedQueue.length - 1).toInt()
        : updatedQueue
            .indexWhere((track) => extractTrackId(track) == activeTrackId);

    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex < 0 ? 0 : nextCurrentIndex,
    );
    unawaited(
      ref.read(audioPlayerProvider.notifier).moveManagedQueueItem(
            oldIndex,
            normalizedNewIndex,
          ),
    );

    if (state.mode == PlaybackQueueMode.playlist && state.playlistId != null) {
      ref
          .read(playlistProvider.notifier)
          .replaceTracks(state.playlistId!, updatedQueue);
    }
    _primeUpcomingQueue();
  }

  void reorderUpcomingQueue(int oldIndex, int newIndex) {
    final activeIndex = _resolvedCurrentIndex();
    final upcomingStartIndex = activeIndex + 1;
    if (upcomingStartIndex >= state.queue.length) return;

    final upcomingLength = state.queue.length - upcomingStartIndex;
    if (oldIndex < 0 || oldIndex >= upcomingLength) return;

    final actualOldIndex = upcomingStartIndex + oldIndex;
    final actualNewIndex = upcomingStartIndex + newIndex;
    reorderQueue(actualOldIndex, actualNewIndex);
  }

  Future<void> loadMore() async {
    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      return;
    }
    if (state.mode == PlaybackQueueMode.playlist) {
      if (!state.hasMoreRecommendations) return;
      await _refreshPlaylistRecommendations(
        state.currentTrackId,
        append: true,
      );
    }
  }

  Future<void> _appendRadioRecommendations({String? seedId}) async {
    if (state.mode != PlaybackQueueMode.radio) return;
    final effectiveSeedId = seedId ?? state.currentTrackId;
    if (effectiveSeedId == null || effectiveSeedId.isEmpty) return;
    if (state.isLoadingQueue) return;

    final remaining = state.queue.length - _resolvedCurrentIndex() - 1;
    if (remaining > 5) return;

    state = state.copyWith(isLoadingQueue: true);
    try {
      final existingIds =
          state.queue.map(extractTrackId).whereType<String>().toSet();
      final fetchedTracks = await _fetchRecommendations(effectiveSeedId);
      final newTracks = fetchedTracks
          .where((track) => !existingIds.contains(extractTrackId(track)))
          .toList(growable: false);
      if (newTracks.isNotEmpty) {
        state = state.copyWith(queue: [...state.queue, ...newTracks]);
        unawaited(
          ref.read(audioPlayerProvider.notifier).appendManagedQueueTracks(
                newTracks,
              ),
        );
        _primeTracks(newTracks);
      }
    } catch (_) {
      // Radio queue loading stays silent.
    } finally {
      if (state.mode == PlaybackQueueMode.radio) {
        state = state.copyWith(isLoadingQueue: false);
      }
    }
  }

  Future<void> _refreshPlaylistRecommendations(
    String? seedId, {
    bool append = false,
  }) async {
    if (state.mode != PlaybackQueueMode.playlist) return;
    if (seedId == null || seedId.isEmpty) return;
    if (state.isLoadingRecommendations) return;
    if (append && !state.hasMoreRecommendations) return;

    final existingRecommendations = state.recommendations;
    state = state.copyWith(
      isLoadingRecommendations: true,
    );
    try {
      final excludedIds = <String>{
        ...state.queue.map(extractTrackId).whereType<String>(),
        if (append)
          ...state.recommendations.map(extractTrackId).whereType<String>(),
      };
      final targetCount = append ? 16 : 18;

      List<Map<String, dynamic>> similarTracks = const [];
      try {
        similarTracks = await _fetchSimilarTracks(seedId, limit: targetCount);
      } catch (_) {
        similarTracks = const [];
      }

      List<Map<String, dynamic>> fallbackTracks = const [];
      if (similarTracks.length < targetCount) {
        try {
          fallbackTracks = await _fetchRecommendations(
            seedId,
            limit: targetCount - similarTracks.length,
          );
        } catch (_) {
          fallbackTracks = const [];
        }
      }

      final fetchedTracks = _uniqueTracks(
        [
          ...similarTracks,
          ...fallbackTracks,
        ],
        excludedIds: excludedIds,
      );
      final nextRecommendations = fetchedTracks
          .where((track) => !excludedIds.contains(extractTrackId(track)))
          .toList(growable: false);
      if (!append && nextRecommendations.isEmpty) {
        state = state.copyWith(
          recommendations: existingRecommendations,
          hasMoreRecommendations: false,
        );
        return;
      }
      final isDiscoverySession =
          state.playlistId?.startsWith('discovery:') == true;
      if (isDiscoverySession && nextRecommendations.isNotEmpty) {
        state = state.copyWith(
          queue: [...state.queue, ...nextRecommendations],
          recommendations: const [],
          hasMoreRecommendations: true,
        );
        unawaited(
          ref.read(audioPlayerProvider.notifier).appendManagedQueueTracks(
                nextRecommendations,
              ),
        );
      } else {
        state = state.copyWith(
          recommendations: append
              ? [...state.recommendations, ...nextRecommendations]
              : nextRecommendations,
          hasMoreRecommendations: nextRecommendations.isNotEmpty,
        );
      }
      _primeTracks(nextRecommendations);
    } catch (_) {
      // Recommendation shelf loading stays silent.
    } finally {
      if (state.mode == PlaybackQueueMode.playlist) {
        state = state.copyWith(isLoadingRecommendations: false);
      }
    }
  }
}

final playbackQueueProvider =
    StateNotifierProvider<PlaybackQueueNotifier, PlaybackQueueState>((ref) {
  final notifier = PlaybackQueueNotifier(ref);
  final audioNotifier = ref.read(audioPlayerProvider.notifier);

  Future<void> handleTrackCompleted() async {
    notifier._markCurrentTrackPlayedIfEligible();
    await notifier._recordCurrentQueueInteraction('complete');
    await notifier.playNext(logCurrentSkip: false);
  }

  Future<void> handleTrackChanged(String? videoId) =>
      notifier.handleTrackChanged(videoId);

  audioNotifier.onTrackCompleted = handleTrackCompleted;
  audioNotifier.onTrackChanged = handleTrackChanged;
  if (globalAudioHandler is AuralisAudioHandler) {
    final handler = globalAudioHandler as AuralisAudioHandler;
    handler.onSkipToNext = notifier.playNext;
    handler.onSkipToPrevious = notifier.playPrevious;
  }

  ref.onDispose(() {
    if (identical(audioNotifier.onTrackCompleted, handleTrackCompleted)) {
      audioNotifier.onTrackCompleted = null;
    }
    if (identical(audioNotifier.onTrackChanged, handleTrackChanged)) {
      audioNotifier.onTrackChanged = null;
    }
    if (globalAudioHandler is AuralisAudioHandler) {
      final handler = globalAudioHandler as AuralisAudioHandler;
      if (identical(handler.onSkipToNext, notifier.playNext)) {
        handler.onSkipToNext = null;
      }
      if (identical(handler.onSkipToPrevious, notifier.playPrevious)) {
        handler.onSkipToPrevious = null;
      }
    }
  });

  return notifier;
});
