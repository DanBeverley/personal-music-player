import 'package:auralis_engine/logic/audio_queue_runtime.dart';
import 'package:auralis_engine/logic/audio_stream_runtime.dart';
import 'package:auralis_engine/logic/track_metadata.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:just_audio/just_audio.dart';

Map<String, dynamic> _track(String id) => {
      'videoId': id,
      'title': 'Track $id',
    };

void main() {
  test('stall policy performs at most one bounded recovery per stall', () {
    expect(
      playbackStallRecoveryAction(
        stalledFor: const Duration(seconds: 8),
        waitingForNetwork: true,
        priorRecoveryAttempts: 0,
      ),
      PlaybackStallRecoveryAction.wait,
    );
    expect(
      playbackStallRecoveryAction(
        stalledFor: const Duration(seconds: 10),
        waitingForNetwork: false,
        priorRecoveryAttempts: 0,
      ),
      PlaybackStallRecoveryAction.softSeek,
    );
    expect(
      playbackStallRecoveryAction(
        stalledFor: const Duration(seconds: 18),
        waitingForNetwork: true,
        priorRecoveryAttempts: 0,
      ),
      PlaybackStallRecoveryAction.refreshSource,
    );
    expect(
      playbackStallRecoveryAction(
        stalledFor: const Duration(seconds: 60),
        waitingForNetwork: true,
        priorRecoveryAttempts: 1,
      ),
      PlaybackStallRecoveryAction.wait,
    );
  });

  test('managed physical index maps back to logical queue by identity', () {
    final logical = [_track('first'), _track('second'), _track('third')];
    final admitted = [_track('second'), _track('third')];

    final managedTrackId = trackIdentityAt(admitted, 0);

    expect(managedTrackId, 'second');
    expect(trackIndexByIdentity(logical, managedTrackId), 1);
    expect(trackIndexByIdentity(admitted, 'third'), 1);
  });

  test('fresh three-song queue keeps every source and its original index',
      () async {
    final tracks = [_track('first'), _track('second'), _track('third')];
    final builtIds = <String>[];

    final resolved = await resolveManagedQueueSources(
      tracks: tracks,
      preferredInitialIndex: 0,
      buildManagedAudioSource: (track) async {
        final id = track['videoId'] as String;
        builtIds.add(id);
        return AudioSource.uri(
          Uri.parse('https://playback.test/playback/stream/$id'),
          tag: track,
        );
      },
      registerUnavailableTrack: (_, [__]) {},
    );

    expect(builtIds, ['first', 'second', 'third']);
    expect(resolved.tracks, tracks);
    expect(resolved.sources, hasLength(3));
    expect(resolved.initialIndex, 0);
  });

  test('starting from the second song preserves logical queue indexing',
      () async {
    final tracks = [_track('first'), _track('second'), _track('third')];

    final resolved = await resolveManagedQueueSources(
      tracks: tracks,
      preferredInitialIndex: 1,
      buildManagedAudioSource: (track) async => AudioSource.uri(
        Uri.parse(
          'https://playback.test/playback/stream/${track['videoId']}',
        ),
        tag: track,
      ),
      registerUnavailableTrack: (_, [__]) {},
    );

    expect(resolved.tracks, tracks);
    expect(resolved.sources, hasLength(3));
    expect(resolved.initialIndex, 1);
  });

  test('transient preparation failures do not poison the playback queue', () {
    expect(
      isTerminalPlaybackPreparationFailure({
        'code': 'stream_failed',
        'message': 'upstream timeout',
      }),
      isFalse,
    );
    expect(
      isTerminalPlaybackPreparationFailure({
        'code': 'source_blocked',
        'message': 'temporary provider verification',
      }),
      isFalse,
    );
    expect(
      isTerminalPlaybackPreparationFailure({
        'code': 'playback_source_missing',
      }),
      isTrue,
    );
  });

  test('canonical playback routes through backend identity verification', () {
    final track = {
      'track_key': 'recording:smoke on the water|deep purple',
      'videoId': '6YGQPGBD1uQ',
      'playback': {
        'provider': 'youtube',
        'source_id': '6YGQPGBD1uQ',
      },
    };

    expect(
      extractTrackId(track),
      'recording:smoke on the water|deep purple',
    );
    expect(
      extractPlaybackRoutingKey(track),
      'recording:smoke on the water|deep purple',
    );
  });

  test('provider-only tracks route directly to their physical source', () {
    final track = {
      'track_key': '6YGQPGBD1uQ',
      'videoId': '6YGQPGBD1uQ',
      'playback': {
        'provider': 'youtube',
        'source_id': '6YGQPGBD1uQ',
      },
    };

    expect(extractPlaybackRoutingKey(track), '6YGQPGBD1uQ');
  });
}
