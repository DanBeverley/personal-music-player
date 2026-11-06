import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../logic/assistant_identity.dart';
import '../logic/track_metadata.dart';

class SongDiscussionException implements Exception {
  const SongDiscussionException(this.status);
  final int status;
  String get message => switch (status) {
        401 || 403 => 'Please sign in again to ask about this song.',
        429 => 'The song assistant is busy. Try again in a moment.',
        _ => 'The song assistant is unavailable. Please try again later.',
      };
  @override
  String toString() => message;
}

class PlaybackPreparationException extends StateError {
  final String code;
  PlaybackPreparationException(this.code) : super('Audio preparation: $code');
  @override
  String toString() => 'PlaybackPreparationException($code)';
}

class MusicApi {
  final Uri base;
  final Uri mediaBase;
  final Uri? storageBase;
  final http.Client client;
  DateTime Function() clock = DateTime.now;
  // Enabled by the browser shell; optional metadata never gates a catalog result
  // or the native Play call. Byte warming remains owned by queue preparation.
  bool catalogMediaHints = false;
  String preferredCachedVariant = '';
  bool _loadingCachedMedia = false;
  Completer<void>? _cachedMediaAbort;
  final Set<String> _cachedMediaQueue = {};
  final Set<String> _priorityMediaQueue = {};
  final Set<String> _preparingMedia = {};
  final Map<(String, bool), Future<Uri>> _preparationTasks = {};
  Timer? _startupHold;
  int _startupTicket = 0;

  /// Only optional cache hints wait; native Play and interactive API calls do not.
  int beginPlaybackStartup() {
    final ticket = ++_startupTicket;
    _startupHold?.cancel();
    _startupHold =
        Timer(const Duration(seconds: 2), () => endPlaybackStartup(ticket));
    final pending = _cachedMediaAbort;
    if (pending != null && !pending.isCompleted) pending.complete();
    return ticket;
  }

  void endPlaybackStartup(int? ticket, {bool resume = true}) {
    if (ticket == null || ticket != _startupTicket) return;
    _startupHold?.cancel();
    _startupHold = null;
    if (resume) unawaited(_loadCachedMedia());
  }

  final Map<String, DateTime> _cachedMediaRetryAfter = {};
  String _userScopeId = 'guest';
  int _scopeRevision = 0;
  final Map<String, DateTime> _preparationExpiry = {};
  DateTime? preparedUntil(String id) => _preparationExpiry[id];
  bool startupPrepared(String id) {
    final margin = clock().add(const Duration(seconds: 60));
    return (_preparationExpiry[id]?.isAfter(margin) ?? false) ||
        (preparedMedia(id)?.deliveryUntil?.isAfter(margin) ?? false);
  }
  final Map<String,
          ({Uri uri, String mimeType, DateTime until, double? duration,
            Uri? deliveryUri, DateTime? deliveryUntil})>
      _preparedMedia = {};
  ({Uri uri, String mimeType, DateTime until, double? duration,
    Uri? deliveryUri, DateTime? deliveryUntil})? preparedMedia(
      String id) {
    final value = _preparedMedia[id];
    if (preferredCachedVariant == 'opus-v1' &&
        value?.uri.queryParameters['cache_variant'] == 'aac-v1') {
      _preparedMedia.remove(id);
      return null;
    }
    // An immutable cached file has no expiring provider lease. Keep its proven
    // format while the metadata lane discovers a newer representation. Never
    // extend live-source/session URLs or a resource explicitly forgotten.
    final immutable =
        value?.uri.queryParameters.containsKey('cache_version') == true;
    if (value != null &&
        (immutable
            ? value.until.add(const Duration(hours: 12)).isAfter(clock())
            : value.until.isAfter(clock().add(const Duration(seconds: 60))))) {
      return value;
    }
    _preparedMedia.remove(id);
    return null;
  }

  void forgetPreparedMedia(String id) {
    _preparedMedia.remove(id);
    _preparationExpiry.remove(id);
  }

  void preloadCachedMedia(Iterable<Map<String, dynamic>> tracks,
      {bool priority = false}) {
    if (!catalogMediaHints) return;
    final previous = priority ? _cachedMediaQueue.toList() : <String>[];
    if (priority) {
      _cachedMediaQueue.clear();
      _priorityMediaQueue.clear();
    }
    for (final track in tracks.take(48)) {
      final id = extractTrackId(track);
      if (id == null ||
          id.length > 256 ||
          _preparingMedia.contains(id) ||
          track['playable'] == false ||
          preparedMedia(id) != null && !needsRepresentationRefresh(id) ||
          (_cachedMediaRetryAfter[id]?.isAfter(clock()) ?? false)) continue;
      if (_cachedMediaQueue.length >= 64) break;
      _cachedMediaQueue.add(id);
      if (priority) _priorityMediaQueue.add(id);
    }
    for (final id in previous) {
      if (_cachedMediaQueue.length >= 64) break;
      _cachedMediaQueue.add(id);
    }
    unawaited(_loadCachedMedia());
  }

  bool needsRepresentationRefresh(String id) {
    final media = preparedMedia(id);
    return storageBase != null && media != null &&
            const {'aac-v1', 'opus-v1'}.contains(media.uri.queryParameters['cache_variant']) &&
            (media.deliveryUri == null ||
                !media.deliveryUntil!.isAfter(clock().add(const Duration(seconds: 60)))) ||
        preferredCachedVariant == 'aac-v1' &&
            media?.uri.queryParameters.containsKey('cache_version') == true &&
            media?.uri.queryParameters['cache_variant'] != 'aac-v1' ||
        const {'legacy', 'seek-v1', 'seek-v2'}
            .contains(media?.uri.queryParameters['cache_variant']);
  }

