// ignore_for_file: experimental_member_use

import 'dart:ui' as ui;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'dart:io';
import 'dart:convert';
import 'dart:async';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../ffi/audio_ffi.dart';
import 'auth_provider.dart';
import 'playlist_provider.dart';
import 'package:audio_service/audio_service.dart';
import 'package:just_audio/just_audio.dart';

late AudioHandler globalAudioHandler;
const String proxyBaseUrl =
    String.fromEnvironment('AURALIS_PROXY_URL', defaultValue: 'http://34.172.70.149');
final http.Client appHttpClient = http.Client();

const List<String> _sharedTasteKeywords = <String>[
  'rock',
  'metal',
  'jazz',
  'blues',
  'classical',
  'orchestra',
  'punk',
  'indie',
  'folk',
  'country',
  'ambient',
  'chill',
  'sad',
  'happy',
  'workout',
  'gym',
  'sleep',
  'focus',
  'lofi',
  'edm',
  'house',
  'techno',
  'trance',
  'disco',
  'pop',
  'rap',
  'hip hop',
  'rnb',
  'soul',
  'funk',
  'vibe',
  'mood',
  'party',
  'romantic',
  'rain',
  'night',
  'morning',
  'road trip',
  '80s',
  '90s',
  '2000s',
];

Uri buildProxyUri(String path) {
  final normalizedPath = path.startsWith('/') ? path : '/$path';
  return Uri.parse('$proxyBaseUrl$normalizedPath');
}

Future<void> initAudioService() async {
  globalAudioHandler = await AudioService.init(
    builder: () => AuralisAudioHandler(),
    config: const AudioServiceConfig(
      androidNotificationChannelId: 'com.danbeverley.ebb.channel.audio',
      androidNotificationChannelName: 'EBB Audio Playback',
      androidStopForegroundOnPause: false,
    ),
  );
}

class AuralisAudioHandler extends BaseAudioHandler
    with QueueHandler, SeekHandler {
  Function()? onPlay;
  Function()? onPause;
  Function(Duration)? onSeek;
  Future<void> Function()? onStop;
  Future<void> Function()? onSkipToNext;
  Future<void> Function()? onSkipToPrevious;

  @override
  Future<void> play() async => onPlay?.call();
  @override
  Future<void> pause() async => onPause?.call();
  @override
  Future<void> seek(Duration position) async => onSeek?.call(position);
  @override
  Future<void> stop() async => await onStop?.call();
  @override
  Future<void> skipToNext() async => await onSkipToNext?.call();
  @override
  Future<void> skipToPrevious() async => await onSkipToPrevious?.call();

  void broadcastState(PlaybackState state) {
    playbackState.add(state);
  }

  void broadcastMediaItem(MediaItem item) {
    mediaItem.add(item);
  }

  void broadcastQueue(List<MediaItem> items) {
    queue.add(items);
  }
}

int _parseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

Map<String, String> _parseHeaders(dynamic value) {
  if (value is! Map) return const {};
  final headers = <String, String>{};
  value.forEach((key, dynamic entryValue) {
    if (key == null || entryValue == null) return;
    headers[key.toString()] = entryValue.toString();
  });
  return headers;
}

String? extractTrackId(dynamic track) {
  if (track is! Map) return null;
  final value = track['id'] ?? track['videoId'] ?? track['video_id'];
  final id = value?.toString();
  if (id == null || id.isEmpty) return null;
  return id;
}

Map<String, dynamic> normalizeTrack(dynamic rawTrack) {
  final track = Map<String, dynamic>.from(rawTrack as Map);
  final id = extractTrackId(track);
  return {
    ...track,
    if (id != null) 'id': id,
    if (id != null && track['videoId'] == null) 'videoId': id,
    if (track['channel'] == null && track['author'] != null)
      'channel': track['author'],
    if (track['author'] == null && track['channel'] != null)
      'author': track['channel'],
    if (track['channel'] == null && track['artist'] != null)
      'channel': track['artist'],
    if (track['author'] == null && track['artist'] != null)
      'author': track['artist'],
    'duration': _parseInt(track['duration']),
  };
}

bool isTrackHidden(dynamic track) {
  if (track is! Map) return false;
  return track['isHidden'] == true;
}

Map<String, dynamic> copyTrackWithHidden(
  Map<String, dynamic> track,
  bool isHidden,
) {
  return {
    ...track,
    'isHidden': isHidden,
  };
}

Map<String, dynamic> _cloudTrackPayload(dynamic rawTrack) {
  final track = normalizeTrack(rawTrack);
  final trackId = extractTrackId(track);
  return {
    if (trackId != null) 'id': trackId,
    if (trackId != null) 'videoId': trackId,
    if (track['title'] != null) 'title': track['title'],
    if (track['author'] != null) 'author': track['author'],
    if (track['channel'] != null) 'channel': track['channel'],
    if (track['artist'] != null) 'artist': track['artist'],
    if (track['thumbnail'] != null) 'thumbnail': track['thumbnail'],
    'duration': _parseInt(track['duration']),
    if (track['album'] != null) 'album': track['album'],
    if (track['album_id'] != null) 'album_id': track['album_id'],
  };
}

Future<void> upsertCloudLibraryTrack(dynamic rawTrack) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null || userId == null || userId.isEmpty) return;
  final payload = _cloudTrackPayload(rawTrack);
  final trackId = extractTrackId(payload);
  if (trackId == null || trackId.isEmpty) return;
  try {
    await client.from('library_tracks').upsert(
      {
        'user_id': userId,
        'track_id': trackId,
        'track_data': payload,
        'updated_at': DateTime.now().toUtc().toIso8601String(),
      },
      onConflict: 'user_id,track_id',
    );
  } catch (error) {
    debugPrint('Cloud library sync failed: $error');
  }
}

Future<void> removeCloudLibraryTrack(String? trackId) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null ||
      userId == null ||
      userId.isEmpty ||
      trackId == null ||
      trackId.isEmpty) {
    return;
  }
  try {
    await client
        .from('library_tracks')
        .delete()
        .eq('user_id', userId)
        .eq('track_id', trackId);
  } catch (error) {
    debugPrint('Cloud library delete failed: $error');
  }
}

Future<void> recordCloudSearchEvent(
  String query, {
  required int resultCount,
}) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  final trimmedQuery = query.trim();
  if (client == null ||
      userId == null ||
      userId.isEmpty ||
      trimmedQuery.isEmpty) {
    return;
  }
  try {
    await client.from('search_events').insert({
      'user_id': userId,
      'query': trimmedQuery,
      'result_count': resultCount,
    });
  } catch (error) {
    debugPrint('Cloud search event write failed: $error');
  }
}

final StreamController<String> _recommendationSignalController =
    StreamController<String>.broadcast();

Stream<String> get recommendationSignalStream =>
    _recommendationSignalController.stream;

void notifyRecommendationSignal([String reason = '']) {
  if (_recommendationSignalController.isClosed) return;
  _recommendationSignalController.add(reason);
}

Future<List<String>> getRecentCloudSearchQueries({int limit = 8}) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null || userId == null || userId.isEmpty) {
    return const [];
  }

  try {
    final rows = await client
        .from('search_events')
        .select('query,result_count,created_at')
        .eq('user_id', userId)
        .order('created_at', ascending: false)
        .limit(limit * 4);
    if (rows.isEmpty) return const [];

    final weighted = <String, double>{};
    for (var index = 0; index < rows.length; index++) {
      final query = rows[index]['query']?.toString().trim();
      if (query == null || query.isEmpty) continue;
      final normalized = query.toLowerCase();
      final resultCount = _parseInt(rows[index]['result_count']);
      final recencyWeight = (rows.length - index).toDouble();
      final resultWeight = resultCount > 0 ? 1.0 : 0.35;
      weighted.update(
        normalized,
        (value) => value + recencyWeight + resultWeight,
        ifAbsent: () => recencyWeight + resultWeight,
      );
    }

    final ranked = weighted.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    return ranked.take(limit).map((entry) => entry.key).toList(growable: false);
  } catch (error) {
    debugPrint('Cloud search query lookup failed: $error');
    return const [];
  }
}

class ResolvedStreamSource {
  final String url;
  final Map<String, String> headers;
  final DateTime fetchedAt;

  const ResolvedStreamSource({
    required this.url,
    required this.headers,
    required this.fetchedAt,
  });

  bool get isFresh =>
      DateTime.now().difference(fetchedAt) < const Duration(minutes: 25);
}

class TrackLyricsLine {
  final int index;
  final String text;
  final int? startTimeMs;
  final int? endTimeMs;

  const TrackLyricsLine({
    required this.index,
    required this.text,
    this.startTimeMs,
    this.endTimeMs,
  });

  factory TrackLyricsLine.fromJson(Map<String, dynamic> json) {
    return TrackLyricsLine(
      index: _parseInt(json['index']),
      text: (json['text'] ?? '').toString(),
      startTimeMs: json['start_time_ms'] == null
          ? null
          : _parseInt(json['start_time_ms']),
      endTimeMs:
          json['end_time_ms'] == null ? null : _parseInt(json['end_time_ms']),
    );
  }
}

class TrackLyricsState {
  final bool isLoading;
  final bool hasLyrics;
  final bool hasTimestamps;
  final String? videoId;
  final String? source;
  final String? error;
  final List<TrackLyricsLine> lines;

  const TrackLyricsState({
    this.isLoading = false,
    this.hasLyrics = false,
    this.hasTimestamps = false,
    this.videoId,
    this.source,
    this.error,
    this.lines = const [],
  });
}

class HistoryManager {
  static final _seedController = StreamController<String?>.broadcast();
  static Stream<String?> get seedStream => _seedController.stream;

  static Future<File> get _file async {
    return getScopedDataFile('history.json');
  }

