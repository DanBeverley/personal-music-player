import 'package:auralis_engine/widgets/home/track_menu_button.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('TrackMenuButton opens the action sheet',
      (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        child: MaterialApp(
          home: Scaffold(
            body: TrackMenuButton(
              track: const {
                'id': 'track-1',
                'videoId': 'track-1',
                'title': 'Dreams',
                'author': 'Fleetwood Mac',
              },
              onOpenDetails: () {},
              onAddToPlaylist: (_) async {},
              onStartStation: (_) async {},
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.byType(IconButton));
    await tester.pumpAndSettle();

    expect(find.text('Play next'), findsOneWidget);
    expect(find.text('Play last'), findsOneWidget);
    expect(find.text('Add to playlist'), findsOneWidget);
    expect(find.text('Start station'), findsOneWidget);
    expect(find.text('Download'), findsOneWidget);
    expect(find.text('View details'), findsOneWidget);
  });
}
