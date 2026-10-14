import '../logic/search_semantics.dart';
import '../logic/track_metadata.dart';
import '../logic/charts_provider.dart' show ChartQuery, ChartsSnapshot;
import '../logic/new_releases_provider.dart' show NewReleasesSnapshot;
import 'music_api.dart';

/// Thin catalog client for the backend's canonical search, detail, and Home
/// contracts. It intentionally keeps feed and entity admission on the backend.
class CatalogApi {
  CatalogApi(this.api);

  final MusicApi api;

  Future<ChartsSnapshot> charts(ChartQuery query) async =>
      ChartsSnapshot.fromJson(await api.get(
          api.endpoint('/charts').replace(queryParameters: {
            'country': query.country,
            'range': query.range,
            'source': query.source,
            'limit': '60',
          }).toString(),
          timeout: const Duration(seconds: 12)));

  Future<NewReleasesSnapshot> newReleases() async =>
      NewReleasesSnapshot.fromJson(await api.get('/new_releases?limit=120',
          timeout: const Duration(seconds: 12)));

  Future<Map<String, dynamic>> search(
    String query, {
    bool autoCorrect = true,
    int offset = 0,
    Map<String, dynamic>? continuation,
    bool preview = false,
    Future<void>? cancel,
  }) async {
    final normalizedQuery = query.trim();
    final payload = await api.post(
        '/search',
        {
          ...?continuation,
          'query': normalizedQuery,
          // Kept for compatibility with the current web search request. The
          // canonical backend retains correction and resolution in its response.
          'auto_correct': preview ? false : autoCorrect,
          'search_mode': classifySearchQueryMode(normalizedQuery),
          'limit': preview ? 8 : 24,
          if (preview) 'search_preview': true,
          'user_scope_id': api.userScopeId,
          'context_surface': 'interactive_search',
          'defer_side_surfaces': preview,
          'offset': offset < 0 ? 0 : offset,
        },
        cancel: cancel);
    return _withNormalizedTrackLists(payload);
  }

  Future<List<Map<String, dynamic>>> suggest(String query,
      {List<String> recentQueries = const [],
      List<Map<String, dynamic>> recentTracks = const [],
      Future<void>? cancel}) async {
    final result = await api.post(
        '/suggest',
        {
          'query': query.trim(),
          'limit': 5,
          'user_scope_id': api.userScopeId,
          'context_surface': 'suggest',
          'recent_queries': recentQueries.take(8).toList(),
          'recent_tracks': recentTracks.take(8).map(_playMetadata).toList(),
          'force_refresh': false,
        },
        timeout: const Duration(seconds: 2),
        cancel: cancel);
    return (result['suggestions'] as List? ??
            result['results'] as List? ??
            const [])
        .map((value) => value is Map
            ? {
                ...Map<String, dynamic>.from(value),
                'text': value['text'] ?? value['query'] ?? ''
              }
            : {'text': '$value'})
        .where((value) => '${value['text'] ?? ''}'.trim().isNotEmpty)
        .toList();
  }

  Future<List<Map<String, dynamic>>> recentSearchPicks() async {
    if (api.userScopeId == 'guest') return [];
    final result = await _get(
            '/recent_search_picks?user_scope_id=${Uri.encodeQueryComponent(api.userScopeId)}&limit=12')
        .timeout(const Duration(seconds: 2));
    return (result['picks'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  Future<Map<String, dynamic>> lyrics(Map<String, dynamic> track,
      {bool refresh = false}) async {
    final id = extractTrackId(track);
    if (id == null) return {'lyrics_status': 'not_found', 'lines': []};
    final source = track['playback'];
    final query = <String, String>{
      'title': '${track['title'] ?? ''}',
      'artist': '${track['artist'] ?? track['channel'] ?? ''}',
      'album': '${track['album'] ?? ''}',
      'duration': '${trackDurationSeconds(track).round()}',
      if ((source is Map
              ? source['source_id']
              : track['videoId'] ?? track['source_id'])
          case final value?)
        'source_id': '$value',
      if (refresh) 'force_refresh': 'true',
    };
    return _get(api
        .endpoint('/lyrics/${Uri.encodeComponent(id)}')
        .replace(queryParameters: query)
        .toString());
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
      for (final entry in _compactHomeSignals(signals).entries)
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

  // Feed retrieval needs identity/display evidence, never embedded collections,
  // expiring stream URLs or the same recent list serialized twice.
  static Map<String, dynamic> _compactHomeSignals(
      Map<String, dynamic> signals) {
    return {
      for (final entry in signals.entries)
        if (entry.key != 'last_played_tracks' ||
            signals['recent_track_snapshots'] == null)
          entry.key: const {
            'recent_track_snapshots',
            'top_track_snapshots',
            'last_played_tracks'
          }.contains(entry.key)
              ? MusicApi.tracks(entry.value)
                  .take(16)
                  .map(_playMetadata)
                  .toList()
              : entry.value,
    };
  }

  Future<Map<String, dynamic>> history(
          {List<String> pendingPlayKeys = const []}) =>
      api.post('/history_seed', {
        'user_scope_id': api.homeRequestScopeId,
        'limit': 32,
        if (pendingPlayKeys.isNotEmpty)
          'pending_play_keys': pendingPlayKeys.take(512).toList(),
      });

  Future<void> recordPlay(Map<String, dynamic> track,
      {required String scope,
      required double occurredAt,
      String? playKey}) async {
    final id = extractTrackId(track);
    if (id == null || scope != api.homeRequestScopeId) return;
    final response = await api.post('/interaction_event', {
      'user_scope_id': scope,
      'track_id': id,
      'event_type': 'play',
      'source': 'web_playback',
      'occurred_at': occurredAt,
      'artist_name': track['artist'] ?? track['channel'],
      'metadata': {
        ..._playMetadata(track),
        if (playKey != null) 'web_play_key': playKey,
      },
    });
    if (response['stored'] != true) {
      throw StateError('Listening event was not acknowledged.');
    }
  }

  static Map<String, dynamic> _playMetadata(Map<String, dynamic> track) => {
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
          'musicbrainz_recording_id',
          'canonical_entity_id',
          'play_count',
          'last_played_at',
          'playable',
          'source_verified'
        ])
          if (track[key] != null) key: track[key],
        if (track['playback'] is Map)
          'playback': {
            for (final key in const ['provider', 'source_id'])
              if ((track['playback'] as Map)[key] != null)
                key: (track['playback'] as Map)[key],
          },
      };

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
    return _withNormalizedTrackLists(await api.get(path));
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
