import 'package:auralis_engine/screens/personal_mix_detail_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('PersonalMixDetailScreen renders mix metadata and tracks',
      (tester) async {
    await tester.pumpWidget(
      const ProviderScope(
        child: MaterialApp(
          home: PersonalMixDetailScreen(
            mix: {
              'title': 'Late Night Mix',
              'subtitle': 'Built around your repeats',
              'description': 'A focused playlist for slower evenings.',
              'badge': 'CURATED',
            },
            tracks: [
              {
                'id': 'track-1',
                'videoId': 'track-1',
                'title': 'Midnight City',
                'author': 'M83',
                'duration': 250,
              },
              {
                'id': 'track-2',
                'videoId': 'track-2',
                'title': 'Space Song',
                'author': 'Beach House',
                'duration': 320,
              },
            ],
            playerScreenBuilder: _placeholderBuilder,
            trackDetailsScreenBuilder: _placeholderTrackBuilder,
          ),
        ),
      ),
    );

    expect(find.text('Late Night Mix'), findsAtLeastNWidgets(1));
    expect(find.text('Built around your repeats'), findsOneWidget);
    expect(find.text('A focused playlist for slower evenings.'), findsOneWidget);
    expect(find.text('Midnight City'), findsOneWidget);
    expect(find.text('Space Song'), findsOneWidget);
  });
}

Widget _placeholderBuilder(BuildContext context) => const SizedBox.shrink();

Widget _placeholderTrackBuilder(Map<String, dynamic> track) =>
    const SizedBox.shrink();
