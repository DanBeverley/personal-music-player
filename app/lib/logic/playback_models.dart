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
  final String sourceKind;
  final DateTime? expiresAt;

  const ResolvedStreamSource({
    required this.url,
    required this.headers,
    required this.fetchedAt,
    this.sourceKind = 'proxy',
    this.expiresAt,
  });

  bool get isFresh {
    final now = DateTime.now();
    if (expiresAt != null && now.isAfter(expiresAt!)) {
      return false;
    }
    return now.difference(fetchedAt) < const Duration(minutes: 25);
  }
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

class LyricsMeaningInsight {
  final String summary;
  final List<String> themes;
  final String emotionalTone;
  final List<String> contextNotes;
  final List<String> notableImagery;
  final double confidence;
  final String sourceNotes;
  final String assistantSeedMessage;
  final bool cached;

  const LyricsMeaningInsight({
    required this.summary,
    this.themes = const [],
    this.emotionalTone = '',
    this.contextNotes = const [],
    this.notableImagery = const [],
    this.confidence = 0,
    this.sourceNotes = '',
    this.assistantSeedMessage = '',
    this.cached = false,
  });

  factory LyricsMeaningInsight.fromJson(Map<String, dynamic> json) {
    List<String> stringList(dynamic value) {
      if (value is! List) return const [];
      return value
          .map((item) => item.toString().trim())
          .where((item) => item.isNotEmpty)
          .toList(growable: false);
    }

    double parseConfidence(dynamic value) {
      if (value is num) {
        return value.toDouble().clamp(0.0, 1.0).toDouble();
      }
      final parsed = double.tryParse(value?.toString() ?? '');
      if (parsed == null) return 0;
      return parsed.clamp(0.0, 1.0).toDouble();
    }

    return LyricsMeaningInsight(
      summary: json['summary']?.toString() ?? '',
      themes: stringList(json['themes']),
      emotionalTone: json['emotional_tone']?.toString() ?? '',
      contextNotes: stringList(json['context_notes']),
      notableImagery: stringList(json['notable_imagery']),
      confidence: parseConfidence(json['confidence']),
      sourceNotes: json['source_notes']?.toString() ?? '',
      assistantSeedMessage: json['assistant_seed_message']?.toString() ?? '',
      cached: json['cached'] == true,
    );
  }
}

class LyricsMeaningState {
  final bool isLoading;
  final String? videoId;
  final String? error;
  final LyricsMeaningInsight? insight;

  const LyricsMeaningState({
    this.isLoading = false,
    this.videoId,
    this.error,
    this.insight,
  });

  bool get hasInsight => insight != null && insight!.summary.trim().isNotEmpty;
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