  static Future<List<String>> _readLocalHistory() async {
    final f = await _file;
    if (!f.existsSync()) return const [];
    try {
      final raw = (await f.readAsString()).trim();
      if (raw.isEmpty) return const [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return const [];
      return decoded
          .map((entry) => entry?.toString() ?? '')
          .where((entry) => entry.isNotEmpty)
          .toList(growable: false);
    } catch (error) {
      debugPrint('History cache parse failed, resetting local history: $error');
      try {
        await f.writeAsString('[]');
      } catch (_) {}
      return const [];
    }
  }

  static Future<void> _recordCloudPlayEvent(String videoId) async {
    final client = supabaseClientOrNull;
    final userId = currentAuthenticatedUserId;
    if (client == null || userId == null || userId.isEmpty) return;
    try {
      await client.from('play_events').insert({
        'user_id': userId,
        'track_id': videoId,
        'event_type': 'play',
      });
    } catch (e) {
      debugPrint("Cloud history write error: $e");
    }
  }

  static Future<void> addHistory(String? videoId) async {
    try {
      if (videoId == null || videoId.isEmpty) return;
      final f = await _file;
      List<String> history = await _readLocalHistory();
      history.remove(videoId);
      history.insert(0, videoId);
      if (history.length > 50) history = history.sublist(0, 50);
      await f.writeAsString(jsonEncode(history));
      _seedController.add(videoId);
      unawaited(_recordCloudPlayEvent(videoId));
    } catch (e) {
      debugPrint("History Write Error: $e");
    }
  }

  static Future<String?> getLatestSeed() async {
    try {
      final history = await _readLocalHistory();
      if (history.isNotEmpty) return history.first;

      final client = supabaseClientOrNull;
      final userId = currentAuthenticatedUserId;
      if (client != null && userId != null && userId.isNotEmpty) {
        final rows = await client
            .from('play_events')
            .select('track_id')
            .eq('user_id', userId)
            .eq('event_type', 'play')
            .order('created_at', ascending: false)
            .limit(1);
        if (rows.isNotEmpty) {
          return rows.first['track_id']?.toString();
        }
      }
    } catch (e) {
      debugPrint("History Read Error: $e");
    }
    return null;
  }

  static Future<String?> getRecommendationSeed() async {
    try {
      final client = supabaseClientOrNull;
      final userId = currentAuthenticatedUserId;
      if (client != null && userId != null && userId.isNotEmpty) {
        final rows = await client
            .from('play_events')
            .select('track_id')
            .eq('user_id', userId)
            .eq('event_type', 'play')
            .order('created_at', ascending: false)
            .limit(40);
        if (rows.isNotEmpty) {
          final scores = <String, double>{};
          for (var index = 0; index < rows.length; index++) {
            final trackId = rows[index]['track_id']?.toString();
            if (trackId == null || trackId.isEmpty) continue;
            final recencyWeight = (rows.length - index).toDouble();
            scores.update(
              trackId,
              (value) => value + recencyWeight,
              ifAbsent: () => recencyWeight,
            );
          }
          if (scores.isNotEmpty) {
            final ranked = scores.entries.toList()
              ..sort((a, b) => b.value.compareTo(a.value));
            return ranked.first.key;
          }
        }
      }
    } catch (error) {
      debugPrint('Recommendation seed lookup failed: $error');
    }
    return getLatestSeed();
  }

  static Future<List<String>> getRecentSeeds({int limit = 8}) async {
    final ordered = <String>[];
    final seen = <String>{};

    void addTrackId(String? trackId) {
      if (trackId == null || trackId.isEmpty || !seen.add(trackId)) return;
      ordered.add(trackId);
    }

    try {
      final history = await _readLocalHistory();
      for (final trackId in history) {
        addTrackId(trackId);
        if (ordered.length >= limit) {
          return ordered;
        }
      }

      final client = supabaseClientOrNull;
      final userId = currentAuthenticatedUserId;
      if (client != null && userId != null && userId.isNotEmpty) {
        final rows = await client
            .from('play_events')
            .select('track_id')
            .eq('user_id', userId)
            .eq('event_type', 'play')
            .order('created_at', ascending: false)
            .limit(limit * 3);
        for (final row in rows) {
          addTrackId(row['track_id']?.toString());
          if (ordered.length >= limit) {
            break;
          }
        }
      }
    } catch (error) {
      debugPrint('Recent seed lookup failed: $error');
    }

    return ordered;
  }
}

// Provides the singleton FFI audio engine
final audioEngineProvider = Provider<AudioEngineFFI>((ref) {
  final engine = AudioEngineFFI();
  if (engine.isAvailable) {
    engine.initEngine();
  }
  return engine;
});

class PlayerState {
  final bool isPlaying;
  final bool isLooping;
  final String currentTrackName;
  final bool isDownloading;
  final String? thumbnail;
  final int duration;
  final int filesize;
  final int currentPosition;
  final int currentPositionMs;
  final String? artist;
  final String? videoId;
  final int sleepTimerRemainingSeconds;

  PlayerState({
    this.isPlaying = false,
    this.isLooping = false,
    this.currentTrackName = 'No track loaded',
    this.isDownloading = false,
    this.thumbnail,
    this.duration = 0,
    this.filesize = 0,
    this.currentPosition = 0,
    this.currentPositionMs = 0,
    this.artist,
    this.videoId,
    this.sleepTimerRemainingSeconds = 0,
  });

