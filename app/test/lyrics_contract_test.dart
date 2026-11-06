import 'package:auralis_engine/logic/audio_provider.dart';
import 'package:auralis_engine/widgets/player/player_lyrics_panel.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('long source attribution fits a narrow lyrics panel', (tester) async {
    await tester.binding.setSurfaceSize(const Size(360, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: PlayerLyricsPanel(
      playerState: PlayerState(currentTrackName: 'Track', artist: 'Artist'),
      lyricsState: const TrackLyricsState(source: 'Source: Musixmatch'),
      meaningState: const LyricsMeaningState(), isMeaningPanelOpen: false,
      activeIndex: -1, lyricKeyFor: (_) => GlobalKey(), radiusLarge: 24,
      accentColor: Colors.white, onOpenMeaning: () {}, onCloseMeaning: () {},
      onRetryLyrics: () {}, onAskNeatie: () {},
    ))));
    expect(find.text('Source: Musixmatch'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
  test('player state keeps canonical identity separate from playback source',
      () {
    final state = PlayerState().copyWith(
      currentTrackName: 'No Surprises',
      artist: 'Radiohead',
      album: 'OK Computer',
      videoId: 'recording:980a426e-623e-4ea5-98c7-008d037a0508',
      playbackSourceId: '7374CZQoS2Y',
      duration: 229,
    );

    expect(
      state.videoId,
      'recording:980a426e-623e-4ea5-98c7-008d037a0508',
    );
    expect(state.playbackSourceId, '7374CZQoS2Y');
    expect(state.album, 'OK Computer');
  });

  testWidgets('lyrics panel distinguishes an instrumental recording',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(900, 1400));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: PlayerLyricsPanel(
            playerState: PlayerState(
              currentTrackName: 'Instrumental',
              artist: 'Composer',
            ),
            lyricsState: const TrackLyricsState(
              availability: 'instrumental',
            ),
            meaningState: const LyricsMeaningState(),
            isMeaningPanelOpen: false,
            activeIndex: -1,
            lyricKeyFor: (_) => GlobalKey(),
            radiusLarge: 24,
            accentColor: Colors.white,
            onOpenMeaning: () {},
            onCloseMeaning: () {},
            onRetryLyrics: () {},
            onAskNeatie: () {},
          ),
        ),
      ),
    );

    expect(find.text('This recording is instrumental.'), findsOneWidget);
    expect(find.text('There are no vocal lyrics to display.'), findsOneWidget);
  });
}
