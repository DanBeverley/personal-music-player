import 'package:auralis_engine/logic/recommendation_feed_models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('home feed payload removes retired rows and diagnostics', () {
    final state = RecommendationFeedState.fromHomeJson({
      'session_id': 'session-1',
      'rows': [
        {
          'id': 'continue_listening',
          'kind': 'continue_listening',
          'item_type': 'track',
          'items': [
            {'id': 'track-1', 'title': 'Active'},
          ],
        },
        {
          'id': 'listeners_like_you',
          'kind': 'listeners_like_you',
          'item_type': 'track',
          'items': [
            {'id': 'track-2', 'title': 'Retired'},
          ],
        },
      ],
      'diagnostics': {
        'row_status': {
          'continue_listening': {'status': 'emitted'},
          'listeners_like_you': {'status': 'emitted'},
        },
        'row_order': ['continue_listening', 'listeners_like_you'],
        'deferred_row_kinds': ['listeners_like_you'],
      },
    });

    expect(state.rows.map((row) => row.kind), ['continue_listening']);
    expect(state.diagnostics['row_order'], ['continue_listening']);
    expect(state.diagnostics['deferred_row_kinds'], isEmpty);
    expect(
      state.diagnostics['row_status'],
      {
        'continue_listening': {'status': 'emitted'},
      },
    );
  });

  test('active home row parser rejects inactive row payloads', () {
    expect(
      activeHomeFeedRowFromJson({
        'id': 'trending_for_you',
        'kind': 'trending_for_you',
      }),
      isNull,
    );
    expect(
      activeHomeFeedRowFromJson({
        'id': 'quiet_picks',
        'kind': 'quiet_picks',
        'item_type': 'track',
        'items': [
          {'id': 'track-1', 'title': 'Quiet'},
        ],
      })?.kind,
      'quiet_picks',
    );
  });
}