  PlayerState copyWith(
      {bool? isPlaying,
      bool? isLooping,
      String? currentTrackName,
      bool? isDownloading,
      String? thumbnail,
      int? duration,
      int? filesize,
      int? currentPosition,
      int? currentPositionMs,
      String? artist,
      String? videoId,
      int? sleepTimerRemainingSeconds}) {
    final nextCurrentPosition = currentPosition ?? this.currentPosition;
    return PlayerState(
      isPlaying: isPlaying ?? this.isPlaying,
      isLooping: isLooping ?? this.isLooping,
      currentTrackName: currentTrackName ?? this.currentTrackName,
      isDownloading: isDownloading ?? this.isDownloading,
      thumbnail: thumbnail ?? this.thumbnail,
      duration: duration ?? this.duration,
      filesize: filesize ?? this.filesize,
      currentPosition: nextCurrentPosition,
      currentPositionMs: currentPositionMs ??
          (currentPosition != null
              ? currentPosition * 1000
              : this.currentPositionMs),
      artist: artist ?? this.artist,
      videoId: videoId ?? this.videoId,
      sleepTimerRemainingSeconds:
          sleepTimerRemainingSeconds ?? this.sleepTimerRemainingSeconds,
    );
  }
}

class AudioPlayerNotifier extends StateNotifier<PlayerState> {
  final AudioEngineFFI audioEngine;
  Future<void> Function()? onTrackCompleted;
  Future<void> Function(String? videoId)? onTrackChanged;
  Timer? _playbackTimer;
  final streamPlayer = AudioPlayer(useLazyPreparation: false);
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
  final Map<String, ResolvedStreamSource> _streamCache = {};
  final Map<String, Future<ResolvedStreamSource>> _pendingStreamLookups = {};
  StreamSubscription<Duration>? _streamPositionSub;
  StreamSubscription<Duration?>? _streamDurationSub;
  StreamSubscription<dynamic>? _streamPlaybackSub;
  StreamSubscription<int?>? _streamCurrentIndexSub;
  Future<void> _streamCommandQueue = Future<void>.value();
  LockCachingAudioSource? _cachedStreamAudioSource;
  String? _cachedStreamVideoId;
  int _streamLoadVersion = 0;
  bool _desiredStreamPlaying = false;
  String? _completedTrackIdNotified;
  bool _streamTransitionInProgress = false;
  bool _managedQueueActive = false;
  List<Map<String, dynamic>> _managedQueueTracks = const [];
  final List<String?> _prefetchedTrackIds = <String?>[null, null];
  final Set<String> _unavailableStreamTrackIds = <String>{};
  final Map<String, String> _unavailableStreamReasons = <String, String>{};
  int _latencySummaryProbeCounter = 0;
  final Map<String, DateTime> _recentPrepareBatches = <String, DateTime>{};
  final Map<String, Future<void>> _pendingPrepareBatches = <String, Future<void>>{};
  Timer? _sleepTimer;
  DateTime? _sleepTimerEndsAt;
  final Map<String, Future<void>> _fullPrefetchTasks = {};
  bool _streamFailureRecoveryInProgress = false;

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
      handler.onStop = () => stopPlayback(resetState: true);
    }
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
      unawaited(onTrackChanged?.call(value.videoId) ?? Future<void>.value());
    }
    
    if (shouldBroadcast) {
      _broadcastState();
    }
  }

  void _notifyTrackCompletedIfNeeded() {
    if (_streamTransitionInProgress) return;
    if (_managedQueueActive) {
      final currentIndex = streamPlayer.currentIndex ?? 0;
      if (currentIndex >= 0 && currentIndex < _managedQueueTracks.length - 1) {
        return;
      }
    }
    final videoId = state.videoId;
    if (videoId == null || videoId.isEmpty) return;
    if (_completedTrackIdNotified == videoId) return;
    _completedTrackIdNotified = videoId;
    unawaited(onTrackCompleted?.call() ?? Future<void>.value());
  }

  void _broadcastState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;

    handler.broadcastState(PlaybackState(
      controls: [
        MediaControl.skipToPrevious,
        if (state.isPlaying) MediaControl.pause else MediaControl.play,
        MediaControl.stop,
        MediaControl.skipToNext,
      ],
      systemActions: const {
        MediaAction.seek,
      },
      androidCompactActionIndices: const [0, 1, 3],
      processingState: state.isDownloading
          ? AudioProcessingState.buffering
          : AudioProcessingState.ready,
      playing: state.isPlaying,
      updatePosition: Duration(seconds: state.currentPosition),
      bufferedPosition: Duration(seconds: state.currentPosition),
    ));

    handler.broadcastMediaItem(_currentMediaItem());
  }

  void _broadcastStoppedState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    handler.broadcastState(
      PlaybackState(
        controls: [MediaControl.play],
        processingState: AudioProcessingState.idle,
        playing: false,
        updatePosition: Duration.zero,
        bufferedPosition: Duration.zero,
      ),
    );
    handler.broadcastQueue(const []);
  }

  MediaItem _currentMediaItem() {
    return MediaItem(
      id: state.videoId ?? 'current_track',
      album: state.artist ?? 'EBB',
      title: state.currentTrackName,
      artist: state.artist ?? 'Unknown Artist',
      duration: Duration(seconds: state.duration),
      artUri: state.thumbnail != null ? Uri.parse(state.thumbnail!) : null,
    );
  }

  MediaItem _mediaItemFromTrack(Map<String, dynamic> track) {
    final duration = _parseInt(track['duration']);
    final thumbnail = track['thumbnail']?.toString();
    return MediaItem(
      id: extractTrackId(track) ?? track['title']?.toString() ?? 'track',
      album: track['album']?.toString() ?? track['album_title']?.toString() ?? '',
      title: track['title']?.toString() ?? 'Unknown Track',
      artist: _trackArtist(track) ?? 'Unknown Artist',
      duration: duration > 0 ? Duration(seconds: duration) : null,
      artUri: thumbnail != null && thumbnail.isNotEmpty
          ? Uri.tryParse(thumbnail)
          : null,
    );
  }

  void _broadcastManagedQueue() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) {
      handler.broadcastQueue(const []);
      return;
    }
    handler.broadcastQueue(
      _managedQueueTracks.map(_mediaItemFromTrack).toList(growable: false),
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
      _applyManagedTrackMetadata(index, resetPosition: true);
      final currentTrackId = extractTrackId(_managedQueueTracks[index]);
      HistoryManager.addHistory(currentTrackId);
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
        final nextDuration =
            streamPlayer.duration?.inSeconds ?? state.duration;
        final nextDurationMs = nextDuration * 1000;
        final cappedPosition = isCompleted
            ? nextDuration
            : streamPlayer.position.inSeconds;
        final cappedPositionMs = isCompleted
            ? nextDurationMs
            : streamPlayer.position.inMilliseconds;

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
        final shouldNotifyCompletion = isCompleted &&
            !_streamTransitionInProgress &&
            (!_managedQueueActive || !streamPlayer.hasNext);

        if (shouldNotifyCompletion) {
          _notifyTrackCompletedIfNeeded();
        } else {
          _completedTrackIdNotified = null;
        }
      },
      onError: (Object error, StackTrace stackTrace) {
        if (!_activeStream) return;
        state = state.copyWith(
          isPlaying: false,
          isDownloading: false,
          currentTrackName: 'Stream failed: $error',
        );
        unawaited(_handleStreamFailure(error));
      },
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

  Future<void> _clearOldCachedStream(LockCachingAudioSource? oldSource) async {
    if (oldSource == null) return;
    try {
      final cacheFile = await oldSource.cacheFile;
      if (cacheFile.path.contains('${Platform.pathSeparator}stream_cache${Platform.pathSeparator}')) {
        return;
      }
      await oldSource.clearCache();
    } catch (_) {
      // Cache eviction should stay silent.
    }
  }

  void _updateSleepTimerState() {
    if (_sleepTimerEndsAt == null) {
      if (state.sleepTimerRemainingSeconds != 0) {
        state = state.copyWith(sleepTimerRemainingSeconds: 0);
      }
      return;
    }
    final remaining =
        _sleepTimerEndsAt!.difference(DateTime.now()).inSeconds.clamp(0, 86400);
    if (remaining != state.sleepTimerRemainingSeconds) {
      state = state.copyWith(sleepTimerRemainingSeconds: remaining);
    }
    if (remaining <= 0) {
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

  Future<void> _setStreamSource(
    String videoId,
    ResolvedStreamSource source,
  ) async {
    final previousSource = _cachedStreamAudioSource;
    final nextSource = await _buildCachingSourceForVideoId(
      videoId,
      headers: source.headers,
      urlOverride: source.url,
    );

    _cachedStreamAudioSource = nextSource;
    _cachedStreamVideoId = videoId;
    await streamPlayer.stop();
    await streamPlayer.setAudioSource(nextSource, preload: true);
    if (!identical(previousSource, nextSource)) {
      unawaited(_clearOldCachedStream(previousSource));
    }
  }

  Future<void> _setLocalSource(String path) async {
    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = null;
    await streamPlayer.stop();
    await streamPlayer.setFilePath(path, preload: true);
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }
  }

  Future<void> _resumeStreamPlayback() async {
    _desiredStreamPlaying = true;
    if (streamPlayer.audioSource == null) {
      return;
    }
    try {
      if (streamPlayer.processingState == ProcessingState.completed) {
        await streamPlayer.seek(Duration.zero);
      }
      unawaited(
        streamPlayer.play().catchError((Object error, StackTrace stackTrace) {
          if (!_activeStream || !_desiredStreamPlaying) return;
          state = state.copyWith(
            isPlaying: false,
            isDownloading: false,
            currentTrackName: 'Stream failed: $error',
          );
          unawaited(_handleStreamFailure(error));
        }),
      );
    } catch (error) {
      if (!_activeStream || !_desiredStreamPlaying) return;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Stream failed: $error',
      );
      unawaited(_handleStreamFailure(error));
    }
  }

  @override
  void dispose() {
    _playbackTimer?.cancel();
    _sleepTimer?.cancel();
    _streamPositionSub?.cancel();
    _streamDurationSub?.cancel();
    _streamPlaybackSub?.cancel();
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
    final cached = _streamCache[videoId];
    if (cached != null && cached.isFresh) {
      return cached;
    }
    return null;
  }

  ResolvedStreamSource _buildProxyStreamSource(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    return ResolvedStreamSource(
      url: _proxyUri('/proxy_stream/$videoId').toString(),
      headers: const {},
      fetchedAt: fetchedAt ?? DateTime.now(),
    );
  }

  ResolvedStreamSource _markPreparedStream(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    final source = _buildProxyStreamSource(videoId, fetchedAt: fetchedAt);
    _streamCache[videoId] = source;
    return source;
  }

  bool _hasLocalFileForTrack(Map<String, dynamic> track) {
    final localPath = track['local_path']?.toString();
    return localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync();
  }

  void _registerUnavailableTrack(String? videoId, [String? reason]) {
    final normalizedId = videoId?.trim();
    if (normalizedId == null || normalizedId.isEmpty) return;
    _unavailableStreamTrackIds.add(normalizedId);
    if (reason != null && reason.trim().isNotEmpty) {
      _unavailableStreamReasons[normalizedId] = reason.trim();
    }
    _streamCache.remove(normalizedId);
  }

  bool _isKnownUnavailableRemoteTrack(Map<String, dynamic> track) {
    if (_hasLocalFileForTrack(track)) return false;
    final videoId = extractTrackId(track);
    return videoId != null && _unavailableStreamTrackIds.contains(videoId);
  }

  Future<Map<String, dynamic>> _resolveManagedQueueSources(
    List<Map<String, dynamic>> tracks, {
    required int preferredInitialIndex,
  }) async {
    final playableTracks = <Map<String, dynamic>>[];
    final sources = <AudioSource>[];
    var resolvedInitialIndex = preferredInitialIndex;

    for (var i = 0; i < tracks.length; i++) {
      final track = tracks[i];
      final videoId = extractTrackId(track);
      if (_isKnownUnavailableRemoteTrack(track)) {
        if (i < resolvedInitialIndex) {
          resolvedInitialIndex -= 1;
        }
        continue;
      }
      try {
        final source = await _buildManagedAudioSource(track);
        playableTracks.add(track);
        sources.add(source);
      } catch (error) {
        _registerUnavailableTrack(videoId, error.toString());
        if (i < resolvedInitialIndex) {
          resolvedInitialIndex -= 1;
        }
      }
    }

    if (playableTracks.isEmpty) {
      resolvedInitialIndex = 0;
    } else {
      resolvedInitialIndex =
          resolvedInitialIndex.clamp(0, playableTracks.length - 1).toInt();
    }

    return {
      'tracks': playableTracks,
      'sources': sources,
      'initialIndex': resolvedInitialIndex,
    };
  }

  Future<void> _handleStreamFailure(Object error) async {
    if (_streamFailureRecoveryInProgress || !_activeStream) return;
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) {
      return;
    }

    _streamFailureRecoveryInProgress = true;
    try {
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

      final nextIndex = failedIndex.clamp(0, remainingTracks.length - 1).toInt();
      await configureManagedQueue(
        remainingTracks,
        initialIndex: nextIndex,
        autoplay: _desiredStreamPlaying,
      );
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

  String _sanitizeCacheKey(String value) {
    return value.replaceAll(RegExp(r'[^A-Za-z0-9_\-]'), '_');
  }

  Future<Directory> _streamCacheDirectory() async {
    final userDir = await getScopedUserDirectory();
    final cacheDir = Directory('${userDir.path}/stream_cache');
    if (!cacheDir.existsSync()) {
      cacheDir.createSync(recursive: true);
    }
    return cacheDir;
  }

  Future<File> _cacheFileForVideoId(String videoId) async {
    final cacheDir = await _streamCacheDirectory();
    return File('${cacheDir.path}/${_sanitizeCacheKey(videoId)}.audio');
  }

  Future<File> _cacheMarkerForVideoId(String videoId) async {
    final cacheFile = await _cacheFileForVideoId(videoId);
    return File('${cacheFile.path}.done');
  }

  Future<LockCachingAudioSource> _buildCachingSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) async {
    final cacheFile = await _cacheFileForVideoId(videoId);
    return LockCachingAudioSource(
      Uri.parse(urlOverride ?? _proxyUri('/proxy_stream/$videoId').toString()),
      headers: headers == null || headers.isEmpty ? null : headers,
      cacheFile: cacheFile,
      tag: tag,
    );
  }

  Future<void> _logLatencySummaryIfDue() async {
    if (!kDebugMode) return;
    _latencySummaryProbeCounter++;
    if (_latencySummaryProbeCounter % 24 != 0) return;
    try {
      final response = await appHttpClient
          .get(_proxyUri('/latency_summary'))
          .timeout(const Duration(seconds: 8));
      if (response.statusCode != 200) return;
      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final summary = payload['summary'];
      if (summary is! Map) return;
      debugPrint(
        '[EBB] latency-summary '
        'resolveAvg=${summary['avg_resolve_ms']}ms '
        'chunkAvg=${summary['avg_chunk_ms']}ms '
        'serverAvg=${summary['avg_server_ms']}ms '
        'prefixAvg=${summary['avg_cached_prefix_bytes']} '
        'resolveHit=${summary['resolve_cache_hit_rate']} '
        'chunkHit=${summary['chunk_cache_hit_rate']}',
      );
    } catch (_) {
      // Summary logging is best-effort only.
    }
  }

  String _prepareBatchKey(
    Iterable<String> ids, {
    String? currentVideoId,
    required bool activeQueue,
    required int lookahead,
  }) {
    return [
      activeQueue ? '1' : '0',
      currentVideoId ?? '',
      '$lookahead',
      ids.join(','),
    ].join('|');
  }

  void _prunePrepareBatchTracking() {
    final now = DateTime.now();
    _recentPrepareBatches.removeWhere(
      (_, startedAt) => now.difference(startedAt) > const Duration(minutes: 2),
    );
    if (_recentPrepareBatches.length <= 120) return;
    final ordered = _recentPrepareBatches.entries.toList()
      ..sort((a, b) => a.value.compareTo(b.value));
    final overflow = ordered.length - 80;
    for (var i = 0; i < overflow; i++) {
      _recentPrepareBatches.remove(ordered[i].key);
    }
  }

  Future<void> _prepareQueueSession(
    Iterable<String?> videoIds, {
    String? currentVideoId,
    bool activeQueue = false,
    int lookahead = 8,
  }) async {
    final idsToWarm = <String>[];
    final seen = <String>{};
    for (final rawId in videoIds) {
      final videoId = rawId?.toString();
      if (videoId == null || videoId.isEmpty || !seen.add(videoId)) {
        continue;
      }
      idsToWarm.add(videoId);
      if (idsToWarm.length >= lookahead) {
        break;
      }
    }
    if (idsToWarm.isEmpty) return;

    final idsNeedingWarm = idsToWarm
        .where((videoId) => _freshStreamSource(videoId) == null)
        .toList(growable: false);
    if (idsNeedingWarm.isEmpty) return;

    final batchKey = _prepareBatchKey(
      idsNeedingWarm,
      currentVideoId: currentVideoId,
      activeQueue: activeQueue,
      lookahead: lookahead,
    );

    final pendingBatch = _pendingPrepareBatches[batchKey];
    if (pendingBatch != null) {
      await pendingBatch;
      return;
    }

    final lastPreparedAt = _recentPrepareBatches[batchKey];
    final cooldown = activeQueue
        ? const Duration(seconds: 6)
        : const Duration(seconds: 12);
    if (lastPreparedAt != null &&
        DateTime.now().difference(lastPreparedAt) < cooldown) {
      return;
    }

    Future<void> runPrepareTask() async {
      final res = await appHttpClient
          .post(
            _proxyUri('/prepare_session'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'video_ids': idsNeedingWarm,
              'current_video_id': currentVideoId,
              'active_queue': activeQueue,
              'lookahead': lookahead,
            }),
          )
          .timeout(const Duration(seconds: 18));

      if (res.statusCode != 200) {
        throw Exception('Prepared session failed: ${res.statusCode}');
      }

      final data = jsonDecode(res.body) as Map<String, dynamic>;
      final prepared = data['prepared'] ?? data['streams'];
      final failed = data['failed'];
      if (failed is Map) {
        failed.forEach((key, dynamic value) {
          final failureReason = value is Map
              ? value['message']?.toString() ?? value['code']?.toString()
              : value?.toString();
          _registerUnavailableTrack(key.toString(), failureReason);
        });
      }
      if (prepared is Map) {
        prepared.forEach((key, dynamic value) {
          _markPreparedStream(key.toString());
          if (value is Map) {
            debugPrint(
              '[EBB] prepared ${key.toString()} '
              'resolveMs=${value['resolve_ms']} '
              'chunkMs=${value['chunk_ms']} '
              'targetBytes=${value['target_chunk_bytes']} '
              'chunkBytes=${value['cached_prefix_bytes']}',
            );
          }
        });
      }
      if (activeQueue) {
        unawaited(_logLatencySummaryIfDue());
      }
      _recentPrepareBatches[batchKey] = DateTime.now();
      _prunePrepareBatchTracking();
    }

    final pendingTask = runPrepareTask();
    _pendingPrepareBatches[batchKey] = pendingTask;
    try {
      await pendingTask;
    } catch (_) {
      final fallbackCount =
          idsNeedingWarm.length < 4 ? idsNeedingWarm.length : 4;
      for (final id in idsNeedingWarm.take(fallbackCount)) {
        unawaited(prewarmStream(id));
      }
    } finally {
      _pendingPrepareBatches.remove(batchKey);
    }
  }

  Future<void> _cleanupFullPrefetchCache(Set<String> keepIds) async {
    final cacheDir = await _streamCacheDirectory();
    if (!cacheDir.existsSync()) return;
    final keepNames = keepIds.map(_sanitizeCacheKey).toSet();
    for (final entity in cacheDir.listSync(followLinks: false)) {
      if (entity is! File) continue;
      final name = entity.uri.pathSegments.isEmpty
          ? ''
          : entity.uri.pathSegments.last;
      if (name.isEmpty) continue;
      final baseName = name.endsWith('.done')
          ? name.substring(0, name.length - 5)
          : name.replaceAll('.audio', '');
      if (keepNames.contains(baseName)) continue;
      try {
        entity.deleteSync();
      } catch (_) {}
    }
  }

  Future<void> _prefetchTrackToTempStorage(String videoId) async {
    final existing = _fullPrefetchTasks[videoId];
    if (existing != null) {
      return existing;
    }

    final task = () async {
      final cacheFile = await _cacheFileForVideoId(videoId);
      final markerFile = await _cacheMarkerForVideoId(videoId);
      if (cacheFile.existsSync() &&
          cacheFile.lengthSync() > 100000 &&
          markerFile.existsSync()) {
        return;
      }

      final tempFile = File('${cacheFile.path}.part');
      if (tempFile.existsSync()) {
        try {
          tempFile.deleteSync();
        } catch (_) {}
      }

      try {
        final request =
            http.Request('GET', _proxyUri('/proxy_stream/$videoId'));
        final response = await appHttpClient.send(request);
        if (response.statusCode != 200) {
          throw Exception('Prefetch failed: ${response.statusCode}');
        }
        final sink = tempFile.openWrite();
        await for (final chunk in response.stream) {
          sink.add(chunk);
        }
        await sink.close();
        if (cacheFile.existsSync()) {
          try {
            cacheFile.deleteSync();
          } catch (_) {}
        }
        await tempFile.rename(cacheFile.path);
        await markerFile.writeAsString(DateTime.now().toIso8601String());
        debugPrint('[EBB] full-prefetched $videoId bytes=${cacheFile.lengthSync()}');
      } catch (error) {
        debugPrint('[EBB] full-prefetch failed for $videoId: $error');
        if (tempFile.existsSync()) {
          try {
            tempFile.deleteSync();
          } catch (_) {}
        }
      }
    }();

    _fullPrefetchTasks[videoId] = task;
    await task.whenComplete(() => _fullPrefetchTasks.remove(videoId));
  }

  Future<void> prefetchFixedQueueTracks(
    List<Map<String, dynamic>> tracks, {
    required int currentIndex,
    int count = 2,
  }) async {
    if (tracks.isEmpty || currentIndex < 0 || currentIndex >= tracks.length) {
      return;
    }

    final keepIds = <String>{};
    final currentTrackId = extractTrackId(tracks[currentIndex]);
    if (currentTrackId != null && currentTrackId.isNotEmpty) {
      keepIds.add(currentTrackId);
    }

    final targetIds = <String>[];
    for (var i = currentIndex + 1; i < tracks.length; i++) {
      final track = tracks[i];
      if (isTrackHidden(track)) continue;
      final id = extractTrackId(track);
      if (id == null || id.isEmpty) continue;
      targetIds.add(id);
      keepIds.add(id);
      if (targetIds.length >= count) break;
    }

    await _cleanupFullPrefetchCache(keepIds);
    for (final id in targetIds) {
      unawaited(_prefetchTrackToTempStorage(id));
    }
  }

  Future<void> _prefetchManagedQueueAhead({int lookahead = 2}) async {
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) return;
    final currentIndex = currentManagedQueueIndex ?? 0;
    final targets = <Map<String, dynamic>>[];
    for (var i = currentIndex + 1; i < _managedQueueTracks.length; i++) {
      final track = _managedQueueTracks[i];
      if (isTrackHidden(track)) continue;
      targets.add(track);
      if (targets.length >= lookahead) break;
    }

    for (var slot = 0; slot < _prefetchPlayers.length; slot++) {
      final track = slot < targets.length ? targets[slot] : null;
      final trackId = track == null ? null : extractTrackId(track);
      if (_prefetchedTrackIds[slot] == trackId) {
        continue;
      }

      if (track == null || trackId == null || trackId.isEmpty) {
        _prefetchedTrackIds[slot] = null;
        try {
          await _prefetchPlayers[slot].stop();
        } catch (_) {}
        continue;
      }

      final localPath = track['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          File(localPath).existsSync()) {
        _prefetchedTrackIds[slot] = trackId;
        continue;
      }

      try {
        final source = await _buildCachingSourceForVideoId(trackId, tag: track);
        _prefetchedTrackIds[slot] = trackId;
        await _prefetchPlayers[slot].stop();
        await _prefetchPlayers[slot].setAudioSource(source, preload: true);
      } catch (_) {
        _prefetchedTrackIds[slot] = null;
      }
    }
  }

  Future<void> _refreshManagedQueueWarmup({int lookahead = 12}) async {
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) return;
    final currentIndex = currentManagedQueueIndex ?? 0;
    if (currentIndex < 0 || currentIndex >= _managedQueueTracks.length) return;
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
    if (_unavailableStreamTrackIds.contains(videoId)) {
      final reason = _unavailableStreamReasons[videoId];
      throw Exception(reason == null || reason.isEmpty
          ? 'Track unavailable for streaming'
          : reason);
    }

    return _buildCachingSourceForVideoId(videoId, tag: track);
  }

  Future<void> configureManagedQueue(
    List<Map<String, dynamic>> tracks, {
    required int initialIndex,
    bool autoplay = true,
  }) async {
    if (tracks.isEmpty || initialIndex < 0 || initialIndex >= tracks.length) {
      return;
    }

    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final currentTrack = normalizedTracks[initialIndex];
    final currentTrackId = extractTrackId(currentTrack);
    final lookaheadIds = <String?>[
      extractTrackId(currentTrack),
      ...normalizedTracks
          .skip(initialIndex + 1)
          .where((track) => !isTrackHidden(track))
          .take(8)
          .map(extractTrackId),
    ];

    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = autoplay;
    _activeStream = true;
    _managedQueueActive = true;
    _managedQueueTracks = normalizedTracks;
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
      videoId: null,
      duration: 0,
      currentPosition: 0,
      currentPositionMs: 0,
    );

    await _prepareQueueSession(
      lookaheadIds,
      currentVideoId: currentTrackId,
      activeQueue: true,
      lookahead: 8,
    );
    if (loadVersion != _streamLoadVersion) return;

    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: initialIndex,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    final resolvedInitialIndex = (resolvedQueue['initialIndex'] as int?) ?? 0;
    if (playableTracks.isEmpty || sources.isEmpty) {
      _streamTransitionInProgress = false;
      _managedQueueActive = false;
      _managedQueueTracks = const [];
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'No playable tracks available',
        videoId: null,
      );
      _broadcastManagedQueue();
      return;
    }
    if (loadVersion != _streamLoadVersion) return;

    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = extractTrackId(playableTracks[resolvedInitialIndex]);
    _managedQueueTracks = playableTracks;

    await _runStreamCommand(() async {
      if (loadVersion != _streamLoadVersion) return;
      await streamPlayer.stop();
      await streamPlayer.setAudioSources(
        sources,
        initialIndex: resolvedInitialIndex,
        preload: true,
      );
      await streamPlayer.setLoopMode(
        state.isLooping ? LoopMode.one : LoopMode.off,
      );
    });
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }
    if (loadVersion != _streamLoadVersion) return;

    _streamTransitionInProgress = false;
    _applyManagedTrackMetadata(resolvedInitialIndex, resetPosition: true);
    state = state.copyWith(
      isDownloading: false,
      duration: streamPlayer.duration?.inSeconds ?? state.duration,
    );
    HistoryManager.addHistory(extractTrackId(playableTracks[resolvedInitialIndex]));
    unawaited(_prefetchManagedQueueAhead());
    if (_desiredStreamPlaying) {
      await _resumeStreamPlayback();
    }
  }

  Future<void> appendManagedQueueTracks(
    List<Map<String, dynamic>> tracks,
  ) async {
    if (!_managedQueueActive || tracks.isEmpty) return;
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
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
      await streamPlayer.addAudioSources(sources);
    });
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
        await streamPlayer.insertAudioSource(insertIndex + offset, sources[offset]);
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
    HistoryManager.addHistory(extractTrackId(_managedQueueTracks[index]));
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

  Future<ResolvedStreamSource> _fetchStreamSource(String videoId) async {
    final cached = _freshStreamSource(videoId);
    if (cached != null) {
      return cached;
    }

    final stopwatch = Stopwatch()..start();
    final res = await appHttpClient
        .post(
          _proxyUri('/prepare_session'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'video_ids': [videoId],
          }),
        )
        .timeout(const Duration(seconds: 18));

    if (res.statusCode != 200) {
      throw Exception('Prepared stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final prepared = data['prepared'];
    final failed = data['failed'];
    if (failed is Map && failed.containsKey(videoId)) {
      final failure = failed[videoId];
      final failureReason = failure is Map
          ? failure['message']?.toString() ?? failure['code']?.toString()
          : failure?.toString();
      _registerUnavailableTrack(videoId, failureReason);
      throw Exception(failureReason ?? 'Track unavailable for streaming');
    }
    if (prepared is! Map || !prepared.containsKey(videoId)) {
      _registerUnavailableTrack(videoId, 'Track unavailable for streaming');
      throw Exception('Track unavailable for streaming');
    }
    final metrics = prepared[videoId];
    if (metrics is Map) {
      debugPrint(
        '[EBB] prepared $videoId '
        'resolveMs=${metrics['resolve_ms']} '
        'chunkMs=${metrics['chunk_ms']} '
        'chunkBytes=${metrics['cached_prefix_bytes']} '
        'serverMs=${data['server_ms']}',
      );
    }
    debugPrint(
      '[EBB] prepare_session[$videoId] totalMs=${stopwatch.elapsedMilliseconds}',
    );
    return _markPreparedStream(videoId);
  }

  Future<ResolvedStreamSource> _fetchDirectStreamSource(String videoId) async {
    final res = await appHttpClient
        .get(_proxyUri('/direct_url/$videoId'))
        .timeout(const Duration(seconds: 15));

    if (res.statusCode != 200) {
      throw Exception('Direct stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final directUrl = data['url']?.toString();
    if (directUrl == null || directUrl.isEmpty) {
      throw Exception('Proxy returned an empty direct stream URL');
    }

    return ResolvedStreamSource(
      url: directUrl,
      headers: _parseHeaders(data['headers']),
      fetchedAt: DateTime.now(),
    );
  }

  Future<ResolvedStreamSource> _resolveStreamSource(String videoId) {
    final cached = _freshStreamSource(videoId);
    if (cached != null) {
      return Future<ResolvedStreamSource>.value(cached);
    }

    final pending = _pendingStreamLookups[videoId];
    if (pending != null) {
      return pending;
    }

    final lookup = _fetchStreamSource(videoId);
    _pendingStreamLookups[videoId] = lookup;
    lookup.whenComplete(() {
      if (identical(_pendingStreamLookups[videoId], lookup)) {
        _pendingStreamLookups.remove(videoId);
      }
    });
    return lookup;
  }

  Future<void> prewarmStream(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    try {
      await _resolveStreamSource(videoId);
    } catch (_) {
      // Prewarm should stay silent. Playback will surface real errors later.
    }
  }

  Future<void> prewarmStreams(Iterable<String?> videoIds) async {
    await _prepareQueueSession(videoIds, lookahead: 18);
  }

  bool _hasValidDownloadedAudio(String path) {
    try {
      final file = File(path);
      return file.existsSync() && file.lengthSync() >= 10000;
    } catch (_) {
      return false;
    }
  }

  Future<bool> loadLocalFile(String path, String trackName) async {
    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = false;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName: trackName,
      thumbnail: null,
      artist: null,
      videoId: null,
      duration: 0,
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
        currentTrackName: trackName,
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: streamPlayer.duration?.inSeconds ?? 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return true;
    } catch (error) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: $error',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }
  }

  Future<bool> loadLocalWithMeta(String path, Map<String, dynamic> meta) async {
    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = false;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
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
        duration: streamPlayer.duration?.inSeconds ?? _parseInt(meta['duration']),
        filesize: _parseInt(meta['filesize']),
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        currentPosition: 0,
        currentPositionMs: 0,
      );
      HistoryManager.addHistory(meta['video_id'] ?? meta['id']);
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
        thumbnail: null,
        artist: null,
        duration: 0,
        filesize: 0,
        currentPosition: 0);
    // print("Initiating Proxy Request for: $videoId");

    try {
      // 1. Ask the Proxy Server to fetch the Video and extract metadata
      final titleStr = outPath.split('/').last.replaceAll('.mp3', '');
      final res = await appHttpClient.post(_proxyUri('/download'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId, "title": titleStr}));

      if (res.statusCode != 200) {
        throw Exception(
            "Proxy Refused Connection or Failed to DL: ${res.body}");
      }

      final meta = Map<String, dynamic>.from(jsonDecode(res.body) as Map);
      state = state.copyWith(
        currentTrackName: meta['title'] ?? 'Downloading...',
        thumbnail: meta['thumbnail'],
        duration: _parseInt(meta['duration']),
        filesize: _parseInt(meta['filesize']),
        artist: meta['author'],
      );
      HistoryManager.addHistory(videoId);

      // 2. Stream the byte data back from the proxy to the local flutter device
      final req = http.Request('GET', _proxyUri('/stream/$videoId'));
      final streamRes = await appHttpClient.send(req);

      if (streamRes.statusCode != 200) {
        throw Exception("Stream Error: ${streamRes.statusCode}");
      }

      final file = File(outPath);
      final sink = file.openWrite();

      await for (var chunk in streamRes.stream) {
        sink.add(chunk);
      }
      await sink.close();

      if (!_hasValidDownloadedAudio(outPath)) {
        throw Exception('Downloaded file is empty or corrupted');
      }

      final jsonPath = outPath.replaceAll('.mp3', '.json');
      meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';
      await File(jsonPath).writeAsString(jsonEncode(meta));
      unawaited(
        upsertCloudLibraryTrack({
          ...Map<String, dynamic>.from(meta as Map),
          'id': videoId,
          'videoId': videoId,
        }),
      );

      final loaded = await loadLocalWithMeta(outPath, meta);
      if (!loaded) {
        throw Exception('Downloaded file saved, but the player could not load it');
      }
      state = state.copyWith(isDownloading: false);
      await play();
      return true;
    } catch (e) {
      state = state.copyWith(
          isDownloading: false, currentTrackName: 'Download failed: $e');

      try {
        final f = File(outPath);
        if (f.existsSync()) f.deleteSync();
        final jf = File(outPath.replaceAll('.mp3', '.json'));
        if (jf.existsSync()) jf.deleteSync();
      } catch (_) {}
      return false;
    }
  }

  Future<bool> downloadYoutubeBackground(
      String videoId, String outPath, String titleStr) async {
    try {
      final res = await appHttpClient.post(_proxyUri('/download'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId, "title": titleStr}));

      if (res.statusCode != 200) {
        throw Exception("Proxy Refused Connection or Failed to DL");
      }

      final meta = Map<String, dynamic>.from(jsonDecode(res.body) as Map);
      HistoryManager.addHistory(videoId);

      final req = http.Request('GET', _proxyUri('/stream/$videoId'));
      final streamRes = await appHttpClient.send(req);

      if (streamRes.statusCode != 200) {
        throw Exception("Stream Error: ${streamRes.statusCode}");
      }

      final file = File(outPath);
      final sink = file.openWrite();
      
      await for (var chunk in streamRes.stream) {
        sink.add(chunk);
      }
      await sink.close();

      if (!_hasValidDownloadedAudio(outPath)) {
        throw Exception('Downloaded file is empty or corrupted');
      }

      final jsonPath = outPath.replaceAll('.mp3', '.json');
      meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';
      await File(jsonPath).writeAsString(jsonEncode(meta));
      unawaited(
        upsertCloudLibraryTrack({
          ...Map<String, dynamic>.from(meta as Map),
          'id': videoId,
          'videoId': videoId,
        }),
      );
      return true;
    } catch (e) {
      try {
        final f = File(outPath);
        if (f.existsSync()) f.deleteSync();
        final jf = File(outPath.replaceAll('.mp3', '.json'));
        if (jf.existsSync()) jf.deleteSync();
      } catch (_) {}
      return false;
    }
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
        videoId: null,
        duration: 0,
        currentPosition: 0);

    try {
      final cachedPrepared = _freshStreamSource(videoId);
      final prepareStopwatch = Stopwatch()..start();
      final source = cachedPrepared ?? await _resolveStreamSource(videoId);
      final prepareMs = prepareStopwatch.elapsedMilliseconds;
      if (loadVersion != _streamLoadVersion) return;
      final attachStopwatch = Stopwatch()..start();
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setStreamSource(videoId, source);
      });
      if (loadVersion != _streamLoadVersion) return;
      final attachMs = attachStopwatch.elapsedMilliseconds;
      final dur =
          streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);
      _streamTransitionInProgress = false;
      state = state.copyWith(
          currentTrackName: fallbackMeta['title'],
          duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
          isDownloading: false,
          videoId: videoId);
          
      HistoryManager.addHistory(videoId);
      if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
        await _resumeStreamPlayback();
      }
      debugPrint(
        '[EBB] stream $videoId prepared=${cachedPrepared != null} '
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
        final dur =
            streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);
        _streamTransitionInProgress = false;
        state = state.copyWith(
            currentTrackName: fallbackMeta['title'],
            duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
            isDownloading: false,
            videoId: videoId);

        HistoryManager.addHistory(videoId);
        if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
          await _resumeStreamPlayback();
        }
        debugPrint(
          '[EBB] stream $videoId fell back to direct_url totalMs=${totalStopwatch.elapsedMilliseconds}',
        );
      } catch (fallbackError) {
        if (loadVersion != _streamLoadVersion) return;
        _streamTransitionInProgress = false;
        state = state.copyWith(
            isDownloading: false,
            currentTrackName: 'Stream failed: $fallbackError',
            videoId: null);
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
        final pos =
            _activeStream ? streamPlayer.position.inSeconds : audioEngine.getPosition() ~/ 1000;
        if (pos != state.currentPosition) {
          state = state.copyWith(
            currentPosition: pos,
            currentPositionMs: pos * 1000,
          );
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

    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = null;
    _activeStream = false;
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }

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
    final boundedSeconds = state.duration > 0
        ? seconds.clamp(0, state.duration).toInt()
        : (seconds < 0 ? 0 : seconds);
    if (_activeStream) {
      state = state.copyWith(
        currentPosition: boundedSeconds,
        currentPositionMs: boundedSeconds * 1000,
      );
      try {
        await streamPlayer.seek(Duration(seconds: boundedSeconds));
      } catch (_) {}
      return;
    }

    audioEngine.seek(boundedSeconds * 1000); // Send MS to C++
    state = state.copyWith(
      currentPosition: boundedSeconds,
      currentPositionMs: boundedSeconds * 1000,
    );
  }

  Future<void> toggleLoop(int startMs, int endMs) async {
    final newState = !state.isLooping;
    if (_activeStream) {
       await _runStreamCommand(() async {
         await streamPlayer.setLoopMode(newState ? LoopMode.one : LoopMode.off);
       });
    } else {
       audioEngine.setLoop(newState, startMs, endMs);
    }
    state = state.copyWith(isLooping: newState);
  }
}

