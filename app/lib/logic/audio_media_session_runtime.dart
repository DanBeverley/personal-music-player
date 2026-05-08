import 'package:audio_service/audio_service.dart';

import 'audio_service_bridge.dart';
import 'playback_models.dart';
import 'track_metadata.dart';

String? _mediaSessionTrackArtist(dynamic rawTrack) {
  if (rawTrack is! Map) return null;
  return rawTrack['channel']?.toString() ??
      rawTrack['author']?.toString() ??
      rawTrack['artist']?.toString();
}

int _mediaSessionParseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

Map<String, dynamic>? currentTrackSnapshot({
  required PlayerState state,
  required bool managedQueueActive,
  required int? currentIndex,
  required List<Map<String, dynamic>> managedQueueTracks,
  required Map<String, dynamic>? restorableTrackMeta,
}) {
  final activeTrackId = state.videoId?.trim();
  if (activeTrackId == null || activeTrackId.isEmpty) {
    return restorableTrackMeta == null
        ? null
        : Map<String, dynamic>.from(restorableTrackMeta);
  }
  if (managedQueueActive &&
      currentIndex != null &&
      currentIndex >= 0 &&
      currentIndex < managedQueueTracks.length) {
    final track = normalizeTrack(managedQueueTracks[currentIndex]);
    final trackId = extractTrackId(track)?.trim() ?? '';
    if (trackId == activeTrackId) {
      return Map<String, dynamic>.from(track);
    }
  }
  final rememberedTrack = restorableTrackMeta;
  if (rememberedTrack != null) {
    final rememberedId = extractTrackId(rememberedTrack)?.trim() ?? '';
    if (rememberedId == activeTrackId) {
      return Map<String, dynamic>.from(rememberedTrack);
    }
  }
  return null;
}

MediaItem mediaItemFromTrack(Map<String, dynamic> track) {
  final duration = _mediaSessionParseInt(track['duration']);
  final thumbnail = track['thumbnail']?.toString();
  return MediaItem(
    id: extractTrackId(track) ?? track['title']?.toString() ?? 'track',
    album: track['album']?.toString() ?? track['album_title']?.toString() ?? '',
    title: track['title']?.toString() ?? 'Unknown Track',
    artist: _mediaSessionTrackArtist(track) ?? 'Unknown Artist',
    duration: duration > 0 ? Duration(seconds: duration) : null,
    artUri: thumbnail != null && thumbnail.isNotEmpty
        ? Uri.tryParse(thumbnail)
        : null,
    extras: {
      'displayTitle': track['title']?.toString() ?? 'Unknown Track',
      'displaySubtitle': _mediaSessionTrackArtist(track) ?? 'Unknown Artist',
      'displayDescription':
          track['album']?.toString() ?? track['album_title']?.toString() ?? '',
    },
  );
}

MediaItem currentMediaItem({
  required PlayerState state,
  required bool managedQueueActive,
  required int? currentIndex,
  required List<Map<String, dynamic>> managedQueueTracks,
  required Map<String, dynamic>? restorableTrackMeta,
}) {
  final activeTrack = currentTrackSnapshot(
    state: state,
    managedQueueActive: managedQueueActive,
    currentIndex: currentIndex,
    managedQueueTracks: managedQueueTracks,
    restorableTrackMeta: restorableTrackMeta,
  );
  final album =
      (activeTrack?['album'] ?? activeTrack?['album_title'])?.toString().trim();
  final albumTitle = album != null && album.isNotEmpty ? album : 'EBB';
  final artistName = _mediaSessionTrackArtist(activeTrack) ??
      state.artist ??
      'Unknown Artist';
  final thumbnail = (activeTrack?['thumbnail'] ?? state.thumbnail)
      ?.toString()
      .trim();
  final artUri = thumbnail == null || thumbnail.isEmpty
      ? null
      : Uri.tryParse(thumbnail);
  return MediaItem(
    id: state.videoId ?? 'current_track',
    album: albumTitle,
    title: state.currentTrackName,
    artist: artistName,
    duration: Duration(seconds: state.duration),
    artUri: artUri,
    extras: {
      'displayTitle': state.currentTrackName,
      'displaySubtitle': artistName,
      'displayDescription': albumTitle,
    },
  );
}

void broadcastManagedQueue({
  required AuralisAudioHandler handler,
  required bool managedQueueActive,
  required List<Map<String, dynamic>> managedQueueTracks,
}) {
  if (!managedQueueActive || managedQueueTracks.isEmpty) {
    handler.broadcastQueue(const []);
    return;
  }
  handler.broadcastQueue(
    managedQueueTracks.map(mediaItemFromTrack).toList(growable: false),
  );
}

void broadcastPlaybackState({
  required AuralisAudioHandler handler,
  required PlayerState state,
  required bool managedQueueActive,
  required int? currentQueueIndex,
  required int managedQueueLength,
}) {
  final hasReachedTrackEnd = !state.isLooping &&
      !state.isPlaying &&
      state.duration > 0 &&
      state.currentPosition >= state.duration;
  final processingState = state.isDownloading
      ? AudioProcessingState.buffering
      : state.videoId == null || state.videoId!.isEmpty
          ? AudioProcessingState.idle
          : hasReachedTrackEnd
              ? AudioProcessingState.completed
              : AudioProcessingState.ready;
  final broadcastQueueIndex = managedQueueActive
      ? (currentQueueIndex ?? 0)
          .clamp(0, managedQueueLength == 0 ? 0 : managedQueueLength - 1)
          .toInt()
      : null;

  handler.broadcastState(
    PlaybackState(
      controls: [
        MediaControl.skipToPrevious,
        MediaControl.rewind,
        if (state.isPlaying) MediaControl.pause else MediaControl.play,
        MediaControl.fastForward,
        MediaControl.skipToNext,
      ],
      systemActions: const <MediaAction>{
        MediaAction.seek,
        MediaAction.seekForward,
        MediaAction.seekBackward,
      },
      androidCompactActionIndices: const [0, 2, 4],
      processingState: processingState,
      playing: state.isPlaying,
      updatePosition: Duration(seconds: state.currentPosition),
      bufferedPosition: Duration(seconds: state.currentPosition),
      speed: state.isPlaying ? 1.0 : 0.0,
      queueIndex: broadcastQueueIndex,
    ),
  );
}

void broadcastStoppedState({
  required AuralisAudioHandler handler,
}) {
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
