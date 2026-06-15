import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../ffi/audio_ffi.dart';

int _parsePlaybackInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
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
      index: _parsePlaybackInt(json['index']),
      text: (json['text'] ?? '').toString(),
      startTimeMs: json['start_time_ms'] == null
          ? null
          : _parsePlaybackInt(json['start_time_ms']),
      endTimeMs: json['end_time_ms'] == null
          ? null
          : _parsePlaybackInt(json['end_time_ms']),
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

  PlayerState copyWith({
    bool? isPlaying,
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
    int? sleepTimerRemainingSeconds,
    bool clearThumbnail = false,
    bool clearArtist = false,
    bool clearVideoId = false,
  }) {
    final nextCurrentPosition = currentPosition ?? this.currentPosition;
    return PlayerState(
      isPlaying: isPlaying ?? this.isPlaying,
      isLooping: isLooping ?? this.isLooping,
      currentTrackName: currentTrackName ?? this.currentTrackName,
      isDownloading: isDownloading ?? this.isDownloading,
      thumbnail: clearThumbnail ? null : (thumbnail ?? this.thumbnail),
      duration: duration ?? this.duration,
      filesize: filesize ?? this.filesize,
      currentPosition: nextCurrentPosition,
      currentPositionMs: currentPositionMs ??
          (currentPosition != null
              ? currentPosition * 1000
              : this.currentPositionMs),
      artist: clearArtist ? null : (artist ?? this.artist),
      videoId: clearVideoId ? null : (videoId ?? this.videoId),
      sleepTimerRemainingSeconds:
          sleepTimerRemainingSeconds ?? this.sleepTimerRemainingSeconds,
    );
  }
}