// Removed isolateDownload as Flutter now uses HTTP asyncio over the Dart event loop.

final audioPlayerProvider =
    StateNotifierProvider<AudioPlayerNotifier, PlayerState>((ref) {
  final audioE = ref.watch(audioEngineProvider);
  return AudioPlayerNotifier(audioE);
});

// Search & Recommendation State Management
class SearchNotifier extends StateNotifier<List<dynamic>> {
  final Ref ref;
  int _requestVersion = 0;

  SearchNotifier(this.ref) : super([]);
  bool isLoading = false;

  void _primeSearchResults(List<dynamic> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final res = await appHttpClient
          .post(buildProxyUri('/search'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": normalizedQuery, "limit": 10}))
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        state = jsonDecode(res.body)['results'];
        _primeSearchResults(state);
        unawaited(
          recordCloudSearchEvent(
            normalizedQuery,
            resultCount: state.length,
          ),
        );
        notifyRecommendationSignal(normalizedQuery);
      } else {
        state = [];
      }
    } catch (e) {
      // print("Search failed: $e");
    } finally {
      final isLatestRequest = requestVersion == _requestVersion;
      if (isLatestRequest) {
        isLoading = false;
        state = [...state]; // trigger rebuild
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = [];
  }
}

final searchProvider =
    StateNotifierProvider<SearchNotifier, List<dynamic>>((ref) {
  return SearchNotifier(ref);
});

class RecommendationNotifier extends StateNotifier<List<dynamic>> {
  final Ref ref;
  final Random _random = Random();
  RecommendationNotifier(this.ref) : super([]);
  bool isLoading = true;
  bool isPaginating = false;
  bool _hasMorePages = true;
  int _requestVersion = 0;
  final Set<String> _prewarmedRecommendationIds = <String>{};

  bool get hasMorePages => _hasMorePages;

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<File> _cacheFile() => getScopedDataFile('recommendations_cache.json');

  Future<List<dynamic>> _loadCachedRecommendations() async {
    try {
      final file = await _cacheFile();
      if (!file.existsSync()) return const [];
      final decoded = jsonDecode(await file.readAsString());
      if (decoded is! List) return const [];
      return decoded
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .toList(growable: false);
    } catch (error) {
      debugPrint('Recommendation cache load failed: $error');
      return const [];
    }
  }

  Future<void> _saveCachedRecommendations(List<dynamic> tracks) async {
    try {
      final file = await _cacheFile();
      await file.writeAsString(jsonEncode(tracks.take(20).toList(growable: false)));
    } catch (error) {
      debugPrint('Recommendation cache save failed: $error');
    }
  }

  Future<void> bootstrap() async {
    final cached = await _loadCachedRecommendations();
    if (!mounted) return;
    if (cached.isNotEmpty) {
      _hasMorePages = true;
      isLoading = false;
      state = _prepareRecommendationResults(cached);
      _primeRecommendationResults(state);
      return;
    }

    final quickSeed = await HistoryManager.getLatestSeed();
    if (!mounted) return;
    if (quickSeed != null && quickSeed.isNotEmpty) {
      await loadQuickRecommendations(quickSeed);
      if (!mounted) return;
      if (state.isNotEmpty) {
        return;
      }
    }

    final fastDefault = await _fetchDefaultRecommendations();
    if (!mounted) return;
    if (fastDefault.isNotEmpty) {
      final nextState = _prepareRecommendationResults(fastDefault);
      _hasMorePages = true;
      isLoading = false;
      state = nextState;
      _primeRecommendationResults(nextState);
      unawaited(_saveCachedRecommendations(nextState));
      return;
    }

    await refreshFromSignals();
  }

  Future<void> refreshFromSignals({bool forceRefresh = false}) async {
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    await loadRecommendations(seed, forceRefresh);
  }

  @override
  void dispose() {
    _requestVersion++;
    super.dispose();
  }

  String _defaultTrendingQuery() {
    final locale = ui.PlatformDispatcher.instance.locale;
    final countryCode = locale.countryCode?.trim();
    if (countryCode != null && countryCode.isNotEmpty) {
      return 'Trending songs in $countryCode';
    }
    final languageCode = locale.languageCode.trim();
    if (languageCode.isNotEmpty) {
      return 'Trending $languageCode music';
    }
    return 'Trending hit songs';
  }

  List<dynamic> _prepareRecommendationResults(
    List<dynamic> source, {
    Set<String> avoidIds = const <String>{},
    bool forceRefresh = false,
  }) {
    final preferred = <dynamic>[];
    final fallback = <dynamic>[];
    final seen = <String>{};
    for (final track in source) {
      final id = (track['id'] ?? track['videoId'])?.toString();
      if (id != null && id.isNotEmpty && !seen.add(id)) {
        continue;
      }
      if (forceRefresh && id != null && avoidIds.contains(id)) {
        fallback.add(track);
      } else {
        preferred.add(track);
      }
    }
    final combined = [...preferred, ...fallback];
    if (forceRefresh && combined.length > 1) {
      combined.shuffle(_random);
    }
    return combined.take(15).toList(growable: false);
  }

  Future<List<dynamic>> _fetchDefaultRecommendations({
    bool forceRefresh = false,
  }) async {
    try {
      final res = await appHttpClient
          .post(
            buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'query': '',
              'limit': forceRefresh ? 16 : 12,
              'seed_ids': const [],
              'artist_hints': const [],
              'taste_queries': const [],
              'avoid_ids': const [],
            }),
          )
          .timeout(const Duration(seconds: 4));
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final recommendations =
            payload['recommendations'] as List<dynamic>? ?? const [];
        if (recommendations.isNotEmpty) {
          return forceRefresh
              ? ([...recommendations]..shuffle(_random))
              : recommendations;
        }
      }
    } catch (_) {
      // Fall back to the broader search bootstrap below.
    }

