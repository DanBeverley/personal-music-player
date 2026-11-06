import 'dart:async';
import 'dart:convert';

import 'package:auralis_engine/web/catalog_api.dart';
import 'package:auralis_engine/web/catalog_detail.dart';
import 'package:auralis_engine/web/browse_widgets.dart';
import 'package:auralis_engine/web/music_api.dart';
import 'package:auralis_engine/web/playback_port.dart';
import 'package:auralis_engine/web/web_player.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

void main() {
  test('release labels keep exact precision and reject invalid dates', () {
    expect(catalogReleaseDate({'release_date': '2024-04-19', 'year': 2023}),
        '19 Apr 2024');
    expect(catalogReleaseDate({'releaseDate': '2024-04-19T00:00:00Z'}),
        '19 Apr 2024');
    expect(catalogReleaseDate({'release_date': '2024-04'}), 'Apr 2024');
    expect(catalogReleaseDate({'year': 1991}), '1991');
    expect(catalogReleaseDate({'release_date': '2024-02-30', 'year': 2024}),
        '2024');
    expect(catalogReleaseDate({'release_date': 'unknown'}), '');
    expect(catalogReleaseDate({}), '');
  });

  testWidgets('artist overview renders before exact release counts arrive',
      (tester) async {
    final album = Completer<http.Response>();
    final api = CatalogApi(MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      if (request.url.path.startsWith('/album/')) return album.future;
      return http.Response(
          jsonEncode({
            'name': 'Artist',
            'top_songs': [
              {
                'id': 'exact_source',
                'videoId': 'exact_source',
                'title': 'Track',
                'album_id': 'release',
                'year': 2024
              },
            ],
          }),
          200);
    })));
    final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
    addTearDown(player.dispose);
    addTearDown(api.api.client.close);
    await tester.pumpWidget(MaterialApp(
        home: CatalogDetail(
      api: api,
      id: 'artist',
      kind: 'artist',
      title: 'Artist',
      player: player,
      onLike: (_) async {},
      onOpen: (_, __, ___) {},
      onBack: () {},
    )));
    await tester.pumpAndSettle();
    expect(find.text('Track'), findsOneWidget);
    album.complete(http.Response(
        jsonEncode({
          'release_date': '2024-05-16',
          'tracks': [
            {
              'id': 'other_video',
              'videoId': 'other_video',
              'title': 'Track',
              'views': '99M plays'
            },
            {
              'id': 'exact_source',
              'videoId': 'exact_source',
              'title': 'Track',
              'views': '24M plays'
            },
          ],
        }),
        200));
    await tester.pumpAndSettle();
    expect(find.text('16 May 2024 · 24M plays'), findsOneWidget);
    expect(find.textContaining('99M'), findsNothing);
  });

  testWidgets(
      'album shows supplied full release date in its sleeve and liner notes',
      (tester) async {
    final api = _catalogApi({
      'title': 'Release',
      'artist': 'Artist',
      'year': 2020,
      'release_date': '2024-04-19',
      'tracks': []
    });
    final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
    addTearDown(player.dispose);
    addTearDown(api.api.client.close);
    await tester.pumpWidget(MaterialApp(
        home: CatalogDetail(
            api: api,
            id: 'release',
            kind: 'album',
            title: 'Release',
            player: player,
            onLike: (_) async {},
            onOpen: (_, __, ___) {},
            onBack: () {})));
    await tester.pumpAndSettle();
    expect(find.textContaining('Released 19 Apr 2024'), findsOneWidget);
    await tester.scrollUntilVisible(
        find.text('Release date'.toUpperCase()), 200,
        scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
    expect(find.text('19 Apr 2024'), findsOneWidget);
    expect(find.textContaining('2020'), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });
  for (final width in [320.0, 390.0, 1440.0]) {
    testWidgets(
        'album sleeve and long credits fit ${width.toInt()}px with accessible actions',
        (tester) async {
      tester.view.physicalSize = Size(width, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = _catalogApi({
        'title': 'An exceptionally long album title that still deserves room',
        'artist': 'A long artist credit and another collaborator',
        'year': 2024,
        'tracks': [
          {
            'id': 'recording:one',
            'title': 'A song with a long title',
            'artist': 'The artist',
            'duration': 243
          }
        ]
      });
      final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
      addTearDown(player.dispose);
      addTearDown(api.api.client.close);
      await tester.pumpWidget(MaterialApp(
          home: MediaQuery(
              data: MediaQueryData(
                  size: Size(width, 900),
                  disableAnimations: true,
                  textScaler: TextScaler.linear(width == 320 ? 1.5 : 1)),
              child: CatalogDetail(
                  api: api,
                  id: 'album:long',
                  kind: 'album',
                  title: 'Album',
                  player: player,
                  onLike: (_) async {},
                  onOpen: (_, __, ___) {},
                  onBack: () {}))));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.scrollUntilVisible(find.text('Play album'), 200,
          scrollable: find.byType(Scrollable).first);
      expect(find.text('Play album'), findsOneWidget);
      await tester.scrollUntilVisible(
          find.text('A song with a long title'), 200,
          scrollable: find.byType(Scrollable).first);
      expect(find.byTooltip('Like A song with a long title'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('RUNNING TIME'), 200,
          scrollable: find.byType(Scrollable).first);
      expect(find.text('4:03'), findsWidgets);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    });
  }
  for (final width in [390.0, 1440.0]) {
    testWidgets('500-track detail builds only its visible rows at $width',
        (tester) async {
      tester.view.physicalSize = Size(width, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = _catalogApi({
        'title': 'Generated',
        'artist': 'Artist',
        'tracks': [
          for (var i = 0; i < 500; i++)
            {
              'id': 'generated:$i',
              'title': 'Generated song $i',
              'artist': 'Artist',
              'duration': 180
            }
        ]
      });
      final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
      addTearDown(player.dispose);
      addTearDown(api.api.client.close);
      await tester.pumpWidget(MaterialApp(
          home: CatalogDetail(
              api: api,
              id: 'large',
              kind: 'album',
              title: 'Generated',
              player: player,
              onLike: (_) async {},
              onOpen: (_, __, ___) {},
              onBack: () {})));
      await tester.pumpAndSettle();
      final initial = find.byType(BrowseTrackRow).evaluate().length;
      expect(initial, lessThan(30));
      expect(find.text('Generated song 499'), findsNothing);
      final scrollable =
          tester.state<ScrollableState>(find.byType(Scrollable).first);
      scrollable.position.jumpTo(scrollable.position.maxScrollExtent);
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(find.text('Generated song 499'), 200,
          scrollable: find.byType(Scrollable).first, maxScrolls: 100);
      expect(find.byType(BrowseTrackRow).evaluate().length, lessThan(35));
      expect(tester.takeException(), isNull);
      debugPrint('RESOURCE_ROWS width=$width initial=$initial total=500');
      await tester.pumpWidget(const SizedBox());
    });
  }
  testWidgets('artist detail omits empty biography and keeps album fallback',
      (tester) async {
    final api = _catalogApi({
      'id': 'artist:1',
      'name': 'Known Artist',
      'description': '',
      'albums': [
        {'id': 'album:1', 'title': 'First Album', 'artist': 'Known Artist'}
      ],
      'top_songs': [],
    });
    addTearDown(api.api.client.close);
    final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
    addTearDown(player.dispose);
    await tester.pumpWidget(MaterialApp(
        home: CatalogDetail(
      api: api,
      id: 'artist:1',
      kind: 'artist',
      title: 'Known Artist',
      player: player,
      onLike: (_) async {},
      onOpen: (_, __, ___) {},
      onBack: () {},
    )));
    await tester.pumpAndSettle();
    expect(find.text('About'), findsNothing);
    await tester.scrollUntilVisible(find.text('First Album'), 240,
        scrollable: find.byType(Scrollable).first);
    expect(find.text('First Album'), findsOneWidget);
    expect(find.text('Known Artist'), findsWidgets);
  });

  testWidgets('artist detail shows expanded full biography', (tester) async {
    const biography =
        'A full biography with readable long-form artist history.';
    final api = _catalogApi({
      'id': 'artist:1',
      'name': 'Known Artist',
      'description': 'Short description.',
      'full_description': biography,
      'top_songs': [],
    });
    addTearDown(api.api.client.close);
    final player = WebPlayer(api.api, _FakeAudio(), _FakeStore());
    addTearDown(player.dispose);
    await tester.pumpWidget(MaterialApp(
        home: CatalogDetail(
      api: api,
      id: 'artist:1',
      kind: 'artist',
      title: 'Known Artist',
      player: player,
      onLike: (_) async {},
      onOpen: (_, __, ___) {},
      onBack: () {},
    )));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('About'), 240,
        scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('About'));
    await tester.pumpAndSettle();
    expect(find.text(biography), findsOneWidget);
  });
}

CatalogApi _catalogApi(Map<String, dynamic> body) => CatalogApi(MusicApi(
      Uri.parse('https://music.test'),
      client: MockClient((_) async => http.Response(jsonEncode(body), 200)),
    ));

class _FakeAudio extends PlaybackPort {
  @override
  PlaybackPhase get phase => PlaybackPhase.paused;
  final _events = StreamController<String>.broadcast();
  @override
  Stream<String> get events => _events.stream;
  @override
  double get position => 0;
  @override
  double get duration => 0;
  @override
  bool get paused => true;
  @override
  String? get loadedTrackId => null;
  @override
  bool get sourceMatches => false;
  @override
  void syncMetadata(Map<String, dynamic> track) {}
  @override
  void load(Uri uri, Map<String, dynamic> track, {String? mimeType}) {}
  @override
  void reloadSource() {}
  @override
  Future<void> play() async {}
  @override
  void pause() {}
  @override
  void seek(double seconds) {}
  @override
  void dispose() => _events.close();
}

class _FakeStore implements SessionStore {
  @override
  String? read() => null;
  @override
  void write(String value) {}
}
