// ignore_for_file: experimental_member_use

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'dart:io';
import 'dart:convert';
import 'dart:async';
import 'dart:math';
import 'package:http/http.dart' as http;
import '../ffi/audio_ffi.dart';
import 'audio_media_session_runtime.dart';
import 'audio_queue_runtime.dart';
import 'audio_service_bridge.dart';
import 'audio_sleep_timer_policy.dart';
import 'audio_stream_runtime.dart';
import 'auth_provider.dart';
import 'history_manager.dart';
import 'interaction_events.dart';
import 'library_catalog_provider.dart';
import 'playback_models.dart';
import 'proxy_runtime.dart';
import 'remembered_track_store.dart';
import 'stream_loop_policy.dart';
import 'track_metadata.dart';
import 'package:just_audio/just_audio.dart' hide PlayerState;

export 'cloud_search_queries.dart';
export 'audio_service_bridge.dart';
export 'history_manager.dart';
export 'interaction_events.dart';
export 'library_catalog_provider.dart';
export 'playback_models.dart';
export 'proxy_runtime.dart';
export 'recommendation_feed_models.dart';
export 'search_semantics.dart';
export 'search_payload_runtime.dart';
export 'suggest_provider.dart';
export 'track_metadata.dart';

int _parseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

class AudioPlayerNotifier extends StateNotifier<PlayerState> {
  static const int _historyCommitMinSeconds = 12;
  static const double _historyCommitMinRatio = 0.35;
  final AudioEngineFFI audioEngine;
  Map<String, dynamic>? _restorableTrackMeta;
  Future<void> Function()? onTrackCompleted;
  Future<void> Function(String? videoId)? onTrackChanged;
  Timer? _playbackTimer;
  final streamPlayer = AudioPlayer(useLazyPreparation: false);
  final RememberedTrackStore _rememberedTrackStore =
      const RememberedTrackStore();
  final List<AudioPlayer> _prefetchPlayers = List<AudioPlayer>.generate(
    2,
    (_) => AudioPlayer(
      handleInterruptions: false,
      androidApplyAudioAttributes: false,
      handleAudioSessionActivation: false,
      useLazyPreparation: false,
    ),
  );
  bool _activeStream = false;
  final AudioStreamRuntime _streamRuntime = AudioStreamRuntime();
  StreamSubscription<Duration>? _streamPositionSub;
  StreamSubscription<Duration?>? _streamDurationSub;
  StreamSubscription<dynamic>? _streamPlaybackSub;
  StreamSubscription<dynamic>? _streamPlaybackErrorSub;
  StreamSubscription<int?>? _streamCurrentIndexSub;
  Future<void> _streamCommandQueue = Future<void>.value();
  String? _cachedStreamVideoId;
  int _streamLoadVersion = 0;
  bool _desiredStreamPlaying = false;
  String? _completedTrackIdNotified;
  bool _streamTransitionInProgress = false;
  bool _streamSeekRecoveryInProgress = false;
  Future<void>? _streamSeekDrainFuture;
  int? _pendingStreamSeekSeconds;
  bool _managedQueueActive = false;
  List<Map<String, dynamic>> _managedQueueTracks = const [];
  final List<String?> _prefetchedTrackIds = <String?>[null, null];
  Timer? _sleepTimer;
  DateTime? _sleepTimerEndsAt;
  bool _streamFailureRecoveryInProgress = false;
  int _lastFailureRecoveryLoadVersion = -1;
  Map<String, dynamic>? _pendingHistorySnapshot;
  String? _historyCommittedTrackId;

  AudioPlayerNotifier(this.audioEngine) : super(PlayerState()) {
    audioEngine.pause(); // Kill ghost audio surviving hot restarts
    _bindStreamPlayer();
    for (final player in _prefetchPlayers) {
      unawaited(player.setVolume(0));
    }

    if (globalAudioHandler is AuralisAudioHandler) {
      final handler = globalAudioHandler as AuralisAudioHandler;
      handler.onPlay = play;
      handler.onPause = pause;
      handler.onSeek = (Duration d) => seek(d.inSeconds);
      handler.onFastForward = () => seek(state.currentPosition + 10);
      handler.onRewind = () => seek(state.currentPosition - 10);
      handler.onStop = () => stopPlayback(resetState: true);
      handler.onSkipToNext = skipManagedQueueNext;
      handler.onSkipToPrevious = skipManagedQueuePrevious;
    }
    unawaited(_restoreRememberedTrack());
  }

  @override
  set state(PlayerState value) {
    final previousState = super.state;
    bool shouldBroadcast = super.state.isPlaying != value.isPlaying ||
        super.state.currentTrackName != value.currentTrackName ||
        super.state.isDownloading != value.isDownloading ||
        (super.state.currentPosition - value.currentPosition).abs() > 1;

    super.state = value;

    if (previousState.videoId != value.videoId) {
      _completedTrackIdNotified = null;
      _historyCommittedTrackId = null;
      final activeTrackId = value.videoId?.trim();
      final pendingTrackId = extractTrackId(_pendingHistorySnapshot);
      if (activeTrackId == null ||
          activeTrackId.isEmpty ||
          pendingTrackId != activeTrackId) {
        _pendingHistorySnapshot = null;
      }
      unawaited(onTrackChanged?.call(value.videoId) ?? Future<void>.value());
    }

    if (shouldBroadcast) {
      _broadcastState();
    }
  }

  void _notifyTrackCompletedIfNeeded() {
    if (_streamTransitionInProgress || state.isLooping) return;
    if (_managedQueueActive) {
      final currentIndex = streamPlayer.currentIndex ?? 0;
      if (currentIndex >= 0 && currentIndex < _managedQueueTracks.length - 1) {
        return;
      }
    }
    final videoId = state.videoId;
    if (videoId == null || videoId.isEmpty) return;
    if (_completedTrackIdNotified == videoId) return;
    unawaited(_commitPendingHistoryIfEligible(force: true));
    _completedTrackIdNotified = videoId;
    unawaited(onTrackCompleted?.call() ?? Future<void>.value());
  }

