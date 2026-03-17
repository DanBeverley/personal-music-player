import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';
import 'dart:convert';
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../ffi/audio_ffi.dart';
import '../ffi/ingestion_ffi.dart';
import 'package:ffi/ffi.dart';
import 'package:audio_service/audio_service.dart';
import 'package:just_audio/just_audio.dart';

late AudioHandler globalAudioHandler;
const String proxyBaseUrl =
    String.fromEnvironment('AURALIS_PROXY_URL', defaultValue: 'http://34.172.70.149');

Future<void> initAudioService() async {
  globalAudioHandler = await AudioService.init(
    builder: () => AuralisAudioHandler(),
    config: const AudioServiceConfig(
      androidNotificationChannelId: 'com.example.app.channel.audio',
      androidNotificationChannelName: 'Auralis Audio Playback',
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

  @override
  Future<void> play() async => onPlay?.call();
  @override
  Future<void> pause() async => onPause?.call();
  @override
  Future<void> seek(Duration position) async => onSeek?.call(position);

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

// Provides the singleton FFI ingestion engine
final ingestionEngineProvider = Provider<IngestionEngineFFI>((ref) {
  return IngestionEngineFFI();
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
      String? artist,
      String? videoId}) {
    return PlayerState(
      isPlaying: isPlaying ?? this.isPlaying,
      isLooping: isLooping ?? this.isLooping,
      currentTrackName: currentTrackName ?? this.currentTrackName,
      isDownloading: isDownloading ?? this.isDownloading,
      thumbnail: thumbnail ?? this.thumbnail,
      duration: duration ?? this.duration,
      filesize: filesize ?? this.filesize,
      currentPosition: currentPosition ?? this.currentPosition,
      artist: artist ?? this.artist,
      videoId: videoId ?? this.videoId,
    );
  }
}

class AudioPlayerNotifier extends StateNotifier<PlayerState> {
  final AudioEngineFFI audioEngine;
  final IngestionEngineFFI ingestionEngine;
  Timer? _playbackTimer;
  final streamPlayer = AudioPlayer();
  bool _activeStream = false;

  AudioPlayerNotifier(this.audioEngine, this.ingestionEngine)
      : super(PlayerState()) {
    audioEngine.pause(); // Kill ghost audio surviving hot restarts

    if (globalAudioHandler is AuralisAudioHandler) {
      final handler = globalAudioHandler as AuralisAudioHandler;
      handler.onPlay = play;
      handler.onPause = pause;
      handler.onSeek = (Duration d) => seek(d.inSeconds);
    }
  }

  @override
  set state(PlayerState value) {
    bool shouldBroadcast = super.state.isPlaying != value.isPlaying ||
        super.state.currentTrackName != value.currentTrackName ||
        super.state.isDownloading != value.isDownloading ||
        (super.state.currentPosition - value.currentPosition).abs() > 1;

    super.state = value;
    
    if (shouldBroadcast) {
      _broadcastState();
    }
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
      album: state.artist ?? 'Auralis',
      title: state.currentTrackName,
      artist: state.artist ?? 'Unknown Artist',
      duration: Duration(seconds: state.duration),
      artUri: state.thumbnail != null ? Uri.parse(state.thumbnail!) : null,
    ));
  }

  @override
  void dispose() {
    _playbackTimer?.cancel();
    super.dispose();
  }

  Uri _proxyUri(String path) => Uri.parse('$proxyBaseUrl$path');

  bool _hasValidDownloadedAudio(String path) {
    try {
      final file = File(path);
      return file.existsSync() && file.lengthSync() >= 10000;
    } catch (_) {
      return false;
    }
  }

  bool _loadNativeFile(String path) {
    final pathPtr = path.toNativeUtf8();
    final loaded = audioEngine.loadFile(pathPtr);
    malloc.free(pathPtr);
    return loaded;
  }

  bool loadLocalFile(String path, String trackName) {
    _activeStream = false;
    streamPlayer.stop();

    if (!_hasValidDownloadedAudio(path)) {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
      );
      return false;
    }

    final loaded = _loadNativeFile(path);
    if (!loaded) {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: audio engine could not load file',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      currentTrackName: trackName,
      thumbnail: null,
      artist: null,
      videoId: null,
      duration: 0,
      currentPosition: 0,
    );
    return true;
  }

  bool loadLocalWithMeta(String path, Map<String, dynamic> meta) {
    _activeStream = false;
    streamPlayer.stop();

    if (!_hasValidDownloadedAudio(path)) {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
      );
      return false;
    }

    final loaded = _loadNativeFile(path);
    if (!loaded) {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: audio engine could not load file',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      currentTrackName: meta['title'] ?? 'Unknown Track',
      thumbnail: meta['thumbnail'],
      duration: _parseInt(meta['duration']),
      filesize: _parseInt(meta['filesize']),
      artist: meta['author'] ?? meta['artist'],
      videoId: meta['video_id'] ?? meta['id'],
      currentPosition: 0,
    );
    HistoryManager.addHistory(meta['video_id'] ?? meta['id']);
    return true;
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
      final res = await http.post(_proxyUri('/download'),
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
      final streamRes = await http.Client().send(req);

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

      final loaded = loadLocalWithMeta(outPath, meta);
      if (!loaded) {
        throw Exception('Downloaded file saved, but the player could not load it');
      }
      state = state.copyWith(isDownloading: false);
      play();
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
      final res = await http.post(_proxyUri('/download'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId, "title": titleStr}));

      if (res.statusCode != 200) {
        throw Exception("Proxy Refused Connection or Failed to DL");
      }

      final meta = jsonDecode(res.body);
      HistoryManager.addHistory(videoId);

      final req = http.Request('GET', _proxyUri('/stream/$videoId'));
      final streamRes = await http.Client().send(req);

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

  void streamYoutube(String videoId, Map<String, dynamic> fallbackMeta) async {
    _activeStream = true;
    audioEngine.pause();
    await streamPlayer.stop();

    state = state.copyWith(
        isDownloading: true,
        currentTrackName: fallbackMeta['title'] ?? 'Connecting Stream...',
        thumbnail: fallbackMeta['thumbnail'],
        artist: fallbackMeta['channel'] ?? fallbackMeta['author'],
        videoId: videoId,
        duration: 0,
        currentPosition: 0);

    try {
      final res = await http
          .get(_proxyUri('/direct_url/$videoId'))
          .timeout(const Duration(seconds: 15));

      if (res.statusCode != 200) {
        throw Exception('Direct stream lookup failed: ${res.statusCode}');
      }

      final data = jsonDecode(res.body) as Map<String, dynamic>;
      final directUrl = data['url']?.toString();
      if (directUrl == null || directUrl.isEmpty) {
        throw Exception('Proxy returned an empty stream URL');
      }

      final headers = _parseHeaders(data['headers']);
      await streamPlayer.setUrl(directUrl, headers: headers.isEmpty ? null : headers);
      final dur = streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);
      
      state = state.copyWith(
          currentTrackName: fallbackMeta['title'],
          duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
          isDownloading: false);
          
      HistoryManager.addHistory(videoId);
      play();
    } catch (e) {
      try {
        final fallbackUrl = '$proxyBaseUrl/proxy_stream/$videoId';
        await streamPlayer.setUrl(fallbackUrl);
        final dur = streamPlayer.duration?.inSeconds ?? _parseInt(fallbackMeta['duration']);

        state = state.copyWith(
            currentTrackName: fallbackMeta['title'],
            duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
            isDownloading: false);

        HistoryManager.addHistory(videoId);
        play();
      } catch (fallbackError) {
        state = state.copyWith(
            isDownloading: false,
            currentTrackName: 'Stream failed: $fallbackError');
      }
    }
  }

  void _startTimer() {
    _playbackTimer?.cancel();
    _playbackTimer = Timer.periodic(const Duration(milliseconds: 200), (timer) {
      if (state.duration == 0 || state.currentPosition < state.duration) {
        int pos = _activeStream 
            ? streamPlayer.position.inSeconds 
            : audioEngine.getPosition() ~/ 1000;
        state = state.copyWith(currentPosition: pos);
      } else {
        if (state.isLooping) {
          if (_activeStream) {
             streamPlayer.seek(Duration.zero);
          }
          state = state.copyWith(currentPosition: 0);
        } else {
          pause();
        }
      }
    });
  }

  void play() {
    if (_activeStream) {
      streamPlayer.play();
    } else {
      audioEngine.play();
    }
    state = state.copyWith(isPlaying: true);
    _startTimer();
  }

  void pause() {
    if (_activeStream) {
      streamPlayer.pause();
    } else {
      audioEngine.pause();
    }
    state = state.copyWith(isPlaying: false);
    _playbackTimer?.cancel();
  }

  void seek(int seconds) {
    if (_activeStream) {
      streamPlayer.seek(Duration(seconds: seconds));
    } else {
      audioEngine.seek(seconds * 1000); // Send MS to C++
    }
    state = state.copyWith(currentPosition: seconds);
  }

  void toggleLoop(int startMs, int endMs) {
    final newState = !state.isLooping;
    if (_activeStream) {
       streamPlayer.setLoopMode(newState ? LoopMode.one : LoopMode.off);
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
  final ingestionE = ref.watch(ingestionEngineProvider);
  return AudioPlayerNotifier(audioE, ingestionE);
});

