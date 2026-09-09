import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart' show audioPlayerProvider;
import 'audio_provider_history.dart';
import 'audio_queue_runtime.dart';
import 'audio_service_bridge.dart';
import 'auth_provider.dart';
import 'interaction_events.dart';
import 'playlist_provider.dart';
import 'proxy_runtime.dart';
import 'track_metadata.dart';

enum PlaybackQueueMode { none, radio, playlist }

List<Map<String, dynamic>> matchQueueTracks(
    Iterable<Map<String, dynamic>> queue, String query) {
  final words = query.trim().toLowerCase().split(RegExp(r'\s+'));
  if (query.trim().isEmpty) return const [];
  return queue.where((track) {
    final text = ['title', 'artist', 'author', 'channel']
        .map((key) => track[key] ?? '').join(' ').toLowerCase();
    return words.every(text.contains);
  }).toList(growable: false);
}

class _RelatedTracksPage {
  final List<Map<String, dynamic>> tracks;
  final bool hasMore;
  final bool temporarilyUnavailable;

  const _RelatedTracksPage({
    required this.tracks,
    required this.hasMore,
    this.temporarilyUnavailable = false,
  });
}

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

  Map<String, dynamic> toSnapshot() => {
    'version': 1, 'mode': mode.name, 'queue': queue,
    'current_track_id': currentTrackId, 'played_track_ids': playedTrackIds,
    'playlist_id': playlistId, 'playlist_name': playlistName,
    'recommendations': recommendations,
    'has_more': hasMoreRecommendations,
  };

  static PlaybackQueueState? fromSnapshot(dynamic raw) {
    if (raw is! Map || raw['version'] != 1 || raw['queue'] is! List) return null;
    final mode = PlaybackQueueMode.values.where((v) => v.name == raw['mode']).firstOrNull;
    if (mode == null || mode == PlaybackQueueMode.none) return null;
    final queue = (raw['queue'] as List).whereType<Map>()
        .map((v) => Map<String, dynamic>.from(v)).toList();
    if (queue.isEmpty || queue.any((v) => extractTrackId(v) == null)) return null;
    final index = queue.indexWhere((v) => extractTrackId(v) == raw['current_track_id']);
    if (index < 0) return null;
    return PlaybackQueueState(mode: mode, queue: queue, currentIndex: index,
      currentTrackId: extractTrackId(queue[index]),
      playedTrackIds: (raw['played_track_ids'] as List? ?? []).whereType<String>().toList(),
      playlistId: raw['playlist_id'] as String?, playlistName: raw['playlist_name'] as String?,
      recommendations: (raw['recommendations'] as List? ?? []).whereType<Map>()
          .map((v) => Map<String, dynamic>.from(v)).toList(),
      hasMoreRecommendations: raw['has_more'] != false);
  }

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
  DateTime? _recommendationRetryAfter;
  int _recommendationSessionRevision = 0;

  final String storageScopeId;
  late final Future<void> restored;
  Future<void> _pendingSave = Future.value();
  bool _restoring = false;
  int _stateRevision = 0;

  PlaybackQueueNotifier(this.ref, this.storageScopeId) : super(const PlaybackQueueState()) {
    restored = _restoreSession();
  }

  @override
  set state(PlaybackQueueState value) {
    _stateRevision++;
    super.state = value;
    if (!_restoring) unawaited(saveSession());
  }

  Future<void> saveSession() {
    final payload = jsonEncode(state.toSnapshot());
    // Serialize writes and capture the account before any await.
    _pendingSave = _pendingSave.then((_) async {
      final file = await getScopedDataFile('playback_session.json', storageScopeId);
      final temporary = File('${file.path}.tmp');
      await temporary.writeAsString(payload, flush: true);
      await temporary.rename(file.path);
    }).catchError((Object error) {
      debugPrint('Playback session save failed: $error');
    });
    return _pendingSave;
  }

  Future<void> _restoreSession() async {
    final revision = _stateRevision;
    try {
      final file = await getScopedDataFile('playback_session.json', storageScopeId);
      if (!await file.exists()) return;
      final saved = PlaybackQueueState.fromSnapshot(jsonDecode(await file.readAsString()));
      final audio = ref.read(audioPlayerProvider.notifier);
      await audio.rememberedTrackRestored;
      if (!mounted || revision != _stateRevision || saved == null ||
          ref.read(authProvider).storageScopeId != storageScopeId) return;
      _restoring = true;
      state = saved;
      audio.restoreTrackPreview(saved.queue[saved.currentIndex]);
    } catch (error) {
      debugPrint('Playback session restore failed: $error');
    } finally {
      _restoring = false;
    }
  }

  Future<bool> resumeSavedQueue() async {
    await restored;
    if (!mounted || state.queue.isEmpty || state.mode == PlaybackQueueMode.none) return false;
    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
      state.queue, initialIndex: state.currentIndex);
    return true;
  }

  void _primeTracks(
    Iterable<dynamic> tracks, {
    int limit = 2,
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

  void _primeUpcomingQueue({int lookahead = 2}) {
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
  }

  int _resolvedCurrentIndex() {
    if (state.queue.isEmpty) return 0;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final managedTrackId = audioNotifier.currentManagedQueueTrackId;
    if (audioNotifier.hasManagedQueue &&
        managedTrackId != null &&
        managedTrackId.isNotEmpty) {
      final logicalIndex = trackIndexByIdentity(state.queue, managedTrackId);
      if (logicalIndex >= 0) return logicalIndex;
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

  Future<_RelatedTracksPage> _fetchRelatedTracks(
    String seedId, {
    int limit = 18,
    int offset = 0,
    Set<String> avoidIds = const {},
  }) async {
    final seed = state.queue.cast<Map<String, dynamic>?>().firstWhere(
          (track) => extractTrackId(track) == seedId,
          orElse: () => null,
        );
    final res = await appHttpClient
        .post(
          buildProxyUri('/related_tracks'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'track_key': extractPlaybackRoutingKey(seed) ?? seedId,
            'title': seed?['title']?.toString() ?? '',
            'artist': seed?['channel']?.toString() ??
                seed?['artist']?.toString() ??
                '',
            'album': seed?['album']?.toString() ?? '',
            'duration': seed?['duration'] ?? 0,
            'source_id': extractPlaybackSourceId(seed) ?? '',
            'source_provider': extractPlaybackProvider(seed),
            'user_scope_id': ref.read(authProvider).storageScopeId,
            'avoid_ids': avoidIds.toList(growable: false),
            'offset': offset,
            'limit': limit,
          }),
        )
        .timeout(const Duration(seconds: 30));

    if (res.statusCode != 200) {
      throw Exception('Related-track lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks = payload['tracks'] as List<dynamic>? ?? const <dynamic>[];
    debugPrint('[EBB:related-tracks] seed=$seedId status=${payload['status']} '
        'received=${rawTracks.length} excluded=${avoidIds.length}');
    return _RelatedTracksPage(
      tracks: _uniqueTracks(rawTracks, excludedIds: {...avoidIds, seedId}),
      hasMore: payload['has_more'] == true,
      temporarilyUnavailable:
          payload['status']?.toString() == 'temporarily_unavailable',
    );
  }

  Future<void> startRadioSession(dynamic track) async {
    _recommendationSessionRevision++;
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
    Iterable<dynamic> initialCandidates = const [],
  }) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null || videoId.isEmpty) return;

    // Search already has query-related and similar tracks. Use those persisted
    // results immediately instead of blocking playback on another network
    // round-trip. startPlaylistSession schedules background replenishment.
    final contextualTracks = _uniqueTracks(
      initialCandidates,
      excludedIds: {videoId},
    ).take(18).toList(growable: false);
    final queue = <Map<String, dynamic>>[
      normalizedTrack,
      ...contextualTracks,
    ];

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
    _recommendationSessionRevision++;
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
    final existingIndex = trackIndexByIdentity(state.queue, extractTrackId(currentTrack));
    if (!shuffle && state.mode == PlaybackQueueMode.playlist &&
        state.playlistId == playlistId && existingIndex >= 0) {
      await playQueueIndex(existingIndex);
      return;
    }
    final sessionRevision = ++_recommendationSessionRevision;
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
    _recommendationRetryAfter = null;

    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
          normalizedQueue,
          initialIndex: currentIndex,
        );
    if (!mounted || sessionRevision != _recommendationSessionRevision) return;
    _refreshHistorySignalConsumers();
    _primeUpcomingQueue();
    unawaited(_refreshPlaylistRecommendations(currentTrackId));
  }

  void clearSession() {
    _recommendationSessionRevision++;
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
    if (_restoring) return;
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
      final managedIndex = audioNotifier.managedQueueIndexForTrackId(videoId);
      if (managedIndex != null) {
        final played = await audioNotifier.playManagedQueueTrack(videoId);
        if (!played) return;
      } else {
        final configured = await audioNotifier.configureManagedQueue(
          state.queue,
          initialIndex: index,
        );
        if (!configured) return;
      }
    } else {
      final localPath = track['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          File(localPath).existsSync()) {
        final loaded = await audioNotifier.loadLocalWithMeta(localPath, track);
        if (!loaded) return;
        await audioNotifier.play();
      } else {
        final configured = await audioNotifier.configureManagedQueue(
          state.queue,
          initialIndex: index,
        );
        if (!configured) return;
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
      unawaited(audioNotifier.removeManagedQueueTrack(videoId));
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
    unawaited(audioNotifier.removeManagedQueueTrack(videoId));
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
    unawaited(audioNotifier.removeManagedQueueTrack(trackId));
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
      final retryAfter = _recommendationRetryAfter;
      if (retryAfter != null && DateTime.now().isBefore(retryAfter)) return;
      await _refreshPlaylistRecommendations(
        state.currentTrackId,
        append: true,
      );
    }
  }

  Future<void> retryRelatedSongs() async {
    if (state.mode != PlaybackQueueMode.playlist) return;
    _recommendationRetryAfter = null;
    state = state.copyWith(hasMoreRecommendations: true);
    await _refreshPlaylistRecommendations(
      state.currentTrackId,
      append: state.recommendations.isNotEmpty,
    );
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
      final page = await _fetchRelatedTracks(
        effectiveSeedId,
        avoidIds: existingIds,
      );
      final newTracks = page.tracks
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

    final sessionRevision = _recommendationSessionRevision;
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
      final page = await _fetchRelatedTracks(
        seedId,
        limit: append ? 16 : 18,
        avoidIds: excludedIds,
      );
      if (!mounted || sessionRevision != _recommendationSessionRevision) {
        return;
      }
      final fetchedTracks = page.tracks;
      final nextRecommendations = fetchedTracks
          .where((track) => !excludedIds.contains(extractTrackId(track)))
          .toList(growable: false);
      if (nextRecommendations.isEmpty && page.temporarilyUnavailable) {
        _recommendationRetryAfter = DateTime.now().add(
          const Duration(seconds: 8),
        );
        state = state.copyWith(
          recommendations: existingRecommendations,
          hasMoreRecommendations: true,
        );
        return;
      }
      if (nextRecommendations.isEmpty) {
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
          hasMoreRecommendations: page.hasMore,
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
          hasMoreRecommendations: page.hasMore,
        );
      }
      if (nextRecommendations.isNotEmpty) {
        _recommendationRetryAfter = null;
      }
      _primeTracks(nextRecommendations);
    } catch (error) {
      if (!mounted || sessionRevision != _recommendationSessionRevision) return;
      debugPrint('[EBB:related-tracks] request failed: ${error.runtimeType}');
      _recommendationRetryAfter = DateTime.now().add(
        const Duration(seconds: 8),
      );
      if (state.mode == PlaybackQueueMode.playlist) {
        state = state.copyWith(hasMoreRecommendations: true);
      }
    } finally {
      if (mounted && sessionRevision == _recommendationSessionRevision &&
          state.mode == PlaybackQueueMode.playlist) {
        state = state.copyWith(isLoadingRecommendations: false);
      }
    }
  }
}

final playbackQueueProvider =
    StateNotifierProvider<PlaybackQueueNotifier, PlaybackQueueState>((ref) {
  final scope = ref.watch(authProvider.select((value) => value.storageScopeId));
  final notifier = PlaybackQueueNotifier(ref, scope);
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
  audioNotifier.onResumeSavedQueue = notifier.resumeSavedQueue;
  if (globalAudioHandler is AuralisAudioHandler) {
    final handler = globalAudioHandler as AuralisAudioHandler;
    handler.onSkipToNext = notifier.playNext;
    handler.onSkipToPrevious = notifier.playPrevious;
  }

  ref.onDispose(() {
    if (audioNotifier.onResumeSavedQueue == notifier.resumeSavedQueue) {
      audioNotifier.onResumeSavedQueue = null;
    }
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
