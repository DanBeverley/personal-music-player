import 'dart:convert';
import 'package:http/http.dart' as http;
import '../logic/track_metadata.dart';

class MusicApi {
  final Uri base;
  final http.Client client;
  DateTime Function() clock = DateTime.now;
  String _userScopeId = 'guest';
  /// Optional feed-only scope. Search and playback discovery keep userScopeId.
  String? homeScopeId;
  String get homeRequestScopeId => userScopeId.toLowerCase() == 'guest'
      ? homeScopeId ?? userScopeId
      : userScopeId;
  String get userScopeId => _userScopeId;
  set userScopeId(String value) {
    final next = value.trim().isEmpty ? 'guest' : value.trim();
    if (next != _userScopeId) clearSearchContext();
    _userScopeId = next;
  }
  String? _searchContextQuery;
  String? _searchContextScope;
  DateTime? _searchContextAt;
  static const searchContextLifetime = Duration(minutes: 10);
  String? Function()? accessToken;
  void Function(String, Map<String, dynamic>)? onDiagnostic;
  MusicApi(this.base, {http.Client? client}) : client = client ?? http.Client();

  Uri endpoint(String path) => base.resolve(path);

  void setManualSearchContext(String query) {
    final value = query.trim();
    if (value.isEmpty) {
      clearSearchContext();
      return;
    }
    _searchContextQuery = value;
    _searchContextScope = userScopeId;
    _searchContextAt = clock();
  }

  void clearSearchContext() {
    _searchContextQuery = null;
    _searchContextScope = null;
    _searchContextAt = null;
  }

  /// Best-effort learning for a directly chosen track. Called only from
  /// explicit start/startQueue actions; queue advancement never calls it.
  Future<void> recordExplicitSearchSelection(Map<String, dynamic> track) async {
    final query = _searchContextQuery?.trim() ?? '';
    final createdAt = _searchContextAt;
    final sameScope = _searchContextScope == userScopeId;
    final fresh = createdAt != null && clock().difference(createdAt) <= searchContextLifetime;
    // A context represents one recent search session and is consumed by the
    // next explicit selection, including a selection that fails relevance.
    clearSearchContext();
    if (query.isEmpty || !sameScope || !fresh) {
      return;
    }
    final title = (track['title'] ?? '').toString().trim();
    final artist = (track['artist'] ?? track['channel'] ?? track['author'] ?? '').toString().trim();
    final id = (track['id'] ?? track['videoId'] ?? track['track_key'] ?? '').toString().trim();
    final recordingId = (track['canonical_recording_id'] ?? track['recording_id'] ?? track['musicbrainz_recording_id'] ?? '').toString().trim();
    if (userScopeId.toLowerCase() == 'guest' || title.isEmpty || artist.isEmpty ||
        (id.isEmpty && recordingId.isEmpty) || !_queryRelatesToTrack(query, title, artist)) return;
    await post('/search_interaction', {
      'query': query,
      'user_scope_id': userScopeId,
      'source': 'web_search_selection',
      'result_count': 0,
      'metadata': {
        'selected_entity_type': 'track',
        'selected_item': {
          if (id.isNotEmpty) 'id': id,
          if (recordingId.isNotEmpty) 'canonical_recording_id': recordingId,
          'title': title,
          'artist': artist,
        },
      },
    });
  }

