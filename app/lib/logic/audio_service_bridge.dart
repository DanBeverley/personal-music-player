import 'dart:async';

import 'package:audio_service/audio_service.dart';
import 'package:flutter/foundation.dart';

final AuralisAudioHandler _bootstrapAudioHandler = AuralisAudioHandler();
AudioHandler globalAudioHandler = _bootstrapAudioHandler;

Future<void> initAudioService() async {
  try {
    globalAudioHandler = await AudioService.init(
      builder: () => _bootstrapAudioHandler,
      config: const AudioServiceConfig(
        androidNotificationChannelId: 'com.danbeverley.ebb.channel.audio',
        androidNotificationChannelName: 'Neatie Audio Playback',
        androidNotificationChannelDescription:
            'Playback controls for Neatie on notifications and the Android lock screen.',
        androidNotificationOngoing: true,
        androidNotificationClickStartsActivity: true,
        androidResumeOnClick: true,
        androidStopForegroundOnPause: true,
        fastForwardInterval: Duration(seconds: 10),
        rewindInterval: Duration(seconds: 10),
      ),
    ).timeout(const Duration(seconds: 8));
  } on TimeoutException catch (error) {
    debugPrint('AudioService init timed out: $error');
  } catch (error) {
    debugPrint('AudioService init failed: $error');
  }
}

class AuralisAudioHandler extends BaseAudioHandler
    with QueueHandler, SeekHandler {
  Function()? onPlay;
  Function()? onPause;
  Function(Duration)? onSeek;
  Function()? onFastForward;
  Function()? onRewind;
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
  Future<void> fastForward() async => onFastForward?.call();
  @override
  Future<void> rewind() async => onRewind?.call();
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