  void _preloadCatalogMedia(Map<String, dynamic> payload) {
    var visited = 0;
    final tracks = <Map<String, dynamic>>[];
    void visit(dynamic value, int depth) {
      if (++visited > 512 || depth > 8 || tracks.length >= 48) return;
      if (value is List) {
        for (final item in value.take(32)) visit(item, depth + 1);
      } else if (value is Map<String, dynamic>) {
        final id = extractTrackId(value) ?? '';
        if (value['title'] is String &&
            (id.startsWith('recording:') ||
                id.startsWith('musicbrainz:recording:') ||
                RegExp(r'^[A-Za-z0-9_-]{11}$').hasMatch(id) ||
                value['playback'] is Map)) tracks.add(value);
        for (final entry in value.entries.take(24)) {
          if (entry.key != 'diagnostics' && entry.key != 'playback')
            visit(entry.value, depth + 1);
        }
      }
    }

    visit(payload, 0);
    preloadCachedMedia(tracks);
  }

  Future<void> _loadCachedMedia() async {
    if (_loadingCachedMedia ||
        _cachedMediaQueue.isEmpty ||
        _startupHold != null) return;
    _loadingCachedMedia = true;
    try {
      while (_cachedMediaQueue.isNotEmpty && _startupHold == null) {
        _cachedMediaQueue.removeWhere((id) =>
            _preparingMedia.contains(id) ||
            preparedMedia(id) != null && !needsRepresentationRefresh(id));
        _priorityMediaQueue
            .removeWhere((id) => !_cachedMediaQueue.contains(id));
        if (_cachedMediaQueue.isEmpty) break;
        final revision = _scopeRevision;
        final priority = _priorityMediaQueue.isNotEmpty;
        final ids = (priority ? _priorityMediaQueue : _cachedMediaQueue)
            .take(12)
            .toList();
        _cachedMediaQueue.removeAll(ids);
        _priorityMediaQueue.removeAll(ids);
        for (final id in ids) {
          if (_cachedMediaRetryAfter.length >= 256)
            _cachedMediaRetryAfter.remove(_cachedMediaRetryAfter.keys.first);
          _cachedMediaRetryAfter[id] = clock().add(const Duration(seconds: 60));
        }
        final remembered = {
          for (final id in ids) id: (_preparedMedia[id], _preparationExpiry[id])
        };
        final abort = _cachedMediaAbort = Completer<void>();
        try {
          final result = await post(
              '/prepare_session',
              {
                'track_keys': ids,
                'cached_only': true,
                if (preferredCachedVariant.isNotEmpty)
                  'preferred_cached_variant': preferredCachedVariant,
                if (priority) 'active_queue': true,
              },
              timeout: const Duration(seconds: 4),
              cancel: abort.future);
          if (revision != _scopeRevision) continue;
          _rememberCachedMedia(result['prepared'], ids.where((id) =>
              remembered[id] == (_preparedMedia[id], _preparationExpiry[id])));
        } catch (error) {
          if (revision == _scopeRevision &&
              error is http.RequestAbortedException) {
            // A startup pause is not a failed hint. Retain bounded queued intent.
            for (final id in ids) {
              _cachedMediaRetryAfter.remove(id);
              if (_cachedMediaQueue.length < 64 &&
                  !_preparingMedia.contains(id)) {
                _cachedMediaQueue.add(id);
                if (priority) _priorityMediaQueue.add(id);
              }
            }
          }
        } finally {
          if (identical(_cachedMediaAbort, abort)) _cachedMediaAbort = null;
        }
      }
    } finally {
      _loadingCachedMedia = false;
    }
  }

  void _rememberCachedMedia(dynamic prepared, Iterable<String> ids) {
    if (prepared is! Map) return;
    for (final id in ids.take(24)) {
      final data = prepared[id];
      if (data is! Map || data['cache_tier'] != 'r2_full') continue;
      final path = data['playback_path'], expiry = data['expires_at'];
      if (path is! String ||
          path.length > 1024 ||
          expiry is! num ||
          !expiry.isFinite ||
          expiry <= 0 ||
          expiry >
              clock().add(const Duration(days: 1)).millisecondsSinceEpoch /
                  1000) continue;
      final uri = base.resolve(path);
      // Only verified immutable files can supply format hints before Play.
      if (!uri.queryParameters.containsKey('cache_version')) continue;
      _rememberMedia(
          id,
          uri,
          '${data['mime_type'] ?? ''}'.split(';').first.trim().toLowerCase(),
          DateTime.fromMillisecondsSinceEpoch((expiry * 1000).round()),
          sourceDuration: data['duration'], delivery: data);
    }
  }

