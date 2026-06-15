import 'package:just_audio/just_audio.dart';

enum StreamPlaybackMode {
  singleStream,
  managedQueue,
}

enum StreamLoopPolicy {
  off,
  track,
}

StreamPlaybackMode streamPlaybackModeFor({
  required bool managedQueueActive,
}) =>
    managedQueueActive
        ? StreamPlaybackMode.managedQueue
        : StreamPlaybackMode.singleStream;

StreamLoopPolicy streamLoopPolicyFor({
  required bool isLooping,
}) =>
    isLooping ? StreamLoopPolicy.track : StreamLoopPolicy.off;

bool shouldWarmActiveTrackForLoop({
  required StreamPlaybackMode playbackMode,
  required bool isLooping,
}) =>
    !(playbackMode == StreamPlaybackMode.managedQueue && isLooping);

LoopMode justAudioLoopModeFor({
  required StreamPlaybackMode playbackMode,
  required bool isLooping,
}) =>
    isLooping ? LoopMode.one : LoopMode.off;