    final queries = <String>[_defaultTrendingQuery(), 'Trending hit songs'];
    final futures = queries.map((query) async {
      try {
        final res = await appHttpClient
            .post(
              buildProxyUri('/search'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": query, "limit": forceRefresh ? 14 : 10}),
            )
            .timeout(const Duration(seconds: 4));
        if (res.statusCode != 200) return const <dynamic>[];
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        return payload['results'] as List<dynamic>? ?? const <dynamic>[];
      } catch (_) {
        return const <dynamic>[];
      }
    }).toList(growable: false);

    final responses = await Future.wait(futures);
    final aggregated = <dynamic>[];
    final seen = <String>{};
    for (final results in responses) {
      for (final track in results) {
        final id = (track['id'] ?? track['videoId'])?.toString();
        if (id != null && id.isNotEmpty && !seen.add(id)) {
          continue;
        }
        aggregated.add(track);
      }
      if (!forceRefresh && aggregated.isNotEmpty) {
        return aggregated;
      }
    }
    if (forceRefresh && aggregated.isNotEmpty) {
      aggregated.shuffle(_random);
      return aggregated;
    }
    return const [];
  }

  void _primeRecommendationResults(List<dynamic> tracks) {
    final ids = <String>[];
    for (final track in tracks) {
      final id = (track['id'] ?? track['videoId'])?.toString();
      if (id == null || id.isEmpty || !_prewarmedRecommendationIds.add(id)) {
        continue;
      }
      ids.add(id);
      if (ids.length >= 6) {
        break;
      }
    }
    if (ids.isEmpty) return;
    if (_prewarmedRecommendationIds.length > 160) {
      _prewarmedRecommendationIds.removeAll(
        _prewarmedRecommendationIds.take(_prewarmedRecommendationIds.length - 80),
      );
    }
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(ids));
  }

  void _bumpTasteWeight(
    Map<String, double> weights,
    String? value,
    double weight,
  ) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    weights.update(normalized, (current) => current + weight,
        ifAbsent: () => weight);
  }

  String? _extractArtistHint(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString();
  }

  String? _extractAlbumHint(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['album']?.toString() ?? rawTrack['album_title']?.toString();
  }

  bool _looksLikeTasteQuery(String query) {
    final normalized = query.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    if (normalized.length > 38) return true;
    if (normalized.contains(' mix') ||
        normalized.contains(' playlist') ||
        normalized.contains(' songs') ||
        normalized.contains(' music')) {
      return true;
    }
    return _sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
  }

  Future<Map<String, dynamic>> _buildRecommendationRequestBody(
    String? seedId, {
    required int limit,
    int offset = 0,
    Set<String> avoidIds = const <String>{},
    bool forceRefresh = false,
  }) async {
    final seedIds = <String>[];
    void addSeed(String? id) {
      final normalized = id?.trim();
      if (normalized == null || normalized.isEmpty) return;
      if (!seedIds.contains(normalized)) {
        seedIds.add(normalized);
      }
    }

    addSeed(seedId);
    for (final recentSeed
        in await HistoryManager.getRecentSeeds(limit: forceRefresh ? 12 : 8)) {
      addSeed(recentSeed);
    }

    final artistWeights = <String, double>{};
    final queryWeights = <String, double>{};
    final playlists = ref.read(playlistProvider);
    final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];

    for (final playlist in playlists) {
      _bumpTasteWeight(queryWeights, playlist.name, 1.4);
      for (final track in playlist.tracks.take(18)) {
        _bumpTasteWeight(artistWeights, _extractArtistHint(track), 1.4);
        _bumpTasteWeight(queryWeights, _extractAlbumHint(track), 0.8);
      }
    }

    for (final track in libraryTracks.take(24)) {
      _bumpTasteWeight(artistWeights, _extractArtistHint(track), 1.2);
      _bumpTasteWeight(queryWeights, _extractAlbumHint(track), 0.7);
    }

    final recentQueries = await getRecentCloudSearchQueries(limit: 8);
    for (final query in recentQueries) {
      if (_looksLikeTasteQuery(query)) {
        _bumpTasteWeight(queryWeights, query, 1.35);
      }
    }

    final rankedArtists = artistWeights.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final rankedQueries = queryWeights.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));

    return {
      'query': '',
      'limit': limit,
      'offset': offset,
      if (seedIds.isNotEmpty) 'seed_id': seedIds.first,
      'seed_ids': seedIds.take(forceRefresh ? 6 : 5).toList(growable: false),
      'artist_hints':
          rankedArtists.take(6).map((entry) => entry.key).toList(growable: false),
      'taste_queries':
          rankedQueries.take(8).map((entry) => entry.key).toList(growable: false),
      'avoid_ids': avoidIds.take(40).toList(growable: false),
    };
  }

  Future<void> loadQuickRecommendations(String seedId) async {
    final requestVersion = ++_requestVersion;
    _hasMorePages = true;
    isLoading = true;
    state = [...state];
    try {
      final res = await appHttpClient
          .post(
            buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'query': '',
              'limit': 12,
              'seed_id': seedId,
              'seed_ids': [seedId],
              'artist_hints': const [],
              'taste_queries': const [],
              'avoid_ids': const [],
            }),
          )
          .timeout(const Duration(seconds: 5));
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final fetched = payload['recommendations'] as List<dynamic>;
      if (fetched.isEmpty) return;
      final nextState = _prepareRecommendationResults(fetched);
      _hasMorePages = payload['has_more'] == true;
      state = nextState;
      _primeRecommendationResults(nextState);
      unawaited(_saveCachedRecommendations(nextState));
    } catch (_) {
      // Quick bootstrap is best-effort only.
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  Future<void> loadRecommendations([String? seedId, bool forceRefresh = false]) async {
    final requestVersion = ++_requestVersion;
    final previousIds = forceRefresh
        ? state
            .map((track) => (track['id'] ?? track['videoId'])?.toString())
            .whereType<String>()
            .toSet()
        : const <String>{};
    isLoading = true;
    _hasMorePages = true;
    if (_isRequestCurrent(requestVersion)) {
      state = forceRefresh ? [...state] : [...state];
    }
    try {
      final body = await _buildRecommendationRequestBody(
        seedId,
        limit: 15,
        offset: 0,
        avoidIds: previousIds,
        forceRefresh: forceRefresh,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      final res = await appHttpClient
          .post(buildProxyUri('/recommend'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 10));
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final fetched = payload['recommendations'] as List<dynamic>;
        final nextState = _prepareRecommendationResults(
          fetched,
          avoidIds: previousIds,
          forceRefresh: forceRefresh,
        );
        _hasMorePages = payload['has_more'] == true;
        state = nextState;
        _primeRecommendationResults(state);
        unawaited(_saveCachedRecommendations(nextState));
        return;
      }

      if (state.isEmpty || forceRefresh) {
        final fallbackResults =
            await _fetchDefaultRecommendations(forceRefresh: forceRefresh);
        if (!_isRequestCurrent(requestVersion)) return;
        if (fallbackResults.isNotEmpty) {
          final nextState = _prepareRecommendationResults(
            fallbackResults,
            avoidIds: previousIds,
            forceRefresh: forceRefresh,
          );
          _hasMorePages = fallbackResults.length >= (forceRefresh ? 16 : 12);
          state = nextState;
          _primeRecommendationResults(state);
          unawaited(_saveCachedRecommendations(nextState));
          return;
        }
      }
    } catch (e) {
      if (state.isEmpty || forceRefresh) {
        final fallbackResults =
            await _fetchDefaultRecommendations(forceRefresh: forceRefresh);
        if (!_isRequestCurrent(requestVersion)) return;
        if (fallbackResults.isNotEmpty) {
          final nextState = _prepareRecommendationResults(
            fallbackResults,
            avoidIds: previousIds,
            forceRefresh: forceRefresh,
          );
          _hasMorePages = fallbackResults.length >= (forceRefresh ? 16 : 12);
          state = nextState;
          _primeRecommendationResults(state);
          unawaited(_saveCachedRecommendations(nextState));
        }
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  Future<void> loadMore(String seedId) async {
    if (isPaginating || !_hasMorePages) return;
    final requestVersion = ++_requestVersion;
    isPaginating = true;
    if (_isRequestCurrent(requestVersion)) {
      state = [...state];
    }
    try {
      final effectiveSeedId = await HistoryManager.getRecommendationSeed() ?? seedId;
      if (!_isRequestCurrent(requestVersion)) return;
      final existingIds = state
          .map(extractTrackId)
          .whereType<String>()
          .where((id) => id.isNotEmpty)
          .toSet();
      final body = await _buildRecommendationRequestBody(
        effectiveSeedId,
        limit: 10,
        offset: state.length,
        avoidIds: existingIds,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      final res = await appHttpClient.post(buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body));
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final newRecs = payload['recommendations'] as List<dynamic>;
        final uniqueNew = newRecs
            .whereType<Map>()
            .map((track) => normalizeTrack(track))
            .where((track) {
              final id = extractTrackId(track);
              return id != null && id.isNotEmpty && !existingIds.contains(id);
            })
            .toList(growable: false);
        _hasMorePages = payload['has_more'] == true;
        state = [...state, ...uniqueNew];
        _primeRecommendationResults(uniqueNew);
      }
    } catch (e) {
      // print("Load more failed: $e");
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isPaginating = false;
        state = [...state];
      }
    }
  }
}

final recommendationProvider =
    StateNotifierProvider<RecommendationNotifier, List<dynamic>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = RecommendationNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class SuggestNotifier extends StateNotifier<List<String>> {
  int _requestVersion = 0;

  SuggestNotifier() : super([]);

  Future<void> fetchSuggestions(String query) async {
    if (query.isEmpty) {
      state = [];
      return;
    }
    final requestVersion = ++_requestVersion;
    try {
      final res = await appHttpClient
          .post(buildProxyUri('/suggest'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": query, "limit": 5}))
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        state = List<String>.from(jsonDecode(res.body)['results']);
      }
    } catch (e) {
      // print("Suggest failed: $e");
    }
  }

  void clear() {
    _requestVersion++;
    state = [];
  }
}

final suggestProvider =
    StateNotifierProvider<SuggestNotifier, List<String>>((ref) {
  return SuggestNotifier();
});

class TrackDetailsNotifier extends StateNotifier<Map<String, dynamic>?> {
  final Ref ref;

  TrackDetailsNotifier(this.ref) : super(null);

  Future<void> fetchDetails(String videoId) async {
    state = null; // show loading
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(videoId));
    try {
      final res = await appHttpClient.post(
          buildProxyUri('/track_details'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId}));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body);
        final similarTracks = (state?['similar_tracks'] as List<dynamic>?) ?? const [];
        unawaited(
          ref.read(audioPlayerProvider.notifier).prewarmStreams([
                videoId,
                ...similarTracks
                    .take(8)
                    .map((track) => track['id'] ?? track['videoId']),
              ]),
        );
      }
    } catch (e) {
      // print("Track Details failed: $e");
    }
  }
}