  bool _rememberMedia(String id, Uri uri, String mime, DateTime until,
      {dynamic sourceDuration, Map? delivery}) {
    final cached = RegExp(r'^v1-[a-f0-9]{32}$')
            .hasMatch(uri.queryParameters['cache_version'] ?? '') &&
        const {'legacy', 'seek-v1', 'seek-v2', 'opus-v1', 'aac-v1'}
            .contains(uri.queryParameters['cache_variant']) &&
        (mime != 'audio/mp4' ||
            const {'seek-v2', 'aac-v1'}
                .contains(uri.queryParameters['cache_variant'])) &&
        !uri.queryParameters.containsKey('playback_session') &&
        !uri.queryParameters.containsKey('stream_version');
    final pinned = RegExp(r'^[a-f0-9]{64}$')
            .hasMatch(uri.queryParameters['stream_version'] ?? '') &&
        RegExp(r'^[a-f0-9]{32}$')
            .hasMatch(uri.queryParameters['playback_session'] ?? '') &&
        !uri.queryParameters.containsKey('cache_version') &&
        !uri.queryParameters.containsKey('cache_variant') &&
        !until.isAfter(clock().add(const Duration(minutes: 21)));
    if (id.isEmpty ||
        id.length > 256 ||
        uri.origin != base.origin ||
        !uri.path.startsWith('/playback/stream/') ||
        uri.userInfo.isNotEmpty ||
        uri.fragment.isNotEmpty ||
        uri.queryParameters.keys.any((key) => !const {
              'resolve',
              'source_id',
              'cache_variant',
              'cache_version',
              'playback_session',
              'stream_version'
            }.contains(key)) ||
        !(cached || pinned) ||
        !const {
          'audio/mp4',
          'audio/webm',
          'audio/mpeg',
          'audio/ogg',
          'audio/wav'
        }.contains(mime) ||
        !until.isAfter(clock().add(const Duration(seconds: 60))) ||
        until.isAfter(clock().add(const Duration(days: 1)))) return false;
    final previous = _preparedMedia[id];
    if (_preparedMedia.length >= 256 && previous == null)
      _preparedMedia.remove(_preparedMedia.keys.first);
    if (previous != null && previous.uri != uri) {
      _preparationExpiry.remove(id);
    }
    // Keep a healthy capability's URL stable so returning to a recording can
    // reuse the browser's bytes. Metadata polling must not churn cache identity.
    final retained = previous != null && previous.uri == uri &&
        previous.mimeType == mime && previous.deliveryUri != null &&
        previous.deliveryUntil!.isAfter(clock().add(const Duration(seconds: 60)))
        ? (uri: previous.deliveryUri!, until: previous.deliveryUntil!) : null;
    final transport = cached ? retained ?? _directDelivery(uri, delivery) : null;
    _preparedMedia[id] = (
      uri: uri,
      mimeType: mime,
      until: until,
      duration:
          sourceDuration is num && sourceDuration.isFinite && sourceDuration > 0
              ? sourceDuration.toDouble()
              : null,
      deliveryUri: transport?.uri,
      deliveryUntil: transport?.until,
    );
    return true;
  }

  ({Uri uri, DateTime until})? _directDelivery(Uri canonical, Map? data) {
    if (storageBase == null || data == null ||
        data['delivery_version'] != canonical.queryParameters['cache_version'] ||
        !const {'aac-v1', 'opus-v1'}.contains(canonical.queryParameters['cache_variant'])) return null;
    final raw = data['delivery_url'], expiry = data['delivery_expires_at'];
    if (raw is! String || raw.length > 4096 || expiry is! num || !expiry.isFinite ||
        expiry * 1000 <= clock().add(const Duration(seconds: 60)).millisecondsSinceEpoch ||
        expiry * 1000 > clock().add(const Duration(days: 1, seconds: 2)).millisecondsSinceEpoch) return null;
    final uri = Uri.tryParse(raw);
    final until = DateTime.fromMillisecondsSinceEpoch((expiry * 1000).round());
    if (uri == null || uri.scheme != 'https' || uri.origin != storageBase!.origin ||
        uri.userInfo.isNotEmpty || uri.hasFragment ||
        !until.isAfter(clock().add(const Duration(seconds: 60))) ||
        until.isAfter(clock().add(const Duration(days: 1, seconds: 2)))) return null;
    final source = canonical.queryParameters['source_id'] ?? canonical.pathSegments.last;
    final variant = canonical.queryParameters['cache_variant'];
    final query = uri.queryParameters;
    final ttl = int.tryParse(query['X-Amz-Expires'] ?? '') ?? 0;
    final stamp = query['X-Amz-Date'] ?? '';
    if (!uri.path.endsWith('/$source.bin.$variant') ||
        query['response-content-disposition'] !=
            'inline; filename="${data['delivery_version']}.$variant"' ||
        query['X-Amz-Algorithm'] != 'AWS4-HMAC-SHA256' || ttl < 60 || ttl > 86400 ||
        !RegExp(r'^[a-f0-9]{64}$').hasMatch(query['X-Amz-Signature'] ?? '') ||
        !RegExp(r'^\d{8}T\d{6}Z$').hasMatch(stamp)) return null;
    final signedAt = DateTime.utc(int.parse(stamp.substring(0, 4)),
        int.parse(stamp.substring(4, 6)), int.parse(stamp.substring(6, 8)),
        int.parse(stamp.substring(9, 11)), int.parse(stamp.substring(11, 13)),
        int.parse(stamp.substring(13, 15)));
    final signedUntil = signedAt.add(Duration(seconds: ttl));
    if (until.difference(signedUntil).abs() > const Duration(seconds: 2)) return null;
    return (uri: uri, until: signedUntil.isBefore(until) ? signedUntil : until);
  }

  List<Map<String, dynamic>> savedPreparedMedia() => [
        for (final entry in _preparedMedia.entries.toList().reversed.take(64))
          if (entry.value.uri.queryParameters.containsKey('cache_version') &&
              entry.value.until
                  .isAfter(clock().add(const Duration(seconds: 60))))
            {
              'id': entry.key,
              'uri': entry.value.uri.toString(),
              'mime': entry.value.mimeType,
              'until': entry.value.until.millisecondsSinceEpoch,
              if (entry.value.duration != null)
                'duration': entry.value.duration,
            },
      ];

  void restorePreparedMedia(List values) {
    for (final value in values.take(64)) {
      if (value is! Map ||
          value['id'] is! String ||
          value['uri'] is! String ||
          value['mime'] is! String ||
          value['until'] is! int ||
          (value['uri'] as String).length > 1024) continue;
      try {
        _rememberMedia(value['id'], Uri.parse(value['uri']), value['mime'],
            DateTime.fromMillisecondsSinceEpoch(value['until']),
            sourceDuration: value['duration']);
      } catch (_) {
        /* Invalid saved metadata falls back to isolated playback. */
      }
    }
  }

