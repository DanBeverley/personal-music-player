import 'dart:convert';

import '../logic/search_semantics.dart';
import '../logic/track_metadata.dart';
import 'music_api.dart';

/// Thin catalog client for the backend's canonical search, detail, and Home
/// contracts. It intentionally keeps feed and entity admission on the backend.
class CatalogApi {
  CatalogApi(this.api);

  final MusicApi api;

  Future<Map<String, dynamic>> search(
    String query, {
    bool autoCorrect = true,
    int offset = 0,
    Map<String, dynamic>? continuation,
  }) async {
    final normalizedQuery = query.trim();
    final payload = await api.post('/search', {
      ...?continuation,
      'query': normalizedQuery,
      // Kept for compatibility with the current web search request. The
      // canonical backend retains correction and resolution in its response.
      'auto_correct': autoCorrect,
      'search_mode': classifySearchQueryMode(normalizedQuery),
      'limit': 24,
      'user_scope_id': api.userScopeId,
      'context_surface': 'interactive_search',
      'defer_side_surfaces': false,
      'offset': offset < 0 ? 0 : offset,
    });
    return _withNormalizedTrackLists(payload);
  }

  Future<Map<String, dynamic>> album(String id) =>
      _get('/album/${Uri.encodeComponent(id)}');

  Future<Map<String, dynamic>> artist(String id) =>
      _get('/artist/${Uri.encodeComponent(id)}');

  static const _homeSignalKeys = <String>{
    'client_signal_tier',
    'fresh_account_empty_home',
    'recent_track_ids',
    'top_track_ids',
    'recent_track_snapshots',
    'top_track_snapshots',
    'last_played_tracks',
    'recent_queries',
    'taste_queries',
    'playlist_names',
    'library_track_ids',
    'offline_track_ids',
    'artist_hints',
    'album_hints',
    'seed_id',
    'seed_ids',
    'avoid_ids',
  };

  Future<Map<String, dynamic>> home({
    bool refresh = false,
    bool prepare = false,
    String launchToken = '',
    Map<String, dynamic> signals = const {},
  }) async {
    final payload = await api.post('/recommend', {
      for (final entry in signals.entries)
        if (_homeSignalKeys.contains(entry.key)) entry.key: entry.value,
      'query': '',
      'user_scope_id': api.homeRequestScopeId,
      'force_refresh': refresh,
      if (prepare) 'prepare_next_session': true,
      if (launchToken.isNotEmpty && !refresh && !prepare) ...{
        'promote_ready_on_launch': true,
        'launch_token': launchToken,
      },
      if (refresh) 'feed_refresh_wait_ms': 8000,
    });
    return _withNormalizedTrackLists(payload);
  }

  Future<Map<String, dynamic>> history() => api.post('/history_seed', {
        'user_scope_id': api.homeRequestScopeId,
        'limit': 32,
      });

  Future<void> recordPlay(Map<String, dynamic> track,
      {required String scope, required double occurredAt}) async {
    final id = extractTrackId(track);
    if (id == null || scope != api.homeRequestScopeId) return;
    await api.post('/interaction_event', {
      'user_scope_id': scope,
      'track_id': id,
      'event_type': 'play',
      'source': 'web_playback',
      'occurred_at': occurredAt,
      'artist_name': track['artist'] ?? track['channel'],
      'metadata': {
        for (final key in const [
          'id',
          'track_key',
          'title',
          'artist',
          'channel',
          'album',
          'album_id',
          'thumbnail',
          'duration',
          'videoId',
          'source_id',
          'canonical_recording_id',
          'musicbrainz_recording_id'
        ])
          if (track[key] != null) key: track[key],
        if (track['playback'] is Map)
          'playback': {
            for (final key in const ['provider', 'source_id'])
              if ((track['playback'] as Map)[key] != null)
                key: (track['playback'] as Map)[key],
          },
      },
    });
  }

  Future<Map<String, dynamic>> homeQueueStatus(
      {int revision = 0, bool wait = true}) async {
    final payload = await api.post(
        '/recommend',
        {
          'query': '',
          'limit': 0,
          'offset': 0,
          'user_scope_id': api.homeRequestScopeId,
          'feed_queue_status_only': true,
          'feed_queue_revision': revision < 0 ? 0 : revision,
          'feed_queue_wait_ms': wait ? 9000 : 0,
        },
        timeout: const Duration(seconds: 12));
    return _withNormalizedTrackLists(payload);
  }

  Future<Map<String, dynamic>> homeRow(
          String session, String row, int offset) =>
      api.post('/recommend', {
        'query': '',
        'user_scope_id': api.homeRequestScopeId,
        'session_id': session,
        'row_id': row,
        'offset': offset,
        'limit': 20,
      });

  Future<Map<String, dynamic>> _get(String path) async {
    final response = await api.client.get(api.endpoint(path), headers: {
      if (api.accessToken?.call() case final String token)
        'Authorization': 'Bearer $token',
    }).timeout(const Duration(seconds: 30));
    if (response.statusCode != 200) {
      throw StateError(
          'Music service returned ${response.statusCode}. Please retry.');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Music service returned an invalid payload.');
    }
    return _withNormalizedTrackLists(Map<String, dynamic>.from(decoded));
  }

  static Map<String, dynamic> _withNormalizedTrackLists(
    Map<String, dynamic> payload,
  ) {
    final normalized = Map<String, dynamic>.from(payload);
    for (final key in const [
      'results',
      'tracks',
      'top_result',
      'similar_tracks',
      'artist_tracks',
    ]) {
      final value = payload[key];
      if (value is List) {
        normalized[key] = value.map((item) {
          return item is Map ? normalizeTrack(item) : item;
        }).toList(growable: false);
      } else if (key == 'top_result' && value is Map) {
        final topResult = Map<String, dynamic>.from(value);
        final item = topResult['item'];
        if (topResult['entity_type'] == 'track' && item is Map) {
          topResult['item'] = normalizeTrack(Map<String, dynamic>.from(item));
        }
        normalized[key] = topResult;
      }
    }
    return normalized;
  }
}
