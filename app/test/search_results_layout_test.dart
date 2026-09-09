import 'package:auralis_engine/widgets/home/search_surface_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('late tracks append below related artists without shifting them', (tester) async {
    final scroll = ScrollController();
    addTearDown(scroll.dispose);
    final initial = List.generate(12, (i) => <String, dynamic>{
      'id': 'track:$i', 'title': 'Initial $i', 'artist': 'Artist',
    });
    Widget page(List<Map<String, dynamic>> tracks, {bool artworkReady = false}) => ProviderScope(
      child: MaterialApp(home: Scaffold(body: SingleChildScrollView(
        controller: scroll,
        child: NeatieSearchResultsSection(
          query: 'Track', queryIntent: 'track', topResult: null,
          leadArtist: null, containingAlbum: null, selectedTab: 'top',
          isLoading: false, loadingSurfaces: const {}, appendedItemKeys: const {},
          tracks: tracks, artists: const [], albums: [
            {'id': 'album:late', 'title': 'Late album', 'artist': 'Artist',
              if (artworkReady) 'thumbnail': 'https://example.test/album.jpg'},
          ],
          recentlyPlayedTracks: const [],
          similarArtists: const [{'id': 'artist:neighbor', 'name': 'Neighbor'}],
          artistTracks: const [], artistAlbums: const [], similarTracks: const [],
          playlists: const [], errorMessage: null,
          onPlayTrack: (_) {}, onPrimeTrack: (_) {}, onOpenTrack: (_) {},
          onOpenArtist: (_) {}, onOpenAlbum: (_) {}, onOpenPlaylist: (_) {},
        ),
      ))),
    );
    await tester.pumpWidget(page(initial));
    await tester.pumpAndSettle();
    scroll.jumpTo(scroll.position.maxScrollExtent);
    await tester.pumpAndSettle();
    final anchor = tester.getTopLeft(find.text('Related artists'));
    final offset = scroll.offset;
    final late = {'id': 'track:late', 'title': 'Late song', 'artist': 'Artist'};
    await tester.pumpWidget(page([...initial, late], artworkReady: true));
    await tester.pumpAndSettle();
    expect(scroll.offset, offset);
    expect(tester.getTopLeft(find.text('Related artists')), anchor);
    expect(tester.getTopLeft(find.text('Late song')).dy, greaterThan(anchor.dy));
    expect(tester.getTopLeft(find.text('Late album')).dy, greaterThan(anchor.dy));
    await tester.pumpWidget(page([...initial, late, late], artworkReady: true));
    await tester.pumpAndSettle();
    expect(find.text('Late song'), findsOneWidget);
  });
}
