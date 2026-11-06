import 'package:auralis_engine/logic/charts_provider.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('chart query equality is stable for provider family caching', () {
    expect(
      const ChartQuery(country: 'US', range: 'week', source: 'all'),
      const ChartQuery(country: 'US', range: 'week', source: 'all'),
    );
    expect(
      const ChartQuery(country: 'US'),
      isNot(const ChartQuery(country: 'GB')),
    );
  });

  test('snapshot keeps verified playable entries and rank history', () {
    final snapshot = ChartsSnapshot.fromJson(<String, dynamic>{
      'status': 'ready',
      'country': 'GLOBAL',
      'range': 'week',
      'source': 'all',
      'generated_at': 1000,
      'refreshing': true,
      'total_count': 52,
      'entries': <Map<String, dynamic>>[
        <String, dynamic>{
          'rank': 1,
          'previous_rank': 4,
          'rank_change': 3,
          'is_new_entry': false,
          'title': 'New Song',
          'artist': 'New Artist',
          'thumbnail': 'https://images.example/song.jpg',
          'playable_source_id': 'abc12345678',
          'playable': true,
          'canonical_track_key': 'musicbrainz:recording:recording-id',
          'chart_providers': <String>['listenbrainz', 'lastfm'],
          'listen_count': 12000,
          'history': <Map<String, dynamic>>[
            <String, dynamic>{'at': 900, 'rank': 4},
            <String, dynamic>{'at': 1000, 'rank': 1},
          ],
        },
        <String, dynamic>{
          'rank': 2,
          'title': 'Unresolved Song',
          'artist': 'Unresolved Artist',
          'playable': false,
        },
      ],
    });

    expect(snapshot.entries, hasLength(1));
    expect(snapshot.isRefreshing, isTrue);
    expect(snapshot.totalCount, 52);
    final entry = snapshot.entries.single;
    expect(entry.rankChange, 3);
    expect(entry.isNewEntry, isFalse);
    expect(entry.history.map((point) => point.rank), <int>[4, 1]);
    expect(entry.providers, <String>['listenbrainz', 'lastfm']);
    expect(entry.track['id'], 'abc12345678');
    expect(
      entry.track['canonical_track_key'],
      'musicbrainz:recording:recording-id',
    );
  });
}