  bool _queryRelatesToTrack(String query, String title, String artist) {
    String normalize(String value) => value.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), ' ').trim();
    final normalizedQuery = normalize(query);
    final normalizedItem = normalize('$title $artist');
    if (normalizedQuery.isEmpty || normalizedItem.isEmpty) return false;
    // Require a meaningful shared word; this suppresses obviously unrelated
    // Home/artist picks while allowing either a song-title or artist query.
    final tokens = normalizedQuery.split(RegExp(r'\s+')).where((token) => token.length > 1).toSet();
    return tokens.any((token) => normalizedItem.split(RegExp(r'\s+')).contains(token));
  }

  /// Backend artwork may be a gateway-relative path. Resolve it against the
  /// configured API origin so the browser does not request it from the page
  /// host when the API is hosted separately.
  Uri? artworkUri(dynamic rawArtwork) {
    final value = rawArtwork?.toString().trim() ?? '';
    if (value.isEmpty) return null;
    try {
      final parsed = Uri.parse(value);
      final resolved = parsed.hasScheme ? parsed : base.resolveUri(parsed);
      if (!const {'http', 'https'}.contains(resolved.scheme) ||
          resolved.host.isEmpty) {
        return null;
      }
      return resolved;
    } on FormatException {
      return null;
    }
  }

  Future<Map<String, dynamic>> post(
      String path, Map<String, dynamic> body,
      {Duration timeout = const Duration(seconds: 30)}) async {
    final timer = Stopwatch()..start();
    late http.Response response;
    try {
      response = await client
        .post(endpoint(path),
            headers: {'Content-Type': 'application/json',
              if (accessToken?.call() case final String token) 'Authorization': 'Bearer $token'},
            body: jsonEncode(body))
        .timeout(timeout);
    } catch (e) {
      onDiagnostic?.call('api-failure', {'path': path, 'ms': timer.elapsedMilliseconds,
        'requestedTrack': body['current_track_key'], 'errorType': e.runtimeType.toString()});
      rethrow;
    }
    onDiagnostic?.call('api-response', {
      'path': path, 'status': response.statusCode, 'ms': timer.elapsedMilliseconds,
    });
    if (response.statusCode != 200) {
      throw StateError(
          'Music service returned ${response.statusCode}. Please retry.');
    }
    return Map<String, dynamic>.from(jsonDecode(response.body) as Map);
  }

  Map<String, dynamic>? queryCorrection;
  Future<List<Map<String, dynamic>>> search(String query, {bool autoCorrect = true}) async {
    final result = await post('/search', {
      'query': query,
      'auto_correct': autoCorrect,
      'limit': 24,
      'user_scope_id': userScopeId,
      'defer_side_surfaces': true,
      'context_surface': 'interactive_search',
    });
    queryCorrection = result['query_correction'] is Map
        ? Map<String, dynamic>.from(result['query_correction']) : null;
    return tracks(result['results']);
  }

  Map<String, dynamic> identity(Map<String, dynamic> track) => {
        'track_key': extractTrackId(track),
        'title': track['title'] ?? '',
        'artist': track['artist'] ?? track['author'] ?? track['channel'] ?? '',
        'album': track['album'] ?? track['album_title'] ?? '',
        'duration': trackDurationSeconds(track),
        'source_id': extractPlaybackSourceId(track) ?? '',
      };

  Future<Uri> resolve(Map<String, dynamic> track) async {
    final result = await post('/playback/resolve', identity(track));
    final path = result['url']?.toString() ?? '';
    if (path.isEmpty) throw StateError('No playable source is available.');
    final uri = base.resolve(path);
    if (!['http', 'https'].contains(uri.scheme) || uri.origin != base.origin) {
      throw StateError(
          'The music service returned an unexpected stream origin.');
    }
    return uri;
  }

  Uri streamUri(Map<String, dynamic> track) {
    final data = identity(track);
    final id = data.remove('track_key') as String;
    return endpoint('/playback/stream/${Uri.encodeComponent(id)}').replace(
        queryParameters: {'resolve': '1',
          for (final entry in data.entries) entry.key: entry.value.toString()});
  }

  Future<Uri> prepare(Map<String, dynamic> track, {bool activeQueue = false, bool cacheFull = false}) async {
    final id = extractTrackId(track)!;
    final result = await post('/prepare_session', {
      'track_keys': [id], 'current_track_key': id,
      'active_queue': activeQueue, 'lookahead': 1,
      'cache_full': cacheFull,
    }, timeout: const Duration(seconds: 90));
    onDiagnostic?.call('prepare-result', {'requestedTrack': id,
      'prepared': (result['prepared'] as Map?)?[id] != null,
      'fullyCached': ((result['prepared'] as Map?)?[id] as Map?)?['fully_cached'] == true,
      'failed': (result['failed_ids'] as List?)?.contains(id) == true});
    if ((result['prepared'] as Map?)?[id] == null) {
      throw StateError('Next track is not prepared yet.');
    }
    return streamUri(track);
  }

  Future<({List<Map<String, dynamic>> tracks, bool hasMore})> related(
      Map<String, dynamic> seed, List<Map<String, dynamic>> queue, {
      String discoverySession = '', List<String> recentIds = const []}) async {
    final result = await post('/related_tracks', {
      ...identity(seed),
      'avoid_ids': queue.map(extractTrackId).whereType<String>().toList(),
      'limit': 18,
      'user_scope_id': userScopeId,
      if (discoverySession.isNotEmpty) 'discovery_session': discoverySession,
      'recent_ids': recentIds.take(40).toList(),
    });
    if (result['status'] == 'temporarily_unavailable') {
      throw StateError(
          'Related songs are temporarily unavailable. Retry to continue.');
    }
    return (
      tracks: tracks(result['tracks']),
      hasMore: result['has_more'] == true
    );
  }

  static List<Map<String, dynamic>> tracks(dynamic raw) =>
      (raw is List ? raw : const [])
          .whereType<Map>()
          .map((v) => normalizeTrack(Map<String, dynamic>.from(v)))
          .where((v) => extractTrackId(v) != null)
          .toList();
}
