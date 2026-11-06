import 'dart:convert';

import 'package:auralis_engine/logic/audio_provider_queue.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final tracks = <Map<String, dynamic>>[
    {'id': 'first', 'title': 'Nero Forte', 'artist': 'Slipknot'},
    {'id': 'second', 'title': 'Psychosocial', 'artist': 'Slipknot',
      'playback_source_id': 'source-id', 'local_path': '/download/track'},
  ];
  test('session round trip retains order, current identity and playlist context', () {
    final before = PlaybackQueueState(mode: PlaybackQueueMode.playlist,
      queue: tracks, currentIndex: 1, currentTrackId: 'second',
      playlistId: 'discovery:first', playlistName: 'Inspired by Nero Forte',
      playedTrackIds: const ['first'], hasMoreRecommendations: false);
    final after = PlaybackQueueState.fromSnapshot(jsonDecode(jsonEncode(before.toSnapshot())))!;
    expect(after.queue, tracks);
    expect(after.currentIndex, 1);
    expect(after.currentTrackId, 'second');
    expect(after.playlistId, before.playlistId);
    expect(after.playlistName, before.playlistName);
    expect(after.playedTrackIds, ['first']);
    expect(after.hasMoreRecommendations, false);
    expect(after.isLoadingRecommendations, false);
  });
  test('missing current track and unknown snapshot versions are not restored', () {
    final snapshot = PlaybackQueueState(mode: PlaybackQueueMode.radio,
      queue: tracks, currentTrackId: 'missing').toSnapshot();
    expect(PlaybackQueueState.fromSnapshot(snapshot), isNull);
    expect(PlaybackQueueState.fromSnapshot({...snapshot, 'version': 2}), isNull);
    expect(PlaybackQueueState.fromSnapshot(const PlaybackQueueState().toSnapshot()), isNull);
  });
  test('local queue search matches mixed artist/title tokens and preserves order', () {
    expect(matchQueueTracks(tracks, 'SLIPKNOT nero'), [tracks.first]);
    expect(matchQueueTracks(tracks, 'slipknot'), tracks);
    expect(matchQueueTracks(tracks, 'missing'), isEmpty);
    expect(matchQueueTracks(tracks, ''), isEmpty);
  });
}