  /// Optional feed-only scope. Search and playback discovery keep userScopeId.
  String? homeScopeId;
  String get homeRequestScopeId => userScopeId.toLowerCase() == 'guest'
      ? homeScopeId ?? userScopeId
      : userScopeId;
  String get userScopeId => _userScopeId;
  set userScopeId(String value) {
    final next = value.trim().isEmpty ? 'guest' : value.trim();
    if (next != _userScopeId) {
      _scopeRevision++;
      if (_cachedMediaAbort case final pending? when !pending.isCompleted)
        pending.complete();
      _preparationExpiry.clear();
      _preparedMedia.clear();
      _cachedMediaQueue.clear();
      _priorityMediaQueue.clear();
      _cachedMediaRetryAfter.clear();
      _preparingMedia.clear();
      _preparationTasks.clear();
      _startupHold?.cancel();
      _startupHold = null;
      _startupTicket++;
      clearSearchContext();
    }
    _userScopeId = next;
  }

  String? _searchContextQuery;
  String? _searchContextScope;
  DateTime? _searchContextAt;
  static const searchContextLifetime = Duration(minutes: 10);
  String? Function()? accessToken;
  void Function(String, Map<String, dynamic>)? onDiagnostic;
  bool Function()? online;
  String transportRoute = 'unspecified';
  int _requestSerial = 0;
  MusicApi(this.base, {http.Client? client, Uri? mediaBase, Uri? storageBase})
      : mediaBase = _mediaOrigin(base, mediaBase),
        storageBase = storageBase == null ? null : _mediaOrigin(base, storageBase),
        client = client ?? http.Client();

  static Uri _mediaOrigin(Uri base, Uri? configured) {
    if (configured == null) return Uri.parse(base.origin);
    final loopback = const {'localhost', '127.0.0.1', '::1'};
    if ((configured.scheme != 'https' &&
            !(configured.scheme == 'http' &&
                base.scheme == 'http' &&
                loopback.contains(base.host) &&
                loopback.contains(configured.host))) ||
        configured.host.isEmpty ||
        configured.userInfo.isNotEmpty ||
        (configured.path.isNotEmpty && configured.path != '/') ||
        configured.hasQuery ||
        configured.hasFragment)
      throw ArgumentError(
          'Media configuration must be a trusted HTTPS origin.');
    return Uri.parse(configured.origin);
  }

