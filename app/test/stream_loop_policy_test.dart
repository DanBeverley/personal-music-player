import 'package:auralis_engine/logic/stream_loop_policy.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:just_audio/just_audio.dart';

void main() {
  test('single stream loops delegate track repeat to just_audio', () {
    final playbackMode = streamPlaybackModeFor(managedQueueActive: false);
    final loopPolicy = streamLoopPolicyFor(isLooping: true);

    expect(playbackMode, StreamPlaybackMode.singleStream);
    expect(loopPolicy, StreamLoopPolicy.track);
    expect(
      justAudioLoopModeFor(playbackMode: playbackMode, isLooping: true),
      LoopMode.one,
    );
  });

  test('managed queue loops delegate track repeat to just_audio', () {
    final playbackMode = streamPlaybackModeFor(managedQueueActive: true);
    final loopPolicy = streamLoopPolicyFor(isLooping: true);

    expect(playbackMode, StreamPlaybackMode.managedQueue);
    expect(loopPolicy, StreamLoopPolicy.track);
    expect(
      justAudioLoopModeFor(playbackMode: playbackMode, isLooping: true),
      LoopMode.one,
    );
    expect(
      shouldWarmActiveTrackForLoop(
        playbackMode: playbackMode,
        isLooping: true,
      ),
      isFalse,
    );
  });

  test('non-loop playback can warm queue tracks normally', () {
    final playbackMode = streamPlaybackModeFor(managedQueueActive: true);

    expect(
      shouldWarmActiveTrackForLoop(
        playbackMode: playbackMode,
        isLooping: false,
      ),
      isTrue,
    );
  });

  test('loop off disables both app and just_audio repeat', () {
    final playbackMode = streamPlaybackModeFor(managedQueueActive: true);
    final loopPolicy = streamLoopPolicyFor(isLooping: false);

    expect(loopPolicy, StreamLoopPolicy.off);
    expect(
      justAudioLoopModeFor(playbackMode: playbackMode, isLooping: false),
      LoopMode.off,
    );
  });
}