// Search & Recommendation State Management
class SearchNotifier extends StateNotifier<List<dynamic>> {
  SearchNotifier() : super([]);
  bool isLoading = false;

  Future<void> search(String query) async {
    isLoading = true;
    state = [];
    try {
      final res = await http
          .post(Uri.parse('$proxyBaseUrl/search'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": query, "limit": 10}))
          .timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body)['results'];
      }
    } catch (e) {
      // print("Search failed: $e");
    } finally {
      isLoading = false;
      state = [...state]; // trigger rebuild
    }
  }
}

final searchProvider =
    StateNotifierProvider<SearchNotifier, List<dynamic>>((ref) {
  return SearchNotifier();
});

class RecommendationNotifier extends StateNotifier<List<dynamic>> {
  RecommendationNotifier() : super([]);
  bool isLoading = true;
  bool isPaginating = false;

  Future<void> loadRecommendations([String? seedId]) async {
    isLoading = true;
    state = [];
    try {
      final body = seedId != null
          ? {"query": "", "limit": 15, "seed_id": seedId}
          : {"query": "Trending Hit Songs", "limit": 15};
      final res = await http
          .post(Uri.parse('$proxyBaseUrl/recommend'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body)['recommendations'];
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
      final res = await http.post(Uri.parse('$proxyBaseUrl/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"query": "", "limit": 10, "seed_id": seedId}));
      if (res.statusCode == 200) {
        final newRecs =
            jsonDecode(res.body)['recommendations'] as List<dynamic>;
        final existingIds = state.map((t) => t['id']).toSet();
        final uniqueNew =
            newRecs.where((t) => !existingIds.contains(t['id'])).toList();
        state = [...state, ...uniqueNew];
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
  final notifier = RecommendationNotifier();
  HistoryManager.getLatestSeed().then((seed) {
    notifier.loadRecommendations(seed);
  });
  return notifier;
});

class SuggestNotifier extends StateNotifier<List<String>> {
  SuggestNotifier() : super([]);

  Future<void> fetchSuggestions(String query) async {
    if (query.isEmpty) {
      state = [];
      return;
    }
    try {
      final res = await http
          .post(Uri.parse('$proxyBaseUrl/suggest'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({"query": query, "limit": 5}))
          .timeout(const Duration(seconds: 10));
      if (res.statusCode == 200) {
        state = List<String>.from(jsonDecode(res.body)['results']);
      }
    } catch (e) {
      // print("Suggest failed: $e");
    }
  }

  void clear() => state = [];
}

final suggestProvider =
    StateNotifierProvider<SuggestNotifier, List<String>>((ref) {
  return SuggestNotifier();
});

class TrackDetailsNotifier extends StateNotifier<Map<String, dynamic>?> {
  TrackDetailsNotifier() : super(null);

  Future<void> fetchDetails(String videoId) async {
    state = null; // show loading
    try {
      final res = await http.post(
          Uri.parse('$proxyBaseUrl/track_details'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId}));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body);
      }
    } catch (e) {
      // print("Track Details failed: $e");
    }
  }
}

final trackDetailsProvider =
    StateNotifierProvider<TrackDetailsNotifier, Map<String, dynamic>?>((ref) {
  return TrackDetailsNotifier();
});

final libraryProvider =
    FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final dir = await getApplicationDocumentsDirectory();
  final files = dir.listSync().where((f) => f.path.endsWith('.mp3')).toList();
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