  void _broadcastState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    broadcastPlaybackState(
      handler: handler,
      state: state,
      managedQueueActive: _managedQueueActive,
      currentQueueIndex: streamPlayer.currentIndex,
      managedQueueLength: _managedQueueTracks.length,
    );
    handler.broadcastMediaItem(
      currentMediaItem(
        state: state,
        managedQueueActive: _managedQueueActive,
        currentIndex: streamPlayer.currentIndex,
        managedQueueTracks: _managedQueueTracks,
        restorableTrackMeta: _restorableTrackMeta,
      ),
    );
  }

  void _broadcastStoppedState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    broadcastStoppedState(handler: handler);
  }

  void refreshMediaSession() {
    if (state.videoId == null || state.videoId!.isEmpty) {
      _broadcastStoppedState();
      return;
    }
    _broadcastManagedQueue();
    _broadcastState();
  }

  Future<void> _persistRememberedTrack() async {
    await _rememberedTrackStore.persistSnapshot(_restorableTrackMeta);
  }

  Future<void> _restoreRememberedTrack() async {
    final snapshot = await _rememberedTrackStore.restoreSnapshot();
    final trackId = extractTrackId(snapshot);
    if (snapshot == null || trackId == null || trackId.isEmpty) return;
    _restorableTrackMeta = snapshot;
    if (!mounted) return;
    state = state.copyWith(
      isPlaying: false,
      isDownloading: false,
      currentTrackName: snapshot['title']?.toString() ?? 'Unknown Track',
      thumbnail: snapshot['thumbnail']?.toString(),
      artist: (snapshot['author'] ?? snapshot['artist'] ?? snapshot['channel'])
          ?.toString(),
      videoId: trackId,
      duration: _parseInt(snapshot['duration']),
      currentPosition: 0,
      currentPositionMs: 0,
    );
  }

  void _rememberTrackMeta(
    dynamic rawTrack, {
    String? localPath,
  }) {
    final snapshot =
        buildRememberedTrackSnapshot(rawTrack, localPath: localPath);
    if (snapshot == null) return;
    _restorableTrackMeta = snapshot;
    _stageHistorySnapshot(snapshot);
    unawaited(_persistRememberedTrack());
  }

  void _stageHistorySnapshot(
    dynamic rawTrack, {
    String? localPath,
  }) {
    final snapshot =
        buildRememberedTrackSnapshot(rawTrack, localPath: localPath);
    final trackId = extractTrackId(snapshot);
    if (snapshot == null || trackId == null || trackId.isEmpty) return;
    _pendingHistorySnapshot = Map<String, dynamic>.from(snapshot);
    _historyCommittedTrackId = null;
  }

  void _commitTrackToHistorySoon() {
    unawaited(_activatePendingHistorySnapshotIfReady());
  }

  Future<void> _activatePendingHistorySnapshotIfReady() async {
    final snapshot = _pendingHistorySnapshot;
    final pendingTrackId = extractTrackId(snapshot);
    if (snapshot == null ||
        pendingTrackId == null ||
        pendingTrackId.isEmpty ||
        _historyCommittedTrackId == pendingTrackId) {
      return;
    }
    final activeTrackId = state.videoId?.trim();
    if (activeTrackId == null ||
        activeTrackId.isEmpty ||
        activeTrackId != pendingTrackId) {
      return;
    }
    final hasStartedPlayback = state.currentPositionMs > 0 ||
        state.currentPosition > 0 ||
        state.isPlaying;
    if (!hasStartedPlayback) {
      return;
    }
    _historyCommittedTrackId = pendingTrackId;
    await HistoryManager.addHistoryTrack(
      snapshot,
      localPath: snapshot['local_path']?.toString(),
    );
  }

  bool _isCurrentTrackHistoryEligible() {
    final activeTrackId = state.videoId?.trim();
    final pendingTrackId = extractTrackId(_pendingHistorySnapshot);
    if (activeTrackId == null ||
        activeTrackId.isEmpty ||
        pendingTrackId == null ||
        pendingTrackId.isEmpty ||
        pendingTrackId != activeTrackId) {
      return false;
    }
    final positionSeconds = state.currentPosition;
    final durationSeconds = state.duration;
    if (positionSeconds <= 0) return false;
    if (durationSeconds <= 0) {
      return positionSeconds >= _historyCommitMinSeconds;
    }
    final requiredSeconds = max(
      8,
      min(
        20,
        (durationSeconds * _historyCommitMinRatio).round(),
      ),
    );
    return positionSeconds >= requiredSeconds ||
        positionSeconds >= durationSeconds - 1;
  }

  Future<void> _commitPendingHistoryIfEligible({
    bool force = false,
  }) async {
    final snapshot = _pendingHistorySnapshot;
    final pendingTrackId = extractTrackId(snapshot);
    if (snapshot == null ||
        pendingTrackId == null ||
        pendingTrackId.isEmpty ||
        _historyCommittedTrackId == pendingTrackId) {
      return;
    }
    final activeTrackId = state.videoId?.trim();
    if (!force &&
        (activeTrackId == null ||
            activeTrackId.isEmpty ||
            activeTrackId != pendingTrackId ||
            !_isCurrentTrackHistoryEligible())) {
      return;
    }
    if (force &&
        activeTrackId != null &&
        activeTrackId.isNotEmpty &&
        activeTrackId != pendingTrackId) {
      return;
    }
    _historyCommittedTrackId = pendingTrackId;
    await HistoryManager.addHistoryTrack(
      snapshot,
      localPath: snapshot['local_path']?.toString(),
    );
  }

  Future<bool> _restoreRememberedTrackForPlayback() async {
    final snapshot = _restorableTrackMeta;
    if (snapshot == null) return false;
    final localPath = snapshot['local_path']?.toString();
    if (localPath != null && localPath.isNotEmpty) {
      final loaded = await loadLocalWithMeta(localPath, snapshot);
      if (loaded) {
        return true;
      }
      _restorableTrackMeta = {
        ...snapshot,
      }..remove('local_path');
      unawaited(_persistRememberedTrack());
    }
    final trackId = extractTrackId(snapshot);
    if (trackId == null || trackId.isEmpty) return false;
    await streamYoutube(trackId, snapshot);
    return state.videoId == trackId;
  }

  void _broadcastManagedQueue() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    broadcastManagedQueue(
      handler: handler,
      managedQueueActive: _managedQueueActive,
      managedQueueTracks: _managedQueueTracks,
    );
  }

  void _bindStreamPlayer() {
    _streamDurationSub = streamPlayer.durationStream.listen((duration) {
      if (!_activeStream || duration == null) return;
      final seconds = duration.inSeconds;
      if (seconds > 0 && seconds != state.duration) {
        state = state.copyWith(duration: seconds);
      }
    });

    _streamCurrentIndexSub = streamPlayer.currentIndexStream.listen((index) {
      if (!_activeStream || !_managedQueueActive) return;
      if (index == null || index < 0 || index >= _managedQueueTracks.length) {
        return;
      }
      _streamRuntime.markActivePlaybackVideoId(
        extractTrackId(_managedQueueTracks[index]),
      );
      _applyManagedTrackMetadata(index, resetPosition: true);
      unawaited(_refreshManagedQueueWarmup());
    });

    _streamPositionSub = streamPlayer.positionStream.listen((position) {
      if (!_activeStream) return;
      final seconds = position.inSeconds;
      final milliseconds = position.inMilliseconds;
      if (seconds != state.currentPosition ||
          milliseconds != state.currentPositionMs) {
        state = state.copyWith(
          currentPosition: seconds,
          currentPositionMs: milliseconds,
        );
        unawaited(_activatePendingHistorySnapshotIfReady());
        unawaited(_commitPendingHistoryIfEligible());
      }
    });

    _streamPlaybackSub = streamPlayer.playerStateStream.listen(
      (playerState) {
        if (!_activeStream) return;

        final processingState = playerState.processingState;
        final isCompleted = processingState == ProcessingState.completed;
        final isBuffering = playerState.playing &&
            (processingState == ProcessingState.loading ||
                processingState == ProcessingState.buffering);
        final nextDuration = streamPlayer.duration?.inSeconds ?? state.duration;
        final nextDurationMs = nextDuration * 1000;
        final cappedPosition =
            isCompleted ? nextDuration : streamPlayer.position.inSeconds;
        final cappedPositionMs =
            isCompleted ? nextDurationMs : streamPlayer.position.inMilliseconds;

        _playbackTimer?.cancel();
        state = state.copyWith(
          isPlaying: playerState.playing && !isCompleted,
          isDownloading: isBuffering,
          duration: nextDuration > 0 ? nextDuration : state.duration,
          currentPosition: cappedPosition
              .clamp(
                0,
                nextDuration > 0 ? nextDuration : cappedPosition,
              )
              .toInt(),
          currentPositionMs: cappedPositionMs
              .clamp(
                0,
                nextDuration > 0 ? nextDurationMs : cappedPositionMs,
              )
              .toInt(),
        );
        if (playerState.playing && !isBuffering && !isCompleted) {
          unawaited(_activatePendingHistorySnapshotIfReady());
        }
        final shouldNotifyCompletion = isCompleted &&
            _streamLoopPolicy == StreamLoopPolicy.off &&
            !_streamTransitionInProgress &&
            (!_managedQueueActive || !streamPlayer.hasNext);

        if (shouldNotifyCompletion) {
          _notifyTrackCompletedIfNeeded();
        } else {
          _completedTrackIdNotified = null;
        }
      },
      onError: (Object error, StackTrace stackTrace) {
        _handleStreamPlaybackError(error);
      },
    );

    _streamPlaybackErrorSub = streamPlayer.playbackEventStream.listen(
      (_) {},
      onError: (Object error, StackTrace stackTrace) {
        _handleStreamPlaybackError(error);
      },
    );
  }

  void _handleStreamPlaybackError(Object error) {
    if (!_activeStream) return;
    final canRecover = state.isLooping || _desiredStreamPlaying;
    state = state.copyWith(
      isPlaying: false,
      isDownloading: canRecover,
      currentTrackName:
          canRecover ? state.currentTrackName : 'Stream failed: $error',
    );
    unawaited(_handleStreamFailure(error));
  }

  void _markStreamFailed(
    Object error, {
    int? currentPosition,
    int? currentPositionMs,
  }) {
    state = state.copyWith(
      isPlaying: false,
      isDownloading: false,
      currentPosition: currentPosition,
      currentPositionMs: currentPositionMs,
      currentTrackName: 'Stream failed: $error',
    );
  }

  Future<T> _runStreamCommand<T>(Future<T> Function() action) {
    final completer = Completer<T>();
    _streamCommandQueue = _streamCommandQueue
        .catchError((Object _, StackTrace __) {})
        .then((_) async {
      try {
        completer.complete(await action());
      } catch (error, stackTrace) {
        completer.completeError(error, stackTrace);
      }
    });
    return completer.future;
  }

  StreamPlaybackMode get _streamPlaybackMode =>
      streamPlaybackModeFor(managedQueueActive: _managedQueueActive);

  StreamLoopPolicy get _streamLoopPolicy =>
      streamLoopPolicyFor(isLooping: state.isLooping);

  LoopMode _streamLoopModeForState(bool isLooping) => justAudioLoopModeFor(
        playbackMode: _streamPlaybackMode,
        isLooping: isLooping,
      );

  LoopMode get _streamLoopMode => _streamLoopModeForState(state.isLooping);

  Future<void> _stopManagedQueuePrefetchPlayers() async {
    for (var i = 0; i < _prefetchedTrackIds.length; i++) {
      _prefetchedTrackIds[i] = null;
    }
    await Future.wait(
      _prefetchPlayers.map((player) async {
        try {
          await player.stop();
        } catch (_) {}
      }),
    );
  }

  void _updateSleepTimerState() {
    if (_sleepTimerEndsAt == null) {
      if (state.sleepTimerRemainingSeconds != 0) {
        state = state.copyWith(sleepTimerRemainingSeconds: 0);
      }
      return;
    }
    final remaining = sleepTimerRemainingSeconds(_sleepTimerEndsAt);
    if (remaining != state.sleepTimerRemainingSeconds) {
      state = state.copyWith(sleepTimerRemainingSeconds: remaining);
    }
    if (sleepTimerHasExpired(_sleepTimerEndsAt)) {
      _sleepTimer?.cancel();
      _sleepTimer = null;
      _sleepTimerEndsAt = null;
      unawaited(stopPlayback());
    }
  }

  Future<void> setSleepTimer(Duration? duration) async {
    _sleepTimer?.cancel();
    _sleepTimer = null;
    if (duration == null || duration <= Duration.zero) {
      _sleepTimerEndsAt = null;
      state = state.copyWith(sleepTimerRemainingSeconds: 0);
      return;
    }
    _sleepTimerEndsAt = DateTime.now().add(duration);
    _updateSleepTimerState();
    _sleepTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _updateSleepTimerState();
    });
  }

  Future<void> _setStreamSource(String videoId, ResolvedStreamSource source,
      {Duration? initialPosition}) async {
    _streamRuntime.markActivePlaybackVideoId(videoId);
    final nextSource = await _buildPlaybackSourceForVideoId(
      videoId,
      headers: source.headers,
      urlOverride: source.url,
    );

    _cachedStreamVideoId = videoId;
    await streamPlayer.stop();
    await streamPlayer.setAudioSource(
      nextSource,
      preload: true,
      initialPosition: initialPosition,
    );
  }

  int _managedQueueRecoveryIndex() {
    final activeTrackId = state.videoId?.trim();
    if (state.isLooping && activeTrackId != null && activeTrackId.isNotEmpty) {
      for (var index = 0; index < _managedQueueTracks.length; index++) {
        if (extractTrackId(_managedQueueTracks[index]) == activeTrackId) {
          return index;
        }
      }
    }
    final currentIndex = streamPlayer.currentIndex;
    if (_managedQueueActive &&
        currentIndex != null &&
        currentIndex >= 0 &&
        currentIndex < _managedQueueTracks.length) {
      return currentIndex;
    }
    if (activeTrackId != null && activeTrackId.isNotEmpty) {
      for (var index = 0; index < _managedQueueTracks.length; index++) {
        if (extractTrackId(_managedQueueTracks[index]) == activeTrackId) {
          return index;
        }
      }
    }
    return 0;
  }

  Future<void> _restoreManagedQueueAfterSeekFailure({
    required int boundedSeconds,
    required bool shouldResume,
  }) async {
    final preferredIndex = _managedQueueRecoveryIndex();
    final resolvedQueue = await _resolveManagedQueueSources(
      _managedQueueTracks,
      preferredInitialIndex: preferredIndex,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    final resolvedInitialIndex = (resolvedQueue['initialIndex'] as int?) ?? 0;
    if (playableTracks.isEmpty || sources.isEmpty) {
      throw StateError('managed_queue_unavailable');
    }

    _cachedStreamVideoId = extractTrackId(playableTracks[resolvedInitialIndex]);
    _streamRuntime.markActivePlaybackVideoId(_cachedStreamVideoId);
    _managedQueueActive = true;
    _managedQueueTracks = playableTracks;

    await _runStreamCommand(() async {
      if (!_activeStream) return;
      await streamPlayer.stop();
      await streamPlayer.setAudioSources(
        sources,
        initialIndex: resolvedInitialIndex,
        initialPosition: Duration(seconds: boundedSeconds),
        preload: true,
      );
      await streamPlayer.setLoopMode(_streamLoopMode);
    });

    if (!_activeStream) return;

    _broadcastManagedQueue();
    _applyManagedTrackMetadata(resolvedInitialIndex, resetPosition: false);
    if (shouldResume && !streamPlayer.playing) {
      await _resumeStreamPlayback();
    }
    final nextDuration = streamPlayer.duration?.inSeconds ?? state.duration;
    state = state.copyWith(
      isPlaying: shouldResume || streamPlayer.playing,
      isDownloading: false,
      duration: nextDuration > 0 ? nextDuration : state.duration,
      currentPosition: boundedSeconds,
      currentPositionMs: boundedSeconds * 1000,
      videoId: _cachedStreamVideoId,
    );
    unawaited(_prefetchManagedQueueAhead());
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> _setLocalSource(String path) async {
    _cachedStreamVideoId = null;
    _streamRuntime.markActivePlaybackVideoId(null);
    await streamPlayer.stop();
    await streamPlayer.setFilePath(path, preload: true);
  }

  Future<void> _seekActiveStreamPlayer(Duration position) async {
    final currentIndex = streamPlayer.currentIndex;
    if (_managedQueueActive &&
        currentIndex != null &&
        currentIndex >= 0 &&
        currentIndex < _managedQueueTracks.length) {
      await streamPlayer.seek(position, index: currentIndex);
      return;
    }
    await streamPlayer.seek(position);
  }

  Future<void> _resumeStreamPlayback({
    bool allowCompletedRecovery = true,
  }) async {
    _desiredStreamPlaying = true;
    if (streamPlayer.audioSource == null) {
      return;
    }
    try {
      await streamPlayer.setLoopMode(_streamLoopMode);
      if (streamPlayer.processingState == ProcessingState.completed) {
        try {
          await _seekActiveStreamPlayer(Duration.zero);
        } catch (_) {
          final prefersRecoveryPath =
              _managedQueueActive || _cachedStreamVideoId != null;
          if (allowCompletedRecovery && prefersRecoveryPath) {
            await _recoverStreamAfterSeekFailure(
              boundedSeconds: 0,
              shouldResume: true,
            );
            return;
          }
          rethrow;
        }
      }
      unawaited(
        streamPlayer.play().catchError((Object error, StackTrace stackTrace) {
          if (!_activeStream || !_desiredStreamPlaying) return;
          _markStreamFailed(error);
          unawaited(_handleStreamFailure(error));
        }),
      );
    } catch (error) {
      if (!_activeStream || !_desiredStreamPlaying) return;
      _markStreamFailed(error);
      unawaited(_handleStreamFailure(error));
    }
  }

  Future<void> _recoverStreamAfterSeekFailure({
    required int boundedSeconds,
    required bool shouldResume,
  }) async {
    if (!_activeStream || _streamSeekRecoveryInProgress) {
      return;
    }
    final videoId = state.videoId?.trim() ?? _cachedStreamVideoId?.trim();
    if (videoId == null || videoId.isEmpty) {
      throw StateError('stream_source_missing');
    }
    _streamSeekRecoveryInProgress = true;
    _streamTransitionInProgress = true;
    state = state.copyWith(isDownloading: true);
    try {
      if (_managedQueueActive && _managedQueueTracks.isNotEmpty) {
        await _restoreManagedQueueAfterSeekFailure(
          boundedSeconds: boundedSeconds,
          shouldResume: shouldResume,
        );
      } else {
        final source =
            _freshStreamSource(videoId) ?? await _resolveStreamSource(videoId);
        await _runStreamCommand(() async {
          if (!_activeStream) return;
          await _setStreamSource(
            videoId,
            source,
            initialPosition: Duration(seconds: boundedSeconds),
          );
          await streamPlayer.setLoopMode(_streamLoopMode);
        });
        if (!_activeStream) return;
        if (shouldResume && !streamPlayer.playing) {
          await _resumeStreamPlayback(
            allowCompletedRecovery: false,
          );
        }
        final nextDuration = streamPlayer.duration?.inSeconds ?? state.duration;
        state = state.copyWith(
          isPlaying: shouldResume || streamPlayer.playing,
          isDownloading: false,
          duration: nextDuration > 0 ? nextDuration : state.duration,
          currentPosition: boundedSeconds,
          currentPositionMs: boundedSeconds * 1000,
          videoId: videoId,
        );
      }
    } finally {
      _streamTransitionInProgress = false;
      _streamSeekRecoveryInProgress = false;
    }
  }

  @override
  void dispose() {
    _playbackTimer?.cancel();
    _sleepTimer?.cancel();
    _streamPositionSub?.cancel();
    _streamDurationSub?.cancel();
    _streamPlaybackSub?.cancel();
    _streamPlaybackErrorSub?.cancel();
    _streamCurrentIndexSub?.cancel();
    unawaited(stopPlayback(resetState: false));
    for (final player in _prefetchPlayers) {
      unawaited(player.dispose());
    }
    unawaited(streamPlayer.dispose());
    super.dispose();
  }

  Uri _proxyUri(String path) => buildProxyUri(path);

  ResolvedStreamSource? _freshStreamSource(String videoId) {
    return _streamRuntime.freshStreamSource(videoId);
  }

  ResolvedStreamSource _buildProxyStreamSource(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    return _streamRuntime.buildProxyStreamSource(
      videoId,
      fetchedAt: fetchedAt,
    );
  }

  void _registerUnavailableTrack(String? videoId, [String? reason]) {
    _streamRuntime.registerUnavailableTrack(videoId, reason);
  }

  bool _isKnownUnavailableRemoteTrack(Map<String, dynamic> track) {
    return _streamRuntime.isKnownUnavailableRemoteTrack(track);
  }

  Future<Map<String, dynamic>> _resolveManagedQueueSources(
    List<Map<String, dynamic>> tracks, {
    required int preferredInitialIndex,
  }) async {
    final resolution = await resolveManagedQueueSources(
      tracks: tracks,
      preferredInitialIndex: preferredInitialIndex,
      isKnownUnavailableRemoteTrack: _isKnownUnavailableRemoteTrack,
      buildManagedAudioSource: _buildManagedAudioSource,
      registerUnavailableTrack: _registerUnavailableTrack,
    );
    return {
      'tracks': resolution.tracks,
      'sources': resolution.sources,
      'initialIndex': resolution.initialIndex,
    };
  }

  Future<void> _handleStreamFailure(Object error) async {
    if (_streamFailureRecoveryInProgress || !_activeStream) return;
    if (_lastFailureRecoveryLoadVersion == _streamLoadVersion) {
      _markStreamFailed(error);
      return;
    }
    _lastFailureRecoveryLoadVersion = _streamLoadVersion;
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) {
      if (!_desiredStreamPlaying && !state.isLooping) {
        return;
      }
      _streamFailureRecoveryInProgress = true;
      try {
        final recoverySeconds = max(0, state.currentPositionMs ~/ 1000);
        await _recoverStreamAfterSeekFailure(
          boundedSeconds: recoverySeconds,
          shouldResume: _desiredStreamPlaying || state.isLooping,
        );
      } catch (recoveryError) {
        if (!_activeStream) return;
        _markStreamFailed(recoveryError);
      } finally {
        _streamFailureRecoveryInProgress = false;
      }
      return;
    }

    _streamFailureRecoveryInProgress = true;
    try {
      if (state.isLooping) {
        _desiredStreamPlaying = true;
        await _stopManagedQueuePrefetchPlayers();
        await _restoreManagedQueueAfterSeekFailure(
          boundedSeconds: max(0, state.currentPositionMs ~/ 1000),
          shouldResume: true,
        );
        return;
      }

      final failedIndex = streamPlayer.currentIndex ?? 0;
      if (failedIndex < 0 || failedIndex >= _managedQueueTracks.length) {
        return;
      }

      final failedTrack = _managedQueueTracks[failedIndex];
      final failedVideoId = extractTrackId(failedTrack) ?? state.videoId;
      _registerUnavailableTrack(failedVideoId, error.toString());

      final remainingTracks = _managedQueueTracks
          .where((track) => !_isKnownUnavailableRemoteTrack(track))
          .toList(growable: false);

      if (remainingTracks.isEmpty) {
        await stopPlayback(resetState: false);
        state = state.copyWith(
          isPlaying: false,
          isDownloading: false,
          currentTrackName: 'Track unavailable right now',
          videoId: failedVideoId,
        );
        _managedQueueActive = false;
        _managedQueueTracks = const [];
        _broadcastManagedQueue();
        return;
      }

      final nextIndex =
          failedIndex.clamp(0, remainingTracks.length - 1).toInt();
      await configureManagedQueue(
        remainingTracks,
        initialIndex: nextIndex,
        autoplay: _desiredStreamPlaying,
      );
    } catch (recoveryError) {
      if (!_activeStream) return;
      _markStreamFailed(recoveryError);
    } finally {
      _streamFailureRecoveryInProgress = false;
    }
  }

  String? _trackArtist(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString();
  }

  Future<LockCachingAudioSource> _buildCachingSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) async {
    return _streamRuntime.buildCachingSourceForVideoId(
      videoId,
      headers: headers,
      urlOverride: urlOverride,
      tag: tag,
    );
  }

  Future<AudioSource> _buildPlaybackSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) {
    return _streamRuntime.buildPlaybackSourceForVideoId(
      videoId,
      headers: headers,
      urlOverride: urlOverride,
      tag: tag,
    );
  }

  Future<void> _prepareQueueSession(
    Iterable<String?> videoIds, {
    String? currentVideoId,
    bool activeQueue = false,
    int lookahead = 8,
  }) async {
    await _streamRuntime.prepareQueueSession(
      videoIds,
      currentVideoId: currentVideoId,
      activeQueue: activeQueue,
      lookahead: lookahead,
      onFallbackPrewarm: (videoId) => _streamRuntime.prewarmStream(videoId),
    );
  }

  Future<void> prefetchFixedQueueTracks(
    List<Map<String, dynamic>> tracks, {
    required int currentIndex,
    int count = 2,
  }) async {
    await _streamRuntime.prefetchFixedQueueTracks(
      tracks,
      currentIndex: currentIndex,
      count: count,
    );
  }

  Future<void> _prefetchManagedQueueAhead({int lookahead = 2}) async {
    if (state.isLooping) {
      await _stopManagedQueuePrefetchPlayers();
      return;
    }
    await prefetchManagedQueueAhead(
      managedQueueActive: _managedQueueActive,
      managedQueueTracks: _managedQueueTracks,
      currentManagedQueueIndex: currentManagedQueueIndex,
      lookahead: lookahead,
      prefetchPlayers: _prefetchPlayers,
      prefetchedTrackIds: _prefetchedTrackIds,
      buildCachingSourceForVideoId: _buildCachingSourceForVideoId,
    );
  }

  Future<void> _refreshManagedQueueWarmup({int lookahead = 12}) async {
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) return;
    final currentIndex = currentManagedQueueIndex ?? 0;
    if (currentIndex < 0 || currentIndex >= _managedQueueTracks.length) return;
    if (state.isLooping) {
      await _stopManagedQueuePrefetchPlayers();
      if (!shouldWarmActiveTrackForLoop(
        playbackMode: _streamPlaybackMode,
        isLooping: state.isLooping,
      )) {
        return;
      }
    }
    final ids = <String?>[];
    for (var i = currentIndex; i < _managedQueueTracks.length; i++) {
      final track = _managedQueueTracks[i];
      if (i != currentIndex && isTrackHidden(track)) continue;
      ids.add(extractTrackId(track));
      if (ids.length >= lookahead) break;
    }
    await _prepareQueueSession(
      ids,
      currentVideoId: extractTrackId(_managedQueueTracks[currentIndex]),
      activeQueue: true,
      lookahead: lookahead,
    );
    unawaited(_prefetchManagedQueueAhead());
  }

  void _applyManagedTrackMetadata(
    int index, {
    bool resetPosition = false,
  }) {
    if (index < 0 || index >= _managedQueueTracks.length) return;
    final track = _managedQueueTracks[index];
    final videoId = extractTrackId(track);
    final hintedDuration = _parseInt(track['duration']);
    state = state.copyWith(
      currentTrackName: track['title']?.toString() ?? state.currentTrackName,
      thumbnail: track['thumbnail']?.toString() ?? state.thumbnail,
      artist: _trackArtist(track) ?? state.artist,
      videoId: videoId,
      duration: hintedDuration > 0 ? hintedDuration : state.duration,
      currentPosition: resetPosition ? 0 : state.currentPosition,
      currentPositionMs: resetPosition ? 0 : state.currentPositionMs,
    );
    _rememberTrackMeta(track, localPath: track['local_path']?.toString());
    _commitTrackToHistorySoon();
  }

  Future<AudioSource> _buildManagedAudioSource(
    Map<String, dynamic> track,
  ) async {
    final localPath = track['local_path']?.toString();
    if (localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync()) {
      return AudioSource.file(localPath, tag: track);
    }

    final videoId = extractTrackId(track);
    if (videoId == null || videoId.isEmpty) {
      throw Exception('Missing track id for managed queue source');
    }
    if (_streamRuntime.isUnavailableTrackId(videoId)) {
      final reason = _streamRuntime.unavailableReason(videoId);
      throw Exception(reason == null || reason.isEmpty
          ? 'Track unavailable for streaming'
          : reason);
    }

    return _buildPlaybackSourceForVideoId(videoId, tag: track);
  }

  Future<void> configureManagedQueue(
    List<Map<String, dynamic>> tracks, {
    required int initialIndex,
    bool autoplay = true,
  }) async {
    if (tracks.isEmpty || initialIndex < 0 || initialIndex >= tracks.length) {
      return;
    }

    final sourceTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final currentTrack = sourceTracks[initialIndex];
    final normalizedTracks = <Map<String, dynamic>>[
      currentTrack,
      ...sourceTracks.skip(initialIndex + 1),
      ...sourceTracks.take(initialIndex),
    ];
    final currentTrackId = extractTrackId(currentTrack);
    final remainingTracks =
        normalizedTracks.skip(1).where((track) => !isTrackHidden(track)).toList();

    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = autoplay;
    _activeStream = true;
    _managedQueueActive = true;
    _managedQueueTracks = normalizedTracks;
    _streamRuntime.markActivePlaybackVideoId(currentTrackId);
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();
    _broadcastManagedQueue();

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName:
          currentTrack['title']?.toString() ?? 'Connecting Queue...',
      thumbnail: currentTrack['thumbnail']?.toString(),
      artist: _trackArtist(currentTrack),
      clearVideoId: true,
      duration: 0,
      currentPosition: 0,
      currentPositionMs: 0,
    );

    await _prepareQueueSession(
      [currentTrackId],
      currentVideoId: currentTrackId,
      activeQueue: true,
      lookahead: 1,
    );
    if (loadVersion != _streamLoadVersion) return;

    final bootstrapTracks = <Map<String, dynamic>>[
      currentTrack,
      ...remainingTracks.take(8),
    ];
    var resolvedQueue = await _resolveManagedQueueSources(
      bootstrapTracks,
      preferredInitialIndex: 0,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    var sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    var resolvedInitialIndex = (resolvedQueue['initialIndex'] as int?) ?? 0;
    if ((playableTracks.isEmpty || sources.isEmpty) &&
        remainingTracks.length > 8) {
      resolvedQueue = await _resolveManagedQueueSources(
        remainingTracks.skip(8).take(12).toList(growable: false),
        preferredInitialIndex: 0,
      );
      playableTracks
        ..clear()
        ..addAll(List<Map<String, dynamic>>.from(
          resolvedQueue['tracks'] as List,
        ));
      sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
      resolvedInitialIndex = (resolvedQueue['initialIndex'] as int?) ?? 0;
    }
    if (playableTracks.isEmpty || sources.isEmpty) {
      _streamTransitionInProgress = false;
      _managedQueueActive = false;
      _managedQueueTracks = const [];
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'No playable tracks available',
        clearVideoId: true,
      );
      _broadcastManagedQueue();
      return;
    }
    if (loadVersion != _streamLoadVersion) return;

    _cachedStreamVideoId = extractTrackId(playableTracks[resolvedInitialIndex]);
    _managedQueueTracks = playableTracks;
    final resolvedBootstrapIds = playableTracks
        .map(extractTrackId)
        .whereType<String>()
        .where((id) => id.isNotEmpty)
        .toSet();

    await _runStreamCommand(() async {
      if (loadVersion != _streamLoadVersion) return;
      await streamPlayer.stop();
      await streamPlayer.setAudioSources(
        sources,
        initialIndex: resolvedInitialIndex,
        preload: true,
      );
      await streamPlayer.setLoopMode(_streamLoopMode);
    });
    if (loadVersion != _streamLoadVersion) return;

    _streamTransitionInProgress = false;
    _applyManagedTrackMetadata(resolvedInitialIndex, resetPosition: true);
    state = state.copyWith(
      isDownloading: false,
      duration: streamPlayer.duration?.inSeconds ?? state.duration,
    );
    unawaited(_prefetchManagedQueueAhead());
    unawaited(_refreshManagedQueueWarmup());
    if (_desiredStreamPlaying) {
      await _resumeStreamPlayback(allowCompletedRecovery: false);
    }
    final deferredRemainingTracks = remainingTracks
        .where((track) {
          final id = extractTrackId(track);
          return id == null || !resolvedBootstrapIds.contains(id);
        })
        .toList(growable: false);
    if (deferredRemainingTracks.isNotEmpty &&
        loadVersion == _streamLoadVersion) {
      unawaited(
        appendManagedQueueTracks(
          deferredRemainingTracks,
          expectedLoadVersion: loadVersion,
        ),
      );
    }
  }

  Future<void> appendManagedQueueTracks(
    List<Map<String, dynamic>> tracks, {
    int? expectedLoadVersion,
  }) async {
    final loadVersion = expectedLoadVersion ?? _streamLoadVersion;
    if (!_managedQueueActive ||
        tracks.isEmpty ||
        loadVersion != _streamLoadVersion) {
      return;
    }
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    await prewarmStreams(
      normalizedTracks.take(3).map(extractTrackId),
    );
    if (!_managedQueueActive || loadVersion != _streamLoadVersion) return;
    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: 0,
    );
    if (!_managedQueueActive || loadVersion != _streamLoadVersion) return;
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    if (playableTracks.isEmpty || sources.isEmpty) {
      return;
    }
    await _runStreamCommand(() async {
      if (!_managedQueueActive || loadVersion != _streamLoadVersion) return;
      await streamPlayer.addAudioSources(sources);
    });
    if (!_managedQueueActive || loadVersion != _streamLoadVersion) return;
    _managedQueueTracks = [..._managedQueueTracks, ...playableTracks];
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> insertManagedQueueTracks(
    int index,
    List<Map<String, dynamic>> tracks,
  ) async {
    if (!_managedQueueActive || tracks.isEmpty) return;
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final insertIndex = index.clamp(0, _managedQueueTracks.length).toInt();
    await prewarmStreams(
      normalizedTracks.take(3).map(extractTrackId),
    );
    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: 0,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    if (playableTracks.isEmpty || sources.isEmpty) {
      return;
    }
    await _runStreamCommand(() async {
      for (var offset = 0; offset < sources.length; offset++) {
        await streamPlayer.insertAudioSource(
            insertIndex + offset, sources[offset]);
      }
    });
    final updated = [..._managedQueueTracks];
    updated.insertAll(insertIndex, playableTracks);
    _managedQueueTracks = updated;
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> moveManagedQueueItem(int oldIndex, int newIndex) async {
    if (!_managedQueueActive) return;
    if (oldIndex < 0 ||
        oldIndex >= _managedQueueTracks.length ||
        newIndex < 0 ||
        newIndex > _managedQueueTracks.length) {
      return;
    }

    var targetIndex = newIndex;
    if (targetIndex > oldIndex) {
      targetIndex -= 1;
    }
    targetIndex = targetIndex.clamp(0, _managedQueueTracks.length - 1);
    if (oldIndex == targetIndex) return;

    await _runStreamCommand(() async {
      await streamPlayer.moveAudioSource(oldIndex, targetIndex);
    });

    final updated = [..._managedQueueTracks];
    updated.insert(targetIndex, updated.removeAt(oldIndex));
    _managedQueueTracks = updated;
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> removeManagedQueueItem(int index) async {
    if (!_managedQueueActive) return;
    if (index < 0 || index >= _managedQueueTracks.length) return;
    await _runStreamCommand(() async {
      await streamPlayer.removeAudioSourceAt(index);
    });
    final updated = [..._managedQueueTracks]..removeAt(index);
    _managedQueueTracks = updated;
    if (_managedQueueTracks.isEmpty) {
      _managedQueueActive = false;
    }
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> playManagedQueueIndex(int index) async {
    if (!_managedQueueActive) return;
    if (index < 0 || index >= _managedQueueTracks.length) return;
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seek(Duration.zero, index: index);
    });
    _streamTransitionInProgress = false;
    _applyManagedTrackMetadata(index, resetPosition: true);
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  Future<void> skipManagedQueueNext() async {
    if (!_managedQueueActive || !streamPlayer.hasNext) return;
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seekToNext();
    });
    _streamTransitionInProgress = false;
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  Future<void> skipManagedQueuePrevious() async {
    if (!_managedQueueActive) return;
    if (streamPlayer.position > const Duration(seconds: 3)) {
      await _runStreamCommand(() async {
        await streamPlayer.seek(Duration.zero);
      });
      return;
    }
    if (!streamPlayer.hasPrevious) {
      await _runStreamCommand(() async {
        await streamPlayer.seek(Duration.zero);
      });
      return;
    }
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seekToPrevious();
    });
    _streamTransitionInProgress = false;
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  bool get hasManagedQueue => _managedQueueActive;
  int? get currentManagedQueueIndex =>
      _managedQueueActive ? streamPlayer.currentIndex : null;

  Future<ResolvedStreamSource> _fetchDirectStreamSource(String videoId) async {
    return _streamRuntime.fetchDirectStreamSource(videoId);
  }

  Future<ResolvedStreamSource> _resolveStreamSource(String videoId) async {
    return _streamRuntime.resolveStreamSource(videoId);
  }

  Future<void> prewarmStream(String? videoId) async {
    await _streamRuntime.prewarmStream(videoId);
  }

  Future<void> prewarmStreams(
    Iterable<String?> videoIds, {
    int lookahead = 18,
    bool immediatePlayback = false,
    String? currentVideoId,
  }) async {
    await _streamRuntime.prewarmStreams(
      videoIds,
      lookahead: lookahead,
      immediatePlayback: immediatePlayback,
      currentVideoId: currentVideoId,
    );
  }

  Future<void> prepareImmediatePlayback(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    await _streamRuntime.prewarmStream(
      videoId,
      immediatePlayback: true,
    );
  }

  Future<bool> loadLocalFile(String path, String trackName) async {
    return loadLocalWithMeta(path, {'title': trackName});
  }

  Future<bool> loadLocalWithMeta(String path, Map<String, dynamic> meta) async {
    final loadVersion = ++_streamLoadVersion;
    _pendingStreamSeekSeconds = null;
    _desiredStreamPlaying = false;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_rememberedTrackStore.hasValidDownloadedAudio(path)) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName: meta['title'] ?? 'Unknown Track',
      thumbnail: meta['thumbnail'],
      duration: _parseInt(meta['duration']),
      filesize: _parseInt(meta['filesize']),
      artist: meta['author'] ?? meta['artist'],
      videoId: meta['video_id'] ?? meta['id'],
      currentPosition: 0,
      currentPositionMs: 0,
    );

    try {
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setLocalSource(path);
      });
      if (loadVersion != _streamLoadVersion) return false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: meta['title'] ?? 'Unknown Track',
        thumbnail: meta['thumbnail'],
        duration:
            streamPlayer.duration?.inSeconds ?? _parseInt(meta['duration']),
        filesize: _parseInt(meta['filesize']),
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        currentPosition: 0,
        currentPositionMs: 0,
      );
      _rememberTrackMeta(meta, localPath: path);
      _commitTrackToHistorySoon();
      return true;
    } catch (error) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: $error',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }
  }

  Future<bool> downloadAndLoadYoutube(String videoId, String outPath) async {
    state = state.copyWith(
        isDownloading: true,
        currentTrackName: 'Initializing Proxy Connection...',
        clearThumbnail: true,
        clearArtist: true,
        duration: 0,
        filesize: 0,
        currentPosition: 0);

    try {
      final titleStr = outPath.split('/').last.replaceAll('.mp3', '');
      final meta = await _downloadYoutubeAsset(
        videoId: videoId,
        outPath: outPath,
        title: titleStr,
        onMetadata: (meta) {
          state = state.copyWith(
            currentTrackName: meta['title'] ?? 'Downloading...',
            thumbnail: meta['thumbnail'],
            duration: _parseInt(meta['duration']),
            filesize: _parseInt(meta['filesize']),
            artist: meta['author'],
          );
        },
      );

      final loaded = await loadLocalWithMeta(outPath, meta);
      if (!loaded) {
        throw Exception(
            'Downloaded file saved, but the player could not load it');
      }
      state = state.copyWith(isDownloading: false);
      await play();
      return true;
    } catch (e) {
      state = state.copyWith(
          isDownloading: false, currentTrackName: 'Download failed: $e');

      _deleteDownloadedAssetFiles(outPath);
      return false;
    }
  }

  Future<bool> downloadYoutubeBackground(
      String videoId, String outPath, String titleStr) async {
    try {
      await _downloadYoutubeAsset(
        videoId: videoId,
        outPath: outPath,
        title: titleStr,
      );
      return true;
    } catch (e) {
      _deleteDownloadedAssetFiles(outPath);
      return false;
    }
  }

  void _deleteDownloadedAssetFiles(String outPath) {
    try {
      final audioFile = File(outPath);
      if (audioFile.existsSync()) audioFile.deleteSync();
      final metadataFile = File(outPath.replaceAll('.mp3', '.json'));
      if (metadataFile.existsSync()) metadataFile.deleteSync();
    } catch (_) {}
  }

  Future<Map<String, dynamic>> _downloadYoutubeAsset({
    required String videoId,
    required String outPath,
    required String title,
    void Function(Map<String, dynamic> meta)? onMetadata,
  }) async {
    final res = await appHttpClient.post(
      _proxyUri('/download'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({"video_id": videoId, "title": title}),
    );

    if (res.statusCode != 200) {
      throw Exception("Proxy Refused Connection or Failed to DL: ${res.body}");
    }

    final meta = Map<String, dynamic>.from(jsonDecode(res.body) as Map);
    onMetadata?.call(meta);
    final req = http.Request('GET', _proxyUri('/stream/$videoId'));
    final streamRes = await appHttpClient.send(req);

    if (streamRes.statusCode != 200) {
      throw Exception("Stream Error: ${streamRes.statusCode}");
    }

    final file = File(outPath);
    final sink = file.openWrite();
    try {
      await for (final chunk in streamRes.stream) {
        sink.add(chunk);
      }
    } finally {
      await sink.close();
    }

    if (!_rememberedTrackStore.hasValidDownloadedAudio(outPath)) {
      throw Exception('Downloaded file is empty or corrupted');
    }

    final jsonPath = outPath.replaceAll('.mp3', '.json');
    meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';
    await File(jsonPath).writeAsString(jsonEncode(meta));
    unawaited(
      upsertCloudLibraryTrack({
        ...Map<String, dynamic>.from(meta),
        'id': videoId,
        'videoId': videoId,
      }),
    );
    return meta;
  }

  Future<void> streamYoutube(
    String videoId,
    Map<String, dynamic> fallbackMeta,
  ) async {
    final loadVersion = ++_streamLoadVersion;
    final totalStopwatch = Stopwatch()..start();
    _desiredStreamPlaying = true;
    if (_activeStream &&
        state.videoId == videoId &&
        _cachedStreamVideoId == videoId) {
      await seek(0);
      await play();
      return;
    }

    _pendingStreamSeekSeconds = null;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    audioEngine.pause();
    _playbackTimer?.cancel();

    state = state.copyWith(
        isDownloading: true,
        currentTrackName: fallbackMeta['title'] ?? 'Connecting Stream...',
        thumbnail: fallbackMeta['thumbnail'],
        artist: fallbackMeta['channel'] ?? fallbackMeta['author'],
        clearVideoId: true,
        duration: 0,
        currentPosition: 0);

    try {
      final cachedPrepared = _freshStreamSource(videoId);
      final prepareStopwatch = Stopwatch()..start();
      ResolvedStreamSource source =
          cachedPrepared ?? _buildProxyStreamSource(videoId);
      if (cachedPrepared == null) {
        try {
          await prepareImmediatePlayback(videoId).timeout(
            const Duration(milliseconds: 2400),
          );
          source = _freshStreamSource(videoId) ?? source;
        } catch (_) {
          unawaited(prewarmStream(videoId));
        }
      }
      final prepareMs = prepareStopwatch.elapsedMilliseconds;
      if (loadVersion != _streamLoadVersion) return;
      final attachStopwatch = Stopwatch()..start();
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setStreamSource(videoId, source);
      });
      if (loadVersion != _streamLoadVersion) return;
      final attachMs = attachStopwatch.elapsedMilliseconds;
      final dur = streamPlayer.duration?.inSeconds ??
          _parseInt(fallbackMeta['duration']);
      _streamTransitionInProgress = false;
      state = state.copyWith(
          currentTrackName: fallbackMeta['title'],
          duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
          isDownloading: false,
          videoId: videoId);
      _rememberTrackMeta({
        ...fallbackMeta,
        'id': videoId,
        'videoId': videoId,
      });
      _commitTrackToHistorySoon();
      if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
        await _resumeStreamPlayback();
      }
      debugProxyLog(
        'stream',
        '$videoId prepared=${cachedPrepared != null} '
            'prepareMs=$prepareMs attachMs=$attachMs totalMs=${totalStopwatch.elapsedMilliseconds}',
      );
    } catch (e) {
      if (loadVersion != _streamLoadVersion) return;
      try {
        final directSource = await _fetchDirectStreamSource(videoId);
        await _runStreamCommand(() async {
          if (loadVersion != _streamLoadVersion) return;
          await _setStreamSource(videoId, directSource);
        });
        if (loadVersion != _streamLoadVersion) return;
        final dur = streamPlayer.duration?.inSeconds ??
            _parseInt(fallbackMeta['duration']);
        _streamTransitionInProgress = false;
        state = state.copyWith(
            currentTrackName: fallbackMeta['title'],
            duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
            isDownloading: false,
            videoId: videoId);
        _rememberTrackMeta({
          ...fallbackMeta,
          'id': videoId,
          'videoId': videoId,
        });
        _commitTrackToHistorySoon();
        if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
          await _resumeStreamPlayback(allowCompletedRecovery: false);
        }
        debugProxyLog(
          'stream',
          '$videoId fell back to direct_url totalMs=${totalStopwatch.elapsedMilliseconds}',
        );
      } catch (fallbackError) {
        if (loadVersion != _streamLoadVersion) return;
        _streamTransitionInProgress = false;
        state = state.copyWith(
            isDownloading: false,
            currentTrackName: 'Stream failed: $fallbackError',
            clearVideoId: true);
      }
    }
  }

  void _startTimer() {
    _playbackTimer?.cancel();
    _playbackTimer = Timer.periodic(const Duration(milliseconds: 200), (timer) {
      if (_activeStream) {
        timer.cancel();
        return;
      }
      if (state.duration == 0 || state.currentPosition < state.duration) {
        final pos = _activeStream
            ? streamPlayer.position.inSeconds
            : audioEngine.getPosition() ~/ 1000;
        if (pos != state.currentPosition) {
          state = state.copyWith(
            currentPosition: pos,
            currentPositionMs: pos * 1000,
          );
          unawaited(_commitPendingHistoryIfEligible());
        }
      } else {
        if (state.isLooping) {
          if (_activeStream) {
            streamPlayer.seek(Duration.zero);
          }
          state = state.copyWith(currentPosition: 0, currentPositionMs: 0);
        } else {
          _notifyTrackCompletedIfNeeded();
          pause();
        }
      }
    });
  }

  Future<void> play() async {
    if (!_activeStream &&
        streamPlayer.audioSource == null &&
        _restorableTrackMeta != null) {
      final restored = await _restoreRememberedTrackForPlayback();
      if (restored) {
        if (_activeStream) {
          await _resumeStreamPlayback();
        } else {
          audioEngine.play();
          state = state.copyWith(isPlaying: true);
          _startTimer();
        }
      }
      return;
    }

    if (_activeStream) {
      await _resumeStreamPlayback();
      return;
    }

    audioEngine.play();
    state = state.copyWith(isPlaying: true);
    _startTimer();
  }

  Future<void> pause() async {
    if (_activeStream) {
      _desiredStreamPlaying = false;
      try {
        await streamPlayer.pause();
      } catch (_) {}
      state = state.copyWith(isPlaying: false, isDownloading: false);
      return;
    }

    audioEngine.pause();
    state = state.copyWith(isPlaying: false);
    _playbackTimer?.cancel();
  }

  Future<void> stopPlayback({bool resetState = true}) async {
    _desiredStreamPlaying = false;
    _pendingStreamSeekSeconds = null;
    _playbackTimer?.cancel();
    _sleepTimer?.cancel();
    _sleepTimer = null;
    _sleepTimerEndsAt = null;
    _completedTrackIdNotified = null;
    _streamTransitionInProgress = false;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    for (var i = 0; i < _prefetchedTrackIds.length; i++) {
      _prefetchedTrackIds[i] = null;
    }

    try {
      await streamPlayer.stop();
    } catch (_) {}
    for (final player in _prefetchPlayers) {
      try {
        await player.stop();
      } catch (_) {}
    }
    try {
      audioEngine.pause();
    } catch (_) {}

    _cachedStreamVideoId = null;
    _streamRuntime.markActivePlaybackVideoId(null);
    _activeStream = false;
    if (resetState) {
      state = PlayerState(isLooping: state.isLooping);
    } else {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentPosition: 0,
        currentPositionMs: 0,
        sleepTimerRemainingSeconds: 0,
      );
    }
    _broadcastStoppedState();
  }

  Future<void> seek(int seconds) async {
    final durationSeconds = streamPlayer.duration?.inSeconds ?? state.duration;
    final maxSeekSeconds = durationSeconds > 1 ? durationSeconds - 1 : 0;
    final boundedSeconds = durationSeconds > 0
        ? seconds.clamp(0, maxSeekSeconds).toInt()
        : (seconds < 0 ? 0 : seconds);
    if (_activeStream) {
      state = state.copyWith(
        currentPosition: boundedSeconds,
        currentPositionMs: boundedSeconds * 1000,
      );
      return _queueStreamSeek(boundedSeconds);
    }

    audioEngine.seek(boundedSeconds * 1000); // Send MS to C++
    state = state.copyWith(
      currentPosition: boundedSeconds,
      currentPositionMs: boundedSeconds * 1000,
    );
  }

  Future<void> _queueStreamSeek(int boundedSeconds) {
    _pendingStreamSeekSeconds = boundedSeconds;
    return _streamSeekDrainFuture ??= _drainStreamSeekQueue();
  }

  Future<void> _drainStreamSeekQueue() async {
    try {
      while (_activeStream && _pendingStreamSeekSeconds != null) {
        if (_streamTransitionInProgress || _streamSeekRecoveryInProgress) {
          await Future<void>.delayed(const Duration(milliseconds: 80));
          continue;
        }
        final boundedSeconds = _pendingStreamSeekSeconds!;
        _pendingStreamSeekSeconds = null;
        await _performStreamSeek(boundedSeconds);
      }
    } finally {
      _streamSeekDrainFuture = null;
      final pendingSeconds = _pendingStreamSeekSeconds;
      if (_activeStream && pendingSeconds != null) {
        unawaited(_queueStreamSeek(pendingSeconds));
      }
    }
  }

  Future<void> _performStreamSeek(int boundedSeconds) async {
    final previousPosition = state.currentPosition;
    final previousPositionMs = state.currentPositionMs;
    final shouldResume =
        _desiredStreamPlaying || streamPlayer.playing || state.isPlaying;
    try {
      try {
        await _runStreamCommand(() async {
          if (!_activeStream) return;
          if (streamPlayer.audioSource == null) {
            throw StateError('stream_source_missing');
          }
          await streamPlayer.setLoopMode(_streamLoopMode);
          await _seekActiveStreamPlayer(Duration(seconds: boundedSeconds))
              .timeout(const Duration(seconds: 4));
        });
      } catch (_) {
        // A newer seek supersedes this failure; recovering the stale position
        // would replace the source while the user is still scrubbing.
        if (_pendingStreamSeekSeconds != null) return;
        await _recoverStreamAfterSeekFailure(
          boundedSeconds: boundedSeconds,
          shouldResume: shouldResume,
        ).timeout(const Duration(seconds: 10));
      }
      if (_pendingStreamSeekSeconds != null) return;
      if (shouldResume && !streamPlayer.playing) {
        await _resumeStreamPlayback();
      }
      state = state.copyWith(
        isPlaying: shouldResume || streamPlayer.playing,
        isDownloading: false,
        currentPosition: boundedSeconds,
        currentPositionMs: boundedSeconds * 1000,
      );
    } catch (recoveryError) {
      if (_pendingStreamSeekSeconds != null) return;
      _markStreamFailed(
        recoveryError,
        currentPosition: previousPosition,
        currentPositionMs: previousPositionMs,
      );
      unawaited(_handleStreamFailure(recoveryError));
    }
  }

  Future<void> toggleLoop(int startMs, int endMs) async {
    final newState = !state.isLooping;
    if (_activeStream) {
      if (newState) {
        await _stopManagedQueuePrefetchPlayers();
      }
      await _runStreamCommand(() async {
        await streamPlayer.setLoopMode(_streamLoopModeForState(newState));
      });
    } else {
      audioEngine.setLoop(newState, startMs, endMs);
    }
    state = state.copyWith(isLooping: newState);
    final activeTrackId = state.videoId?.trim();
    if (newState && activeTrackId != null && activeTrackId.isNotEmpty) {
      unawaited(
        recordProxyInteractionEvent(
          'repeat',
          trackId: activeTrackId,
          rawTrack: {
            'id': activeTrackId,
            'title': state.currentTrackName,
            'artist': state.artist,
            'thumbnail': state.thumbnail,
            'duration': state.duration,
          },
        ),
      );
    }
    if (!newState) {
      unawaited(_refreshManagedQueueWarmup());
    }
  }
}

// Removed isolateDownload as Flutter now uses HTTP asyncio over the Dart event loop.

final audioPlayerProvider =
    StateNotifierProvider<AudioPlayerNotifier, PlayerState>((ref) {
  final audioE = ref.watch(audioEngineProvider);
  return AudioPlayerNotifier(audioE);
});
