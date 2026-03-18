// ignore_for_file: experimental_member_use

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';
import 'dart:convert';
import 'dart:async';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../ffi/audio_ffi.dart';
import 'playlist_provider.dart';
import 'package:audio_service/audio_service.dart';
import 'package:just_audio/just_audio.dart';

late AudioHandler globalAudioHandler;
const String proxyBaseUrl =
    String.fromEnvironment('AURALIS_PROXY_URL', defaultValue: 'http://34.172.70.149');
final http.Client appHttpClient = http.Client();

Uri buildProxyUri(String path) {
  final normalizedPath = path.startsWith('/') ? path : '/$path';
  return Uri.parse('$proxyBaseUrl$normalizedPath');
}

Future<void> initAudioService() async {
  globalAudioHandler = await AudioService.init(
    builder: () => AuralisAudioHandler(),
    config: const AudioServiceConfig(
      androidNotificationChannelId: 'com.example.app.channel.audio',
      androidNotificationChannelName: 'EBB Audio Playback',
      androidNotificationOngoing: true,
      androidStopForegroundOnPause: true,
    ),
  );
}

class AuralisAudioHandler extends BaseAudioHandler
    with QueueHandler, SeekHandler {
  Function()? onPlay;
  Function()? onPause;
  Function(Duration)? onSeek;
  Future<void> Function()? onStop;

  @override
  Future<void> play() async => onPlay?.call();
  @override
  Future<void> pause() async => onPause?.call();
  @override
  Future<void> seek(Duration position) async => onSeek?.call(position);
  @override
  Future<void> stop() async => await onStop?.call();

  void broadcastState(PlaybackState state) {
    playbackState.add(state);
  }

  void broadcastMediaItem(MediaItem item) {
    mediaItem.add(item);
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
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/history.json');
  }

  static Future<void> addHistory(String? videoId) async {
    try {
      if (videoId == null || videoId.isEmpty) return;
      final f = await _file;
      List<String> history = [];
      if (f.existsSync()) {
        history = List<String>.from(jsonDecode(await f.readAsString()));
      }
      history.remove(videoId);
      history.insert(0, videoId);
      if (history.length > 50) history = history.sublist(0, 50);
      await f.writeAsString(jsonEncode(history));
      _seedController.add(videoId);
    } catch (e) {
      debugPrint("History Write Error: $e");
    }
  }

  static Future<String?> getLatestSeed() async {
    try {
      final f = await _file;
      if (f.existsSync()) {
        List<String> history =
            List<String>.from(jsonDecode(await f.readAsString()));
        if (history.isNotEmpty) return history.first;
      }
    } catch (e) {
      debugPrint("History Read Error: $e");
    }
    return null;
  }
}

// Provides the singleton FFI audio engine
final audioEngineProvider = Provider<AudioEngineFFI>((ref) {
  final engine = AudioEngineFFI();
  engine.initEngine();
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
      String? videoId}) {
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
    );
  }
}

class AudioPlayerNotifier extends StateNotifier<PlayerState> {
  final AudioEngineFFI audioEngine;
  Future<void> Function()? onTrackCompleted;
  Future<void> Function(String? videoId)? onTrackChanged;
  Timer? _playbackTimer;
  final streamPlayer = AudioPlayer();
  bool _activeStream = false;
  final Map<String, ResolvedStreamSource> _streamCache = {};
  final Map<String, Future<ResolvedStreamSource>> _pendingStreamLookups = {};
  StreamSubscription<Duration>? _streamPositionSub;
  StreamSubscription<Duration?>? _streamDurationSub;
  StreamSubscription<dynamic>? _streamPlaybackSub;
  Future<void> _streamCommandQueue = Future<void>.value();
  LockCachingAudioSource? _cachedStreamAudioSource;
  String? _cachedStreamVideoId;
  int _streamLoadVersion = 0;
  bool _desiredStreamPlaying = false;
  String? _completedTrackIdNotified;

  AudioPlayerNotifier(this.audioEngine) : super(PlayerState()) {
    audioEngine.pause(); // Kill ghost audio surviving hot restarts
    _bindStreamPlayer();

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

    handler.broadcastMediaItem(MediaItem(
      id: 'current_track',
      album: state.artist ?? 'EBB',
      title: state.currentTrackName,
      artist: state.artist ?? 'Unknown Artist',
      duration: Duration(seconds: state.duration),
      artUri: state.thumbnail != null ? Uri.parse(state.thumbnail!) : null,
    ));
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
  }