final trackDetailsProvider =
    StateNotifierProvider<TrackDetailsNotifier, Map<String, dynamic>?>((ref) {
  return TrackDetailsNotifier(ref);
});

class AlbumSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  int _requestVersion = 0;

  AlbumSearchNotifier() : super(const []);
  bool isLoading = false;

  Future<void> search(String query) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final res = await appHttpClient
          .post(
            buildProxyUri('/search_albums'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({"query": query, "limit": 6}),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final albums = (payload['albums'] as List<dynamic>? ?? const []);
        state = albums
            .map((album) => Map<String, dynamic>.from(album as Map))
            .toList(growable: false);
      } else {
        state = const [];
      }
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final albumSearchProvider =
    StateNotifierProvider<AlbumSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return AlbumSearchNotifier();
});

class AlbumDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? album;
  final String? error;

  const AlbumDetailsState({
    this.isLoading = false,
    this.album,
    this.error,
  });
}

class AlbumDetailsNotifier extends StateNotifier<AlbumDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  AlbumDetailsNotifier(this.ref) : super(const AlbumDetailsState());

  Future<void> fetchAlbum(String? albumId) async {
    if (albumId == null || albumId.isEmpty) {
      state = const AlbumDetailsState(error: 'Album unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const AlbumDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/album/$albumId'))
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const AlbumDetailsState(error: 'Album unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final tracks = (payload['tracks'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              tracks.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
      state = AlbumDetailsState(album: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const AlbumDetailsState(error: 'Album unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const AlbumDetailsState();
  }
}

final albumDetailsProvider =
    StateNotifierProvider<AlbumDetailsNotifier, AlbumDetailsState>((ref) {
  return AlbumDetailsNotifier(ref);
});

class ArtistSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  int _requestVersion = 0;
  bool isLoading = false;

  ArtistSearchNotifier() : super(const []);

  Future<void> search(String query) async {
    final trimmed = query.trim();
    if (trimmed.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final res = await appHttpClient
          .post(
            buildProxyUri('/search_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({"query": trimmed, "limit": 8}),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
      } else {
        state = const [];
      }
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final artistSearchProvider =
    StateNotifierProvider<ArtistSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return ArtistSearchNotifier();
});

class RecommendedArtistsNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;

  RecommendedArtistsNotifier(this.ref) : super(const []);

  void _bumpWeight(Map<String, double> weights, String? value, double weight) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    weights.update(
      normalized,
      (current) => current + weight,
      ifAbsent: () => weight,
    );
  }

  String? _extractArtistHint(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString();
  }

  bool _looksLikeTasteQuery(String query) {
    final normalized = query.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    if (normalized.length > 38) return true;
    if (normalized.contains(' mix') ||
        normalized.contains(' playlist') ||
        normalized.contains(' songs') ||
        normalized.contains(' music')) {
      return true;
    }
    return _sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
  }

  Future<void> bootstrap() async {
    await loadRecommendedArtists();
  }

  Future<void> loadRecommendedArtists({
    List<String> seedArtistHints = const [],
    List<String> seedTasteQueries = const [],
  }) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final artistWeights = <String, double>{};
      final queryWeights = <String, double>{};
      final playlists = ref.read(playlistProvider);
      final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];

      for (final artist in seedArtistHints) {
        _bumpWeight(artistWeights, artist, 2.8);
      }
      for (final query in seedTasteQueries) {
        _bumpWeight(queryWeights, query, 2.0);
      }

      for (final playlist in playlists) {
        _bumpWeight(queryWeights, playlist.name, 1.2);
        for (final track in playlist.tracks.take(18)) {
          _bumpWeight(artistWeights, _extractArtistHint(track), 1.5);
        }
      }

      for (final track in libraryTracks.take(24)) {
        _bumpWeight(artistWeights, _extractArtistHint(track), 1.25);
      }

      final recentQueries = await getRecentCloudSearchQueries(limit: 8);
      for (final query in recentQueries) {
        if (_looksLikeTasteQuery(query)) {
          _bumpWeight(queryWeights, query, 1.1);
        }
      }

      final rankedArtists = artistWeights.entries.toList()
        ..sort((a, b) => b.value.compareTo(a.value));
      final rankedQueries = queryWeights.entries.toList()
        ..sort((a, b) => b.value.compareTo(a.value));

      final res = await appHttpClient
          .post(
            buildProxyUri('/recommended_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'query': '',
              'limit': 8,
              'artist_hints': rankedArtists
                  .take(8)
                  .map((entry) => entry.key)
                  .toList(growable: false),
              'taste_queries': rankedQueries
                  .take(6)
                  .map((entry) => entry.key)
                  .toList(growable: false),
            }),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
      } else {
        state = const [];
      }
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final recommendedArtistsProvider = StateNotifierProvider<
    RecommendedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = RecommendedArtistsNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class ArtistDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? artist;
  final String? error;

  const ArtistDetailsState({
    this.isLoading = false,
    this.artist,
    this.error,
  });
}