  /// Only application-owned stream paths move; metadata and saved descriptors
  /// retain the canonical API origin and their existing validation.
  Uri playbackUri(Uri uri) {
    if (uri.origin != base.origin && uri.origin != mediaBase.origin ||
        !uri.path.startsWith('/playback/stream/') ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment) throw ArgumentError('Unexpected playback resource.');
    final canonical = uri.replace(scheme: base.scheme, host: base.host, port: base.port);
    for (final media in _preparedMedia.values) {
      if (media.uri == canonical && media.deliveryUri != null &&
          media.deliveryUntil!.isAfter(clock().add(const Duration(seconds: 60)))) {
        return media.deliveryUri!;
      }
    }
    return uri.replace(
        scheme: mediaBase.scheme, host: mediaBase.host, port: mediaBase.port);
  }

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
    final fresh = createdAt != null &&
        clock().difference(createdAt) <= searchContextLifetime;
    // A context represents one recent search session and is consumed by the
    // next explicit selection, including a selection that fails relevance.
    clearSearchContext();
    if (query.isEmpty || !sameScope || !fresh) {
      return;
    }
    final title = (track['title'] ?? '').toString().trim();
    final artist =
        (track['artist'] ?? track['channel'] ?? track['author'] ?? '')
            .toString()
            .trim();
    final id = (track['id'] ?? track['videoId'] ?? track['track_key'] ?? '')
        .toString()
        .trim();
    final recordingId = (track['canonical_recording_id'] ??
            track['recording_id'] ??
            track['musicbrainz_recording_id'] ??
            '')
        .toString()
        .trim();
    if (userScopeId.toLowerCase() == 'guest' ||
        title.isEmpty ||
        artist.isEmpty ||
        (id.isEmpty && recordingId.isEmpty) ||
        !_queryRelatesToTrack(query, title, artist)) return;
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
    String normalize(String value) =>
        value.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), ' ').trim();
    final normalizedQuery = normalize(query);
    final normalizedItem = normalize('$title $artist');
    if (normalizedQuery.isEmpty || normalizedItem.isEmpty) return false;
    // Require a meaningful shared word; this suppresses obviously unrelated
    // Home/artist picks while allowing either a song-title or artist query.
    final tokens = normalizedQuery
        .split(RegExp(r'\s+'))
        .where((token) => token.length > 1)
        .toSet();
    return tokens
        .any((token) => normalizedItem.split(RegExp(r'\s+')).contains(token));
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
      if (const {'lastfm.freetls.fastly.net', 'lastfm-img.freetls.fastly.net'}
              .contains(resolved.host.toLowerCase()) &&
          resolved.pathSegments.isNotEmpty &&
          resolved.pathSegments.last.split('.').first.toLowerCase() ==
              '2a96cbd8b46e442fc41c2b86b821562f') return null;
      return resolved;
    } on FormatException {
      return null;
    }
  }

  List<String> artworkUrls(Map<String, dynamic> item, String type) {
    final track = type == 'track' || type == 'tracks';
    var source = track ? extractPlaybackSourceId(item) : null;
    // Old album detail rows kept a physical ID and its matching picture but
    // no playback object. Accept that picture's ownership without changing
    // playback routing or treating arbitrary catalog IDs as video sources.
    final legacy = '${item['id'] ?? ''}';
    final provider = item['source_provider'] ?? item['provider'];
    if (track &&
        source == null &&
        item['playback'] == null &&
        (provider == null || provider == 'youtube' || provider == 'ytmusic') &&
        RegExp(r'^[A-Za-z0-9_-]{11}$').hasMatch(legacy)) {
      final image = Uri.tryParse('${item['thumbnail'] ?? ''}');
      if (image != null &&
          const {'i.ytimg.com', 'img.youtube.com'}.contains(image.host) &&
          RegExp(r'/vi(?:_webp)?/([^/]+)/').firstMatch(image.path)?.group(1) ==
              legacy) source = legacy;
    }
    final youtube = source != null &&
        extractPlaybackProvider(item) == 'youtube' &&
        item['playable'] != false &&
        RegExp(r'^[A-Za-z0-9_-]{11}$').hasMatch(source);
    final values = <dynamic>[
      item['thumbnail'],
      item['thumbnail_url'],
      if (type == 'artist' || type == 'artists') item['artist_thumbnail'],
      item['artwork_url'],
      item['artwork'],
      item['cover_url'],
      item['image_url'],
      if (item['artwork_source_urls'] is List)
        ...item['artwork_source_urls'] as List,
      item['artwork_source_url'],
      if (item['thumbnails'] is List) ...(item['thumbnails'] as List).reversed,
      if (item['images'] is List) ...(item['images'] as List).reversed,
      if (youtube) 'https://i.ytimg.com/vi/$source/hqdefault.jpg',
    ];
    final urls = <String>[];
    for (var value in values) {
      if (value is Map) value = value['url'] ?? value['src'];
      if (value is! String) continue;
      final uri = artworkUri(value);
      if (uri == null) continue;
      final videoImage = RegExp(r'/vi(?:_webp)?/([^/]+)/').firstMatch(uri.path);
      if (track &&
          videoImage != null &&
          (!youtube || videoImage.group(1) != source)) continue;
      if (track && value == item['artist_thumbnail']) continue;
      final url = uri.toString();
      if (!urls.contains(url)) urls.add(url);
    }
    return urls;
  }

  Future<Map<String, dynamic>> post(String path, Map<String, dynamic> body,
          {Duration timeout = const Duration(seconds: 30),
          Future<void>? cancel}) =>
      _request(path, body: body, timeout: timeout, cancel: cancel);

  Future<Map<String, dynamic>> get(String path,
          {Duration timeout = const Duration(seconds: 30)}) =>
      _request(path, timeout: timeout);

  Future<Map<String, dynamic>> _request(String path,
      {Map<String, dynamic>? body,
      Future<void>? cancel,
      Duration timeout = const Duration(seconds: 30)}) async {
    final timer = Stopwatch()..start();
    final revision = _scopeRevision;
    var purpose = body == null ? 'detail' : 'interactive';
    if (path == '/recommend') {
      purpose = 'full_feed';
      if (body?['feed_queue_status_only'] == true) {
        purpose = 'queue_status';
      } else if (body?['prepare_next_session'] == true) {
        purpose = 'background_prepare';
      } else if (body?['row_id'] != null) {
        purpose = 'row_page';
      }
    } else if (path == '/prepare_session') {
      purpose = body?['cached_only'] == true
          ? 'cache_metadata'
          : (body?['background'] == true)
              ? 'background_cache'
              : 'upcoming';
    }
    final endpointPath = Uri.parse(path).path;
    final requestId =
        '${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}-${_requestSerial++}';
    final encodedBody = body == null ? null : jsonEncode(body);
    final details = <String, dynamic>{
      'requestId': requestId,
      'requestBytes': encodedBody == null ? 0 : utf8.encode(encodedBody).length,
      'transportRoute': transportRoute,
      'path': endpointPath.startsWith('/lyrics/')
          ? '/lyrics/*'
          : endpointPath.startsWith('/artist/')
              ? '/artist/*'
              : endpointPath.startsWith('/album/')
                  ? '/album/*'
                  : endpointPath,
      'requestedTrack': body?['current_track_key'],
      'requestPurpose': purpose,
    };
    late http.Response response;
    final abort = Completer<void>();
    try {
      final headers = {
        if (body != null) 'Content-Type': 'application/json',
        if (accessToken?.call() case final String token)
          'Authorization': 'Bearer $token',
        'X-Neatie-Request': requestId,
      };
      if (endpointPath.startsWith('/assistant/') ||
          endpointPath.startsWith('/lyrics/') &&
              endpointPath.endsWith('/meaning')) {
        headers
            .addAll(await assistantHeaders(accessToken: accessToken?.call()));
        if (revision != _scopeRevision) throw StateError('Account changed.');
      }
      final request = http.AbortableRequest(
          body == null ? 'GET' : 'POST', endpoint(path),
          abortTrigger: cancel == null
              ? abort.future
              : Future.any<void>([abort.future, cancel]))
        ..headers.addAll(headers);
      if (encodedBody != null) request.body = encodedBody;
      response = await client
          .send(request)
          .then(http.Response.fromStream)
          .timeout(timeout, onTimeout: () {
        if (!abort.isCompleted) abort.complete();
        throw TimeoutException('Music request timed out.');
      });
    } catch (e) {
      if (revision == _scopeRevision) {
        onDiagnostic?.call('api-failure', {
          ...details,
          'ms': timer.elapsedMilliseconds,
          'online': online?.call(),
          'failureCode': e is TimeoutException
              ? 'request_timeout'
              : e is http.RequestAbortedException
                  ? 'request_aborted'
                  : e is http.ClientException
                      ? 'network_error'
                      : 'request_error',
          'errorType': e is TimeoutException
              ? 'TimeoutException'
              : e is http.RequestAbortedException
                  ? 'RequestAbortedException'
                  : e is http.ClientException
                      ? 'ClientException'
                      : 'RequestError',
        });
      }
      rethrow;
    }
    if (response.statusCode != 200) {
      if (revision == _scopeRevision)
        onDiagnostic?.call('api-response', {
          ...details,
          'status': response.statusCode,
          'ms': timer.elapsedMilliseconds,
          'responseBytes': response.bodyBytes.length
        });
      if (endpointPath.startsWith('/assistant/')) {
        throw SongDiscussionException(response.statusCode);
      }
      throw StateError(
          'Music service returned ${response.statusCode}. Please retry.');
    }
    try {
      final decode = Stopwatch()..start();
      final decoded = jsonDecode(response.body);
      if (decoded is! Map) throw const FormatException('Invalid payload');
      decode.stop();
      if (revision == _scopeRevision) {
        final diagnostics = decoded['diagnostics'] is Map
            ? decoded['diagnostics'] as Map
            : const {};
        onDiagnostic?.call('api-response', {
          ...details,
          'status': response.statusCode,
          'ms': timer.elapsedMilliseconds,
          'responseBytes': response.bodyBytes.length,
          'jsonDecodeMs': decode.elapsedMicroseconds / 1000,
          'serverMs': double.tryParse(RegExp(r'api;dur=([\d.]+)')
                  .firstMatch(response.headers['server-timing'] ?? '')
                  ?.group(1) ??
              ''),
          if (endpointPath == '/history_seed') ...{
            'historyEventReadMs': diagnostics['history_event_read_ms'],
            'historyRouteWaitMs': diagnostics['history_route_wait_ms'],
            'historyCatalogLookupMs': diagnostics['history_catalog_lookup_ms'],
            'historyCountMs': diagnostics['history_count_ms'],
            'historyTotalMs': diagnostics['history_total_ms'],
          },
        });
      }
      if (revision == _scopeRevision && endpointPath == '/recommend') {
        final diagnostics = decoded['diagnostics'] is Map
            ? decoded['diagnostics'] as Map
            : const {};
        onDiagnostic?.call('home-response', {
          ...details,
          'serverRequestId': decoded['request_id'],
          'forceRefresh': body?['force_refresh'] == true,
          'refreshFromVersion': body?['refresh_from_version'],
          'feedAction': decoded['feed_action'] ?? diagnostics['feed_action'],
          'feedReason': diagnostics['feed_action_reason'],
          'sessionId': decoded['session_id'],
          'feedVersion': decoded['feed_version'] ?? diagnostics['feed_version'],
          'queueRevision': diagnostics['queue_revision'],
          'readyFeedCount': diagnostics['ready_feed_count'],
          'queueBuildInflight': diagnostics['queue_build_inflight'],
          'preparationState': diagnostics['preparation_state'],
          'preparationBlockedReason': diagnostics['preparation_blocked_reason'],
          'backgroundSupplyInflight': diagnostics['background_supply_inflight'],
          'noveltyEnforced': diagnostics['novelty_enforced'],
          'noveltyWouldReject': diagnostics['novelty_would_reject'],
          'serverMs': diagnostics['request_ms'],
          'ms': timer.elapsedMilliseconds,
        });
      }
      if (revision == _scopeRevision &&
          (endpointPath == '/search' || endpointPath == '/suggest') &&
          (body?['revision'] as num? ?? 0) == 0) {
        final diagnostics = decoded['diagnostics'];
        onDiagnostic?.call(
            endpointPath == '/search' ? 'search-response' : 'suggest-response',
            {
              ...details,
              'ms': timer.elapsedMilliseconds,
              'resultCount': endpointPath == '/suggest'
                  ? (decoded['suggestions'] as List? ??
                          decoded['results'] as List? ??
                          const [])
                      .length
                  : tracks(decoded['tracks'] ?? decoded['results']).length,
              'serverRequestId': decoded['request_id']?.toString(),
              'searchSurface': body?['result_type']?.toString() ?? 'all',
              'preview': body?['search_preview'] == true,
              if (diagnostics is Map)
                if (diagnostics['request_ms'] is num)
                  'serverMs': diagnostics['request_ms'],
              if (diagnostics is Map)
                for (final key in [
                  'first_response_contract_ms',
                  'direct_lookup_ms'
                ])
                  if (diagnostics[key] is num) key: diagnostics[key],
              if (diagnostics is Map) ...{
                'snapshotHit': diagnostics['search_snapshot_hit'] == true,
                'searchOutcome': diagnostics['search_outcome']?.toString(),
                'searchRefreshRetained': diagnostics['search_refresh_retained'] == true,
                if (diagnostics['search_failed_sources'] is List)
                  'searchProviderFailures': (diagnostics['search_failed_sources'] as List).length,
                if (diagnostics['provider_counts'] case final Map counts)
                  for (final key in ['songs', 'videos', 'artists', 'top'])
                    if (counts[key] is num) 'searchProvider_$key': counts[key],
                for (final stage in [
                  'retrieval',
                  'ranking',
                  'essential_target',
                  'artist_surface_completion',
                  'snapshot_store'
                ])
                  if (diagnostics['stage_timings_ms'] is Map &&
                      diagnostics['stage_timings_ms'][stage] is num)
                    '${stage}Ms': diagnostics['stage_timings_ms'][stage],
                if (diagnostics['retrieval'] is Map &&
                    diagnostics['retrieval']['local_index_ms'] is num)
                  'localIndexMs': diagnostics['retrieval']['local_index_ms'],
                if (diagnostics['retrieval'] is Map &&
                    diagnostics['retrieval']['timed_out_sources'] is List)
                  'providerTimeoutCount':
                      (diagnostics['retrieval']['timed_out_sources'] as List)
                          .length,
              },
            });
      }
      final payload = Map<String, dynamic>.from(decoded);
      if (catalogMediaHints &&
          revision == _scopeRevision &&
          (const {
                '/search',
                '/recommend',
                '/related_tracks',
                '/history_seed',
                '/recent_search_picks',
                '/charts',
                '/new_releases'
              }.contains(endpointPath) ||
              endpointPath.startsWith('/artist/') ||
              endpointPath.startsWith('/album/'))) {
        _preloadCatalogMedia(payload);
      }
      return payload;
    } on FormatException {
      if (revision == _scopeRevision) {
        onDiagnostic?.call('api-failure', {
          ...details,
          'ms': timer.elapsedMilliseconds,
          'errorType': 'FormatException'
        });
      }
      throw const FormatException('Music service returned an invalid payload.');
    }
  }

  Map<String, dynamic>? queryCorrection;
  Future<List<Map<String, dynamic>>> search(String query,
      {bool autoCorrect = true}) async {
    final result = await post('/search', {
      'query': query,
      'auto_correct': autoCorrect,
      'limit': 24,
      'user_scope_id': userScopeId,
      'defer_side_surfaces': true,
      'context_surface': 'interactive_search',
    });
    queryCorrection = result['query_correction'] is Map
        ? Map<String, dynamic>.from(result['query_correction'])
        : null;
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
    return playbackUri(endpoint('/playback/stream/${Uri.encodeComponent(id)}'))
        .replace(queryParameters: {
      'resolve': '1',
      if (storageBase != null) 'direct_cached': '1',
      if (preferredCachedVariant.isNotEmpty)
        'preferred_cached_variant': preferredCachedVariant,
      for (final entry in data.entries) entry.key: entry.value.toString()
    });
  }

  Future<Map<String, dynamic>> queueCacheStatus(String token, int revision) async {
    final scope = _scopeRevision;
    final remembered = {
      for (final id in _preparedMedia.keys)
        id: (_preparedMedia[id], _preparationExpiry[id])
    };
    final result = await post('/prepare_session', {
        'queue_cache_id': token,
        'queue_cache_revision': revision,
        'queue_cache_status_only': true,
        'queue_cache_metadata': true,
        if (preferredCachedVariant.isNotEmpty)
          'preferred_cached_variant': preferredCachedVariant,
      });
    if (scope == _scopeRevision && result['prepared'] is Map) {
      final ids = (result['prepared'] as Map).keys.whereType<String>().take(5);
      _rememberCachedMedia(result['prepared'], ids.where((id) =>
          (remembered[id] ?? (null, null)) ==
          (_preparedMedia[id], _preparationExpiry[id])));
    }
    return result;
  }

  Future<Map<String, dynamic>> cacheQueue(
      String token, int revision, List<Map<String, dynamic>> tracks,
      {bool cancel = false}) async {
    final scope = _scopeRevision;
    final ids =
        tracks.map(extractTrackId).whereType<String>().take(100).toList();
    final remembered = {
      for (final id in ids) id: (_preparedMedia[id], _preparationExpiry[id])
    };
    final result = await post('/prepare_session', {
      'queue_cache_id': token,
      'queue_cache_revision': revision,
      'cancel_queue_cache': cancel,
      'queue_cache_metadata': !cancel,
      if (preferredCachedVariant.isNotEmpty)
        'preferred_cached_variant': preferredCachedVariant,
      'track_keys': ids,
    });
    if (scope == _scopeRevision && !cancel) {
      _rememberCachedMedia(result['prepared'], ids.where((id) =>
          remembered[id] == (_preparedMedia[id], _preparationExpiry[id])));
    }
    return result;
  }

  Future<Uri> prepare(Map<String, dynamic> track,
      {bool activeQueue = false, bool cacheFull = false}) {
    final id = extractTrackId(track)!;
    final ready = preparedMedia(id);
    if (!cacheFull && ready != null && startupPrepared(id)) {
      // Format upgrades belong to the existing background metadata lane. A
      // playable cached source must never wait for an optional replacement.
      if (needsRepresentationRefresh(id))
        preloadCachedMedia([track], priority: activeQueue);
      return Future.value(ready.uri);
    }
    final key = (id, cacheFull);
    final pending = _preparationTasks[key];
    if (pending != null) return pending;
    final scope = _scopeRevision;
    _preparingMedia.add(id);
    _cachedMediaQueue.remove(id);
    _priorityMediaQueue.remove(id);
    late final Future<Uri> task;
    task = _prepareTrack(track, activeQueue: activeQueue, cacheFull: cacheFull)
        .whenComplete(() {
      if (scope == _scopeRevision && identical(_preparationTasks[key], task)) {
        _preparationTasks.remove(key);
        if (!_preparationTasks.keys.any((key) => key.$1 == id))
          _preparingMedia.remove(id);
        unawaited(_loadCachedMedia());
      }
    });
    _preparationTasks[key] = task;
    return task;
  }

  Future<Uri> _prepareTrack(Map<String, dynamic> track,
      {bool activeQueue = false, bool cacheFull = false}) async {
    final id = extractTrackId(track)!;
    final revision = _scopeRevision;
    final result = await post(
        '/prepare_session',
        {
          'track_keys': [id],
          'current_track_key': id,
          'active_queue': activeQueue,
          'lookahead': 1,
          'cache_full': cacheFull,
          'background': cacheFull,
          if (preferredCachedVariant.isNotEmpty)
            'preferred_cached_variant': preferredCachedVariant,
        },
        timeout: const Duration(seconds: 90));
    if (revision == _scopeRevision) {
      if ((result['prepared'] as Map?)?[id] != null) _preparedMedia.remove(id);
      final data = (result['prepared'] as Map?)?[id] as Map?;
      final expiry = data?['expires_at'];
      _preparationExpiry.remove(id);
      if (expiry is num &&
          expiry.isFinite &&
          expiry > 0 &&
          expiry <
              clock().add(const Duration(days: 1)).millisecondsSinceEpoch /
                  1000) {
        if (_preparationExpiry.length >= 256) {
          _preparationExpiry.remove(_preparationExpiry.keys.first);
        }
        final cached = const {'r2_full', 'local_full'}.contains(_cacheTier(data));
        final warmed = data?['startup_ready_until'];
        // A catalog descriptor proves file/type identity, not byte readiness.
        // Older servers with a real prefix retain a short compatibility lease.
        final readyUntil = warmed is num && warmed.isFinite
            ? warmed.toDouble()
            : !cached || (data?['cached_prefix_bytes'] as num? ?? 0) > 0
                ? expiry.toDouble().clamp(0, clock().add(const Duration(minutes: 5)).millisecondsSinceEpoch / 1000)
                : 0.0;
        if (readyUntil > 0) {
          _preparationExpiry[id] = DateTime.fromMillisecondsSinceEpoch(
              (readyUntil.clamp(0, expiry) * 1000).round(), isUtc: true);
        }
      }
    }
    void reportPreparation() {
      if (revision != _scopeRevision) return;
      final media = preparedMedia(id);
      final failed = (result['failed'] as Map?)?[id] as Map?;
      final facts = failed?['diagnostics'] as Map?;
      final tier = _cacheTier((result['prepared'] as Map?)?[id]);
      onDiagnostic?.call('prepare-result', {
        'requestedTrack': id,
        'preparedSourceId': ((result['prepared'] as Map?)?[id] as Map?)?['source_id'],
        'preparedVariant': media?.uri.queryParameters['cache_variant'],
        'preparedVersion': media?.uri.queryParameters['cache_version'] ?? media?.uri.queryParameters['stream_version'],
        'preparedDirect': media?.deliveryUri != null,
        'requestPurpose': cacheFull ? 'background_cache' : 'upcoming',
        'failureCode': _failureCode(
            ((result['failed'] as Map?)?[id] as Map?)?['code'] ??
                result['skip_reason']),
        'cacheTier': tier,
        'failureStage': const {
          'cookie_load',
          'extraction',
          'media_preflight',
          'source_resolution'
        }.contains(facts?['stage'])
            ? facts!['stage']
            : null,
        'failureType': facts?['exception_type'] is String &&
                RegExp(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
                    .hasMatch(facts!['exception_type'])
            ? facts['exception_type']
            : null,
        'failureHttpStatus': facts?['http_status'] is int &&
                facts!['http_status'] >= 400 &&
                facts['http_status'] <= 599
            ? facts['http_status']
            : null,
        'serverMs': result['server_ms'],
        'cachedPrefixBytes':
            ((result['prepared'] as Map?)?[id] as Map?)?['cached_prefix_bytes'],
        'chunkDeferred':
            ((result['prepared'] as Map?)?[id] as Map?)?['chunk_deferred'],
        'startupBytesReady': startupPrepared(id),
        'prepared': (result['prepared'] as Map?)?[id] != null,
        'fullyCached': const {'r2_full', 'local_full'}.contains(tier) ||
            ((result['prepared'] as Map?)?[id] as Map?)?['fully_cached'] ==
                true,
        'failed': (result['failed_ids'] as List?)?.contains(id) == true
      });
    }
    if ((result['prepared'] as Map?)?[id] == null) {
      reportPreparation();
      throw PlaybackPreparationException(
          _failureCode(((result['failed'] as Map?)?[id] as Map?)?['code']) ??
              'stream_failed');
    }
    final path = ((result['prepared'] as Map?)?[id] as Map?)?['playback_path']
        ?.toString();
    if (path != null && path.isNotEmpty && revision == _scopeRevision) {
      final uri = base.resolve(path);
      if (!['http', 'https'].contains(uri.scheme) ||
          uri.origin != base.origin ||
          !uri.path.startsWith('/playback/stream/')) {
        throw StateError(
            'The music service returned an unexpected stream origin.');
      }
      final data = (result['prepared'] as Map)[id] as Map;
      final mime =
          '${data['mime_type'] ?? ''}'.split(';').first.trim().toLowerCase();
      final expiry = data['expires_at'];
      final until = expiry is num && expiry.isFinite && expiry > 0 &&
              expiry <= clock().add(const Duration(days: 1)).millisecondsSinceEpoch / 1000
          ? DateTime.fromMillisecondsSinceEpoch((expiry * 1000).round(), isUtc: true)
          : null;
      final version = uri.queryParameters['cache_version'] ?? '';
      if ((const {'r2_full', 'local_full'}.contains(_cacheTier(data)) ||
              uri.queryParameters.containsKey('stream_version')) &&
          until != null)
        _rememberMedia(id, uri, mime, until, sourceDuration: data['duration'], delivery: data);
      reportPreparation();
      if ((version.isNotEmpty ||
              uri.queryParameters.containsKey('stream_version')) &&
          !_preparedMedia.containsKey(id)) {
        // Incomplete/untrusted descriptors keep the ordinary isolated path.
        return streamUri(track);
      }
      return uri;
    }
    reportPreparation();
    return streamUri(track);
  }

  static String? _failureCode(dynamic value) => const {
        'video_unavailable',
        'format_unavailable',
        'source_blocked',
        'stream_failed',
        'playback_source_missing',
        'playback_source_failed',
        'playback_sources_failed',
        'preparation_timeout',
        'preparation_busy',
        'youtube_temporarily_blocked'
      }.contains(value)
          ? value as String
          : value == null
              ? null
              : 'other';

  static String _cacheTier(dynamic value) {
    if (value is! Map) return 'unprepared';
    if (value['cache_tier'] is String &&
        const {
          'r2_full',
          'local_full',
          'prefix',
          'resolved',
          'cold'
        }.contains(value['cache_tier'])) return value['cache_tier'] as String;
    if (value['source_kind'] == 'r2_proxy') return 'r2_full';
    if (value['source_kind'] == 'local_proxy') return 'local_full';
    if ((value['cached_prefix_bytes'] as num? ?? 0) > 0) return 'prefix';
    return value['resolve_cache_hit'] == true ? 'resolved' : 'cold';
  }

  Future<({List<Map<String, dynamic>> tracks, bool hasMore})> related(
      Map<String, dynamic> seed, List<Map<String, dynamic>> queue,
      {String discoverySession = '', List<String> recentIds = const []}) async {
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
    if (result['recommendation_diagnostics'] is Map) {
      onDiagnostic?.call('queue-recommendations', {
        for (final key in const [
          'candidate_count',
          'fresh_count',
          'repeated_count',
          'selected_count',
          'selected_artist_count',
          'selected_album_count',
          'catalog_candidate_count',
          'provider_depth',
          'fresh_supply_exhausted'
        ])
          if ((result['recommendation_diagnostics'] as Map)[key] != null)
            key: (result['recommendation_diagnostics'] as Map)[key],
      });
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