  void _bindStreamPlayer() {
    _streamDurationSub = streamPlayer.durationStream.listen((duration) {
      if (!_activeStream || duration == null) return;
      final seconds = duration.inSeconds;
      if (seconds > 0 && seconds != state.duration) {
        state = state.copyWith(duration: seconds);
      }
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
        if (isCompleted) {
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
      await oldSource.clearCache();
    } catch (_) {
      // Cache eviction should stay silent.
    }
  }

  Future<void> _setStreamSource(
    String videoId,
    ResolvedStreamSource source,
  ) async {
    final previousSource = _cachedStreamAudioSource;
    final nextSource = LockCachingAudioSource(
      Uri.parse(source.url),
      headers: source.headers.isEmpty ? null : source.headers,
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
        }),
      );
    } catch (error) {
      if (!_activeStream || !_desiredStreamPlaying) return;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Stream failed: $error',
      );
    }
  }

  @override
  void dispose() {
    _playbackTimer?.cancel();
    _streamPositionSub?.cancel();
    _streamDurationSub?.cancel();
    _streamPlaybackSub?.cancel();
    unawaited(stopPlayback(resetState: false));
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

  ResolvedStreamSource? _cacheStreamSource(
    String videoId,
    Map<String, dynamic> payload,
  ) {
    final directUrl = payload['url']?.toString();
    if (directUrl == null || directUrl.isEmpty) {
      return null;
    }

    final source = ResolvedStreamSource(
      url: directUrl,
      headers: _parseHeaders(payload['headers']),
      fetchedAt: DateTime.now(),
    );
    _streamCache[videoId] = source;
    return source;
  }

  Future<ResolvedStreamSource> _fetchStreamSource(String videoId) async {
    final cached = _freshStreamSource(videoId);
    if (cached != null) {
      return cached;
    }

    final res = await appHttpClient
        .get(_proxyUri('/direct_url/$videoId'))
        .timeout(const Duration(seconds: 15));

    if (res.statusCode != 200) {
      throw Exception('Direct stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final source = _cacheStreamSource(videoId, data);
    if (source == null) {
      throw Exception('Proxy returned an empty stream URL');
    }

    return source;
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
    final idsToWarm = <String>[];
    final seen = <String>{};

    for (final rawId in videoIds) {
      final videoId = rawId?.toString();
      if (videoId == null || videoId.isEmpty || !seen.add(videoId)) {
        continue;
      }
      if (_freshStreamSource(videoId) != null ||
          _pendingStreamLookups.containsKey(videoId)) {
        continue;
      }
      idsToWarm.add(videoId);
      if (idsToWarm.length >= 18) {
        break;
      }
    }

    if (idsToWarm.isEmpty) return;

    try {
      final res = await appHttpClient
          .post(
            _proxyUri('/warm_streams'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'video_ids': idsToWarm}),
          )
          .timeout(const Duration(seconds: 18));

      if (res.statusCode != 200) {
        throw Exception('Warm stream lookup failed: ${res.statusCode}');
      }

      final data = jsonDecode(res.body) as Map<String, dynamic>;
      final streams = data['streams'];
      if (streams is! Map) return;

      streams.forEach((key, dynamic value) {
        if (value is Map) {
          _cacheStreamSource(
            key.toString(),
            Map<String, dynamic>.from(value),
          );
        }
      });
    } catch (_) {
      for (final id in idsToWarm.take(4)) {
        unawaited(prewarmStream(id));
      }
    }
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
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
      _activeStream = false;
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
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
      _activeStream = false;
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

      final meta = jsonDecode(res.body);
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
      await File(jsonPath).writeAsString(jsonEncode(meta));

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

      final meta = jsonDecode(res.body);
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
      await File(jsonPath).writeAsString(jsonEncode(meta));
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
    _desiredStreamPlaying = true;
    if (_activeStream &&
        state.videoId == videoId &&
        _cachedStreamVideoId == videoId) {
      await seek(0);
      await play();
      return;
    }

    _activeStream = true;
    audioEngine.pause();
    _playbackTimer?.cancel();

    state = state.copyWith(
        isDownloading: true,
        currentTrackName: fallbackMeta['title'] ?? 'Connecting Stream...',
        thumbnail: fallbackMeta['thumbnail'],
        artist: fallbackMeta['channel'] ?? fallbackMeta['author'],
        videoId: videoId,
        duration: 0,
        currentPosition: 0);

    try {
      final source = await _resolveStreamSource(videoId);
      if (loadVersion != _streamLoadVersion) return;
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setStreamSource(videoId, source);
      });
      if (loadVersion != _streamLoadVersion) return;
      final dur =
          streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);
      
      state = state.copyWith(
          currentTrackName: fallbackMeta['title'],
          duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
          isDownloading: false);
          
      HistoryManager.addHistory(videoId);
      if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
        await _resumeStreamPlayback();
      }
    } catch (e) {
      if (loadVersion != _streamLoadVersion) return;
      try {
        final fallbackUrl = buildProxyUri('/proxy_stream/$videoId').toString();
        await _runStreamCommand(() async {
          if (loadVersion != _streamLoadVersion) return;
          _cachedStreamVideoId = null;
          final previousSource = _cachedStreamAudioSource;
          _cachedStreamAudioSource = null;
          await streamPlayer.stop();
          await streamPlayer.setUrl(fallbackUrl);
          unawaited(_clearOldCachedStream(previousSource));
        });
        if (loadVersion != _streamLoadVersion) return;
        final dur =
            streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);

        state = state.copyWith(
            currentTrackName: fallbackMeta['title'],
            duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
            isDownloading: false);

        HistoryManager.addHistory(videoId);
        if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
          await _resumeStreamPlayback();
        }
      } catch (fallbackError) {
        if (loadVersion != _streamLoadVersion) return;
        state = state.copyWith(
            isDownloading: false,
            currentTrackName: 'Stream failed: $fallbackError');
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
    _completedTrackIdNotified = null;

    try {
      await streamPlayer.stop();
    } catch (_) {}
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
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final res = await appHttpClient
          .post(buildProxyUri('/search'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": query, "limit": 10}))
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        state = jsonDecode(res.body)['results'];
        _primeSearchResults(state);
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

  RecommendationNotifier(this.ref) : super([]);
  bool isLoading = true;
  bool isPaginating = false;

  void _primeRecommendationResults(List<dynamic> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

  Future<void> loadRecommendations([String? seedId]) async {
    isLoading = true;
    state = [];
    try {
      final body = seedId != null
          ? {"query": "", "limit": 15, "seed_id": seedId}
          : {"query": "Trending Hit Songs", "limit": 15};
      final res = await appHttpClient
          .post(buildProxyUri('/recommend'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body)['recommendations'];
        _primeRecommendationResults(state);
      }
    } catch (e) {
      // print("Recommendations failed: $e");
    } finally {
      isLoading = false;
      state = [...state];
    }
  }

  Future<void> loadMore(String seedId) async {
    if (isPaginating) return;
    isPaginating = true;
    try {
      final res = await appHttpClient.post(buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"query": "", "limit": 10, "seed_id": seedId}));
      if (res.statusCode == 200) {
        final newRecs =
            jsonDecode(res.body)['recommendations'] as List<dynamic>;
        final existingIds = state.map((t) => t['id']).toSet();
        final uniqueNew =
            newRecs.where((t) => !existingIds.contains(t['id'])).toList();
        state = [...state, ...uniqueNew];
        _primeRecommendationResults(uniqueNew);
      }
    } catch (e) {
      // print("Load more failed: $e");
    } finally {
      isPaginating = false;
    }
  }
}

final recommendationProvider =
    StateNotifierProvider<RecommendationNotifier, List<dynamic>>((ref) {
  final notifier = RecommendationNotifier(ref);
  HistoryManager.getLatestSeed().then((seed) {
    notifier.loadRecommendations(seed);
  });
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
    int limit = 6,
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

  void _primeUpcomingQueue({int lookahead = 4}) {
    final upcoming = <Map<String, dynamic>>[];
    for (var i = state.currentIndex + 1; i < state.queue.length; i++) {
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
      _primeTracks(state.recommendations, limit: 4);
    }
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
    final res = await appHttpClient
        .post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"query": "", "limit": limit, "seed_id": seedId}),
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

    await ref
        .read(audioPlayerProvider.notifier)
        .streamYoutube(videoId, normalizedTrack);
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
      _primeTracks([localTrack], limit: 1);
      unawaited(_appendRadioRecommendations(seedId: videoId));
    } else {
      clearSession();
    }

    await audioNotifier.play();
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

    await ref
        .read(audioPlayerProvider.notifier)
        .streamYoutube(currentTrackId, currentTrackMap);
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
      playableIndexes.remove(state.currentIndex);
    }
    if (playableIndexes.isEmpty) {
      playableIndexes.add(state.currentIndex.clamp(0, state.queue.length - 1));
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

    final nextIndex = _nextPlayableIndex(state.currentIndex);
    if (nextIndex >= 0) {
      await playQueueIndex(nextIndex);
      return;
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      final refreshedNextIndex = _nextPlayableIndex(state.currentIndex);
      if (refreshedNextIndex >= 0) {
        await playQueueIndex(refreshedNextIndex);
      }
    }
  }

  Future<void> playQueueIndex(int index) async {
    if (index < 0 || index >= state.queue.length) return;
    final track = state.queue[index];
    final videoId = extractTrackId(track);
    if (videoId == null) return;

    final previousTrackId = state.currentTrackId;
    state = state.copyWith(
      currentIndex: index,
      currentTrackId: videoId,
      playedTrackIds: previousTrackId != null && previousTrackId != videoId
          ? _withPlayedTrack(previousTrackId)
          : state.playedTrackIds,
    );
    final localPath = track['local_path']?.toString();
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync()) {
      final loaded = await audioNotifier.loadLocalWithMeta(localPath, track);
      if (!loaded) return;
      await audioNotifier.play();
    } else {
      await audioNotifier.streamYoutube(videoId, track);
    }
    _primeUpcomingQueue();
  }

  void removeFromQueue(String videoId) {
    if (state.mode != PlaybackQueueMode.radio) return;

    final index =
        state.queue.indexWhere((track) => extractTrackId(track) == videoId);
    if (index < 0) return;

    final updatedQueue = [...state.queue]..removeAt(index);
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    if (index == state.currentIndex) {
      final replacementIndex =
          index.clamp(0, updatedQueue.length - 1).toInt();
      state = state.copyWith(
        queue: updatedQueue,
        currentIndex: replacementIndex,
        currentTrackId: extractTrackId(updatedQueue[replacementIndex]),
        playedTrackIds: state.playedTrackIds.where((id) => id != videoId).toList(),
      );
      unawaited(playQueueIndex(replacementIndex));
      return;
    }

    var nextCurrentIndex = state.currentIndex;
    if (index < nextCurrentIndex) {
      nextCurrentIndex -= 1;
    }
    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex,
      playedTrackIds: state.playedTrackIds.where((id) => id != videoId).toList(),
    );
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
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    var nextCurrentIndex = state.currentIndex;
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
    _primeUpcomingQueue();
  }

  Future<void> enqueueTrack(dynamic track) async {
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
    state = state.copyWith(
      queue: [...state.queue, normalizedTrack],
      recommendations: nextRecommendations,
    );
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

    if (state.mode == PlaybackQueueMode.playlist && state.playlistId != null) {
      ref
          .read(playlistProvider.notifier)
          .replaceTracks(state.playlistId!, updatedQueue);
    }
    _primeUpcomingQueue();
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

    final remaining = state.queue.length - state.currentIndex - 1;
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

  ref.onDispose(() {
    if (identical(audioNotifier.onTrackCompleted, handleTrackCompleted)) {
      audioNotifier.onTrackCompleted = null;
    }
    if (identical(audioNotifier.onTrackChanged, handleTrackChanged)) {
      audioNotifier.onTrackChanged = null;
    }
  });

  return notifier;
});

final libraryProvider =
    FutureProvider<List<Map<String, dynamic>>>((ref) async {
  final dir = await getApplicationDocumentsDirectory();
  final files = dir.listSync().where((f) => f.path.endsWith('.mp3')).toList()
    ..sort((a, b) {
      final aTime = a.statSync().modified;
      final bTime = b.statSync().modified;
      return bTime.compareTo(aTime);
    });
  List<Map<String, dynamic>> tracks = [];
  for (var file in files) {
    if (file is File) {
      // GC: Destroy ghost files resulting from aborted connections or proxy refuses
      // Note: Increased to 10000 bytes (10 KB) to eradicate HTML error payloads
      // dumped by rogue port 8000 Educere hijackers. Real MP3s are > 2MB.
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
      final meta = jsonDecode(File(jsonPath).readAsStringSync());
      meta['local_path'] = file.path;
      tracks.add(meta);
    } else {
      tracks.add({
        'title': file.path.split('/').last.replaceAll('.mp3', ''),
        'local_path': file.path,
        'duration': 0,
      });
    }
  }
  return tracks;
});