class ArtistDetailsNotifier extends StateNotifier<ArtistDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  ArtistDetailsNotifier(this.ref) : super(const ArtistDetailsState());

  Future<void> fetchArtist(String? artistId) async {
    if (artistId == null || artistId.isEmpty) {
      state = const ArtistDetailsState(error: 'Artist unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const ArtistDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/artist/$artistId'))
          .timeout(const Duration(seconds: 14));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const ArtistDetailsState(error: 'Artist unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final topSongs = (payload['top_songs'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              topSongs.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
      state = ArtistDetailsState(artist: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const ArtistDetailsState(error: 'Artist unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const ArtistDetailsState();
  }
}

final artistDetailsProvider =
    StateNotifierProvider<ArtistDetailsNotifier, ArtistDetailsState>((ref) {
  return ArtistDetailsNotifier(ref);
});

class LyricsNotifier extends StateNotifier<TrackLyricsState> {
  int _requestVersion = 0;

  LyricsNotifier() : super(const TrackLyricsState());

  Future<void> fetchLyrics(String? videoId) async {
    if (videoId == null || videoId.isEmpty) {
      clear();
      return;
    }
    if (state.videoId == videoId) {
      return;
    }

    final requestVersion = ++_requestVersion;
    state = TrackLyricsState(isLoading: true, videoId: videoId);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/lyrics/$videoId'))
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;

      if (res.statusCode != 200) {
        state = TrackLyricsState(
          videoId: videoId,
          error: 'Lyrics unavailable right now.',
        );
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rawLines = (payload['lines'] as List<dynamic>? ?? const []);
      final lines = rawLines
          .map((raw) =>
              TrackLyricsLine.fromJson(Map<String, dynamic>.from(raw as Map)))
          .where((line) => line.text.trim().isNotEmpty)
          .toList(growable: false);

      state = TrackLyricsState(
        videoId: videoId,
        hasLyrics: payload['has_lyrics'] == true && lines.isNotEmpty,
        hasTimestamps: payload['has_timestamps'] == true,
        source: payload['source']?.toString(),
        lines: lines,
      );
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = TrackLyricsState(
        videoId: videoId,
        error: 'Lyrics unavailable right now.',
      );
    }
  }

  void clear() {
    _requestVersion++;
    state = const TrackLyricsState();
  }
}

final lyricsProvider =
    StateNotifierProvider<LyricsNotifier, TrackLyricsState>((ref) {
  return LyricsNotifier();
});

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
      recommendations:
          clearRecommendations ? const [] : recommendations ?? this.recommendations,
      isLoadingQueue: isLoadingQueue ?? this.isLoadingQueue,
      isLoadingRecommendations:
          isLoadingRecommendations ?? this.isLoadingRecommendations,
    );
  }
}

class PlaybackQueueNotifier extends StateNotifier<PlaybackQueueState> {
  final Ref ref;
  final Random _random = Random();

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

  Future<List<Map<String, dynamic>>> _fetchRecommendations(
    String seedId, {
    int limit = 12,
  }) async {
    final body = await RecommendationNotifier(ref)._buildRecommendationRequestBody(
      seedId,
      limit: limit,
      avoidIds: state.queue
          .map((track) => extractTrackId(track))
          .whereType<String>()
          .toSet(),
    );
    final res = await appHttpClient
        .post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 12));

    if (res.statusCode != 200) {
      throw Exception('Recommendation lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks =
        payload['recommendations'] as List<dynamic>? ?? const <dynamic>[];
    return _uniqueTracks(rawTracks, excludedIds: {seedId});
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
    _primeUpcomingQueue();
    unawaited(_appendRadioRecommendations(seedId: videoId));
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
    final loaded = localTrack['thumbnail'] != null || extractTrackId(localTrack) != null
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
        final track =
            state.recommendations[_random.nextInt(state.recommendations.length)];
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
    final nextPlayedTrackIds = previousTrackId != null && previousTrackId != videoId
        ? _withPlayedTrack(previousTrackId)
        : state.playedTrackIds;

    final matchingIndex = state.queue
        .indexWhere((track) => extractTrackId(track) == videoId);
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

  Future<void> playNext() async {
    if (state.mode == PlaybackQueueMode.none) return;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);

    if (audioNotifier.hasManagedQueue) {
      final resolvedCurrentIndex = _resolvedCurrentIndex();
      final nextIndex = _nextPlayableIndex(resolvedCurrentIndex);
      if (nextIndex >= 0) {
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
      await playQueueIndex(nextIndex);
      return;
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      final refreshedNextIndex = _nextPlayableIndex(_resolvedCurrentIndex());
      if (refreshedNextIndex >= 0) {
        await playQueueIndex(refreshedNextIndex);
      }
    }
  }

  Future<void> playPrevious() async {
    if (state.mode == PlaybackQueueMode.none) return;
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (audioNotifier.hasManagedQueue) {
      await audioNotifier.skipManagedQueuePrevious();
      return;
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    final previousIndex = max(0, resolvedCurrentIndex - 1);
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
          ? _withPlayedTrack(previousTrackId)
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
      final replacementIndex =
          index.clamp(0, updatedQueue.length - 1).toInt();
      state = state.copyWith(
        queue: updatedQueue,
        currentIndex: replacementIndex,
        currentTrackId: extractTrackId(updatedQueue[replacementIndex]),
        playedTrackIds: state.playedTrackIds.where((id) => id != videoId).toList(),
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
      playedTrackIds: state.playedTrackIds.where((id) => id != videoId).toList(),
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
      playedTrackIds: state.playedTrackIds.where((id) => id != trackId).toList(),
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
        : updatedQueue.indexWhere((track) => extractTrackId(track) == activeTrackId);

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
          .where((track) =>
              !existingIds.contains(extractTrackId(track)))
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

    state = state.copyWith(
      isLoadingRecommendations: true,
      recommendations: append ? state.recommendations : const [],
    );
    try {
      final excludedIds = <String>{
        ...state.queue.map(extractTrackId).whereType<String>(),
        if (append)
          ...state.recommendations.map(extractTrackId).whereType<String>(),
      };
      final fetchedTracks = await _fetchRecommendations(seedId);
      final nextRecommendations = fetchedTracks
          .where((track) =>
              !excludedIds.contains(extractTrackId(track)))
          .toList(growable: false);
      state = state.copyWith(
        recommendations: append
            ? [...state.recommendations, ...nextRecommendations]
            : nextRecommendations,
      );
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

  Future<void> handleTrackCompleted() => notifier.playNext();
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

Future<List<Map<String, dynamic>>> _loadLocalLibraryTracks(String scopeId) async {
  final dir = await getScopedDownloadsDirectory(scopeId);
  final files = dir.listSync().where((f) => f.path.endsWith('.mp3')).toList()
    ..sort((a, b) {
      final aTime = a.statSync().modified;
      final bTime = b.statSync().modified;
      return bTime.compareTo(aTime);
    });

  final tracks = <Map<String, dynamic>>[];
  for (final file in files) {
    if (file is File) {
      try {
        if (file.lengthSync() < 10000) {
          file.deleteSync();
          final jsonPath = file.path.replaceAll('.mp3', '.json');
          if (File(jsonPath).existsSync()) File(jsonPath).deleteSync();
          continue;
        }
      } catch (_) {}
    }

    final jsonPath = file.path.replaceAll('.mp3', '.json');
    if (File(jsonPath).existsSync()) {
      final meta = Map<String, dynamic>.from(
        jsonDecode(File(jsonPath).readAsStringSync()) as Map,
      );
      meta['local_path'] = file.path;
      meta['is_downloaded_locally'] = true;
      tracks.add(normalizeTrack(meta));
    } else {
      tracks.add({
        'title': file.path.split('/').last.replaceAll('.mp3', ''),
        'local_path': file.path,
        'duration': 0,
        'is_downloaded_locally': true,
      });
    }
  }
  return tracks;
}

Future<List<Map<String, dynamic>>> _loadCloudLibraryTracks() async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null || userId == null || userId.isEmpty) {
    return const [];
  }

  try {
    final rows = await client
        .from('library_tracks')
        .select('track_id,track_data,added_at')
        .eq('user_id', userId)
        .order('added_at', ascending: false);
    return (rows as List<dynamic>)
        .map((rawRow) {
          final row = Map<String, dynamic>.from(rawRow as Map);
          final payload = normalizeTrack(
            Map<String, dynamic>.from(row['track_data'] as Map),
          );
          final trackId = row['track_id']?.toString();
          if (trackId != null && trackId.isNotEmpty) {
            payload['id'] = trackId;
            payload['videoId'] = trackId;
          }
          payload['is_cloud_saved'] = true;
          return payload;
        })
        .toList(growable: false);
  } catch (error) {
    debugPrint('Cloud library load failed: $error');
    return const [];
  }
}

final libraryProvider = FutureProvider<List<Map<String, dynamic>>>((ref) async {
  final authState = ref.watch(authProvider);
  final scopeId = authState.storageScopeId;
  ref.watch(storageRefreshTickProvider);

  final localTracks = await _loadLocalLibraryTracks(scopeId);
  if (!authState.isAuthenticated || !isSupabaseConfigured) {
    return localTracks;
  }

  final cloudTracks = await _loadCloudLibraryTracks();
  final localById = <String, Map<String, dynamic>>{};
  for (final track in localTracks) {
    final trackId = extractTrackId(track);
    if (trackId != null && trackId.isNotEmpty) {
      localById[trackId] = track;
    }
  }

  final merged = <Map<String, dynamic>>[];
  for (final cloudTrack in cloudTracks) {
    final trackId = extractTrackId(cloudTrack);
    final localTrack = trackId == null ? null : localById.remove(trackId);
    merged.add({
      ...cloudTrack,
      if (localTrack != null) ...localTrack,
      'is_cloud_saved': true,
      'is_downloaded_locally': localTrack != null,
    });
  }

  for (final localTrack in localById.values) {
    merged.add({
      ...localTrack,
      'is_cloud_saved': false,
      'is_downloaded_locally': true,
    });
    unawaited(upsertCloudLibraryTrack(localTrack));
  }

  return merged;
});
