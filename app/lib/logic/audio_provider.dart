// ignore_for_file: experimental_member_use

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'dart:io';
import 'dart:convert';
import 'dart:async';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../ffi/audio_ffi.dart';
import 'auth_provider.dart';
import 'playlist_provider.dart';
import 'package:audio_service/audio_service.dart';
import 'package:just_audio/just_audio.dart';

late AudioHandler globalAudioHandler;
const String _configuredProxyBaseUrl =
    String.fromEnvironment('AURALIS_PROXY_URL', defaultValue: '');
final http.Client appHttpClient = http.Client();
final http.Client proxyControlHttpClient = http.Client();
const String _proxyUnavailableMessage =
    'Recommendation service is unavailable right now. Check the proxy connection and try again.';
const String _searchUnavailableMessage =
    'Search could not complete right now. Try again in a moment.';
const String _searchTimeoutMessage =
    'Search is taking longer than expected. Try again in a moment.';
const bool _disableRecommendationClientTimeouts = true;
const Duration _recommendRequestTimeout = Duration(seconds: 45);
const Duration _recommendRowPageTimeout = Duration(seconds: 30);
const String _recommendTimeoutMessage =
    'Recommendation service took too long to respond. Please try again.';

Future<T> _runRecommendationRequest<T>(Future<T> future, Duration timeout) {
  if (_disableRecommendationClientTimeouts) {
    return future;
  }
  return future.timeout(timeout);
}

String _normalizeProxyBaseUrl(String baseUrl) {
  final trimmed = baseUrl.trim();
  if (trimmed.isEmpty) return trimmed;
  return trimmed.endsWith('/')
      ? trimmed.substring(0, trimmed.length - 1)
      : trimmed;
}

List<String> get proxyBaseUrlCandidates {
  final configured = _configuredProxyBaseUrl.trim();
  if (configured.isNotEmpty) {
    return <String>[_normalizeProxyBaseUrl(configured)];
  }

  final candidates = <String>[
    if (Platform.isAndroid) 'http://10.0.2.2:8000',
    'http://127.0.0.1:8000',
  ];
  final normalized = candidates
      .map(_normalizeProxyBaseUrl)
      .where((value) => value.isNotEmpty)
      .toSet()
      .toList(growable: false);
  return normalized.isEmpty
      ? const <String>['http://127.0.0.1:8000']
      : normalized;
}

String get proxyBaseUrl => proxyBaseUrlCandidates.first;

Future<bool> probeProxyHealth(
    {Duration timeout = const Duration(seconds: 3)}) async {
  for (final baseUrl in proxyBaseUrlCandidates) {
    try {
      final res = await proxyControlHttpClient
          .get(Uri.parse('$baseUrl/'))
          .timeout(timeout);
      if (res.statusCode >= 200 && res.statusCode < 500) {
        return true;
      }
    } catch (_) {
      // Try the next candidate endpoint.
    }
  }
  return false;
}

void _debugProxyLog(String area, String message) {
  if (!kDebugMode) return;
  debugPrint('[EBB:$area] $message');
}

String _compactDiagnosticValue(Object? value) {
  if (value == null) return 'null';
  try {
    final encoded = jsonEncode(value);
    if (encoded.length <= 480) {
      return encoded;
    }
    return '${encoded.substring(0, 480)}...';
  } catch (_) {
    return value.toString();
  }
}

const List<String> _sharedTasteKeywords = <String>[
  'rock',
  'metal',
  'jazz',
  'blues',
  'classical',
  'orchestra',
  'punk',
  'indie',
  'folk',
  'country',
  'ambient',
  'chill',
  'sad',
  'happy',
  'workout',
  'gym',
  'sleep',
  'focus',
  'lofi',
  'edm',
  'house',
  'techno',
  'trance',
  'disco',
  'pop',
  'rap',
  'hip hop',
  'rnb',
  'soul',
  'funk',
  'vibe',
  'mood',
  'party',
  'romantic',
  'rain',
  'night',
  'morning',
  'road trip',
  '80s',
  '90s',
  '2000s',
];

Uri buildProxyUri(String path) {
  final normalizedPath = path.startsWith('/') ? path : '/$path';
  return Uri.parse('$proxyBaseUrl$normalizedPath');
}

Future<void> initAudioService() async {
  globalAudioHandler = await AudioService.init(
    builder: () => AuralisAudioHandler(),
    config: const AudioServiceConfig(
      androidNotificationChannelId: 'com.danbeverley.ebb.channel.audio',
      androidNotificationChannelName: 'EBB Audio Playback',
      androidNotificationOngoing: true,
      androidStopForegroundOnPause: true,
      fastForwardInterval: Duration(seconds: 10),
      rewindInterval: Duration(seconds: 10),
    ),
  );
}

class AuralisAudioHandler extends BaseAudioHandler
    with QueueHandler, SeekHandler {
  Function()? onPlay;
  Function()? onPause;
  Function(Duration)? onSeek;
  Function()? onFastForward;
  Function()? onRewind;
  Future<void> Function()? onStop;
  Future<void> Function()? onSkipToNext;
  Future<void> Function()? onSkipToPrevious;

  @override
  Future<void> play() async => onPlay?.call();
  @override
  Future<void> pause() async => onPause?.call();
  @override
  Future<void> seek(Duration position) async => onSeek?.call(position);
  @override
  Future<void> fastForward() async => onFastForward?.call();
  @override
  Future<void> rewind() async => onRewind?.call();
  @override
  Future<void> stop() async => await onStop?.call();
  @override
  Future<void> skipToNext() async => await onSkipToNext?.call();
  @override
  Future<void> skipToPrevious() async => await onSkipToPrevious?.call();

  void broadcastState(PlaybackState state) {
    playbackState.add(state);
  }

  void broadcastMediaItem(MediaItem item) {
    mediaItem.add(item);
  }

  void broadcastQueue(List<MediaItem> items) {
    queue.add(items);
  }
}

int _parseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

String _cleanString(dynamic value) {
  final text = value?.toString().trim() ?? '';
  return text;
}

Map<String, String> _parseHeaders(dynamic value) {
  if (value is! Map) return const {};
  final headers = <String, String>{};
  value.forEach((key, dynamic entryValue) {
    if (key == null || entryValue == null) return;
    headers[key.toString()] = entryValue.toString();
  });
  return headers;
}

String? extractTrackId(dynamic track) {
  if (track is! Map) return null;
  final value = track['id'] ?? track['videoId'] ?? track['video_id'];
  final id = value?.toString();
  if (id == null || id.isEmpty) return null;
  return id;
}

String _extractPrimaryArtist(dynamic rawTrack) {
  if (rawTrack is! Map) return '';
  final artists = rawTrack['artists'];
  if (artists is! List) return '';
  for (final entry in artists) {
    if (entry is Map) {
      final name =
          _cleanString(entry['name'] ?? entry['artist'] ?? entry['title']);
      if (name.isNotEmpty) return name;
    } else {
      final name = _cleanString(entry);
      if (name.isNotEmpty) return name;
    }
  }
  return '';
}

List<String> extractTrackArtists(dynamic rawTrack) {
  if (rawTrack is! Map) return const <String>[];
  final results = <String>[];
  final seen = <String>{};

  void addArtist(String? rawValue) {
    final value = _cleanString(rawValue);
    final normalized = value.toLowerCase();
    if (value.isEmpty ||
        normalized == 'unknown artist' ||
        !seen.add(normalized)) {
      return;
    }
    results.add(value);
  }

  final artists = rawTrack['artists'];
  if (artists is List) {
    for (final entry in artists) {
      if (entry is Map) {
        addArtist(
          entry['name']?.toString() ??
              entry['artist']?.toString() ??
              entry['title']?.toString(),
        );
      } else {
        addArtist(entry?.toString());
      }
    }
  }

  addArtist(
    rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString(),
  );

  return results;
}

Map<String, dynamic> normalizeTrack(dynamic rawTrack) {
  final track = Map<String, dynamic>.from(rawTrack as Map);
  final id = extractTrackId(track);
  final title = _cleanString(
    track['title'] ??
        track['name'] ??
        track['track'] ??
        track['song'] ??
        track['video_title'],
  );
  final primaryArtist = _extractPrimaryArtist(track);
  final channel = _cleanString(
    track['channel'] ??
        track['author'] ??
        track['artist'] ??
        primaryArtist ??
        track['uploader'],
  );
  final album = _cleanString(track['album'] ?? track['album_title']);
  return {
    ...track,
    if (id != null) 'id': id,
    if (id != null && track['videoId'] == null) 'videoId': id,
    'title': title.isNotEmpty ? title : 'Unknown Track',
    'channel': channel,
    'author': channel,
    'artist': _cleanString(track['artist']).isNotEmpty
        ? _cleanString(track['artist'])
        : channel,
    if (album.isNotEmpty) 'album': album,
    if (album.isNotEmpty) 'album_title': album,
    'duration': _parseInt(track['duration']),
  };
}

bool _isTrackMetadataIncomplete(dynamic rawTrack) {
  if (rawTrack is! Map) return true;
  final title = _cleanString(
    rawTrack['title'] ??
        rawTrack['name'] ??
        rawTrack['track'] ??
        rawTrack['song'],
  ).toLowerCase();
  final artist = _cleanString(
    rawTrack['channel'] ?? rawTrack['author'] ?? rawTrack['artist'],
  ).toLowerCase();
  return title.isEmpty ||
      title == 'unknown track' ||
      artist.isEmpty ||
      artist == 'unknown artist';
}

bool isTrackHidden(dynamic track) {
  if (track is! Map) return false;
  return track['isHidden'] == true;
}

Map<String, dynamic> copyTrackWithHidden(
  Map<String, dynamic> track,
  bool isHidden,
) {
  return {
    ...track,
    'isHidden': isHidden,
  };
}

Map<String, dynamic> _cloudTrackPayload(dynamic rawTrack) {
  final track = normalizeTrack(rawTrack);
  final trackId = extractTrackId(track);
  return {
    if (trackId != null) 'id': trackId,
    if (trackId != null) 'videoId': trackId,
    if (track['title'] != null) 'title': track['title'],
    if (track['author'] != null) 'author': track['author'],
    if (track['channel'] != null) 'channel': track['channel'],
    if (track['artist'] != null) 'artist': track['artist'],
    if (track['thumbnail'] != null) 'thumbnail': track['thumbnail'],
    'duration': _parseInt(track['duration']),
    if (track['album'] != null) 'album': track['album'],
    if (track['album_id'] != null) 'album_id': track['album_id'],
  };
}

Map<String, dynamic>? _trackFromDetailsPayload(dynamic rawPayload) {
  if (rawPayload is! Map) return null;
  final payload = Map<String, dynamic>.from(rawPayload);
  final videoId =
      payload['video_id']?.toString() ?? payload['id']?.toString() ?? '';
  if (videoId.trim().isEmpty) return null;
  return normalizeTrack({
    'id': videoId,
    'videoId': videoId,
    'title': payload['title'],
    'thumbnail': payload['thumbnail'],
    'channel': payload['author'] ?? payload['artist'],
    'author': payload['author'] ?? payload['artist'],
    'artist': payload['author'] ?? payload['artist'],
    'duration': payload['duration'],
    'album': payload['album'] ?? payload['album_title'],
    'album_title': payload['album_title'] ?? payload['album'],
    'album_id': payload['album_id'],
  });
}

Map<String, dynamic>? _lastTrackSnapshotFromRawTrack(
  dynamic rawTrack, {
  String? localPath,
}) {
  final normalized = normalizeTrack(rawTrack);
  final trackId = extractTrackId(normalized);
  if (trackId == null || trackId.isEmpty) return null;
  return {
    'id': trackId,
    'videoId': trackId,
    'title': normalized['title'],
    'thumbnail': normalized['thumbnail'],
    'channel': normalized['channel'],
    'author': normalized['author'],
    'artist': normalized['artist'],
    'duration': _parseInt(normalized['duration']),
    'album': normalized['album'] ?? normalized['album_title'],
    'album_title': normalized['album_title'] ?? normalized['album'],
    'album_id': normalized['album_id'],
    if (localPath != null && localPath.trim().isNotEmpty)
      'local_path': localPath,
  };
}

Future<void> upsertCloudLibraryTrack(dynamic rawTrack) async {
  final payload = _cloudTrackPayload(rawTrack);
  final trackId = extractTrackId(payload);
  if (trackId != null && trackId.isNotEmpty) {
    unawaited(
      recordProxyInteractionEvent(
        'library',
        trackId: trackId,
        rawTrack: payload,
      ),
    );
  }
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null || userId == null || userId.isEmpty) return;
  if (trackId == null || trackId.isEmpty) return;
  try {
    await client.from('library_tracks').upsert(
      {
        'user_id': userId,
        'track_id': trackId,
        'track_data': payload,
        'updated_at': DateTime.now().toUtc().toIso8601String(),
      },
      onConflict: 'user_id,track_id',
    );
  } catch (error) {
    debugPrint('Cloud library sync failed: $error');
  }
}

Future<void> removeCloudLibraryTrack(String? trackId) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null ||
      userId == null ||
      userId.isEmpty ||
      trackId == null ||
      trackId.isEmpty) {
    return;
  }
  try {
    await client
        .from('library_tracks')
        .delete()
        .eq('user_id', userId)
        .eq('track_id', trackId);
  } catch (error) {
    debugPrint('Cloud library delete failed: $error');
  }
}

Future<void> recordCloudSearchEvent(
  String query, {
  required int resultCount,
}) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  final trimmedQuery = query.trim();
  if (client == null ||
      userId == null ||
      userId.isEmpty ||
      trimmedQuery.isEmpty) {
    return;
  }
  try {
    await client.from('search_events').insert({
      'user_id': userId,
      'query': trimmedQuery,
      'result_count': resultCount,
    });
  } catch (error) {
    debugPrint('Cloud search event write failed: $error');
  }
}

Map<String, dynamic>? _interactionEventTrackPayload(dynamic rawTrack) {
  if (rawTrack == null) return null;
  if (rawTrack is Map<String, dynamic>) {
    return normalizeTrack(Map<String, dynamic>.from(rawTrack));
  }
  if (rawTrack is Map) {
    return normalizeTrack(Map<String, dynamic>.from(rawTrack));
  }
  return null;
}

Future<void> recordProxyInteractionEvent(
  String eventType, {
  String? trackId,
  dynamic rawTrack,
  Map<String, dynamic>? metadata,
  DateTime? occurredAt,
}) async {
  final normalizedTrack = _interactionEventTrackPayload(rawTrack);
  final resolvedTrackId =
      (trackId ?? extractTrackId(normalizedTrack))?.trim() ?? '';
  if (resolvedTrackId.isEmpty) return;

  final payload = <String, dynamic>{
    if (normalizedTrack != null) ...normalizedTrack,
    if (metadata != null) ...metadata,
  };

  try {
    await proxyControlHttpClient
        .post(
          buildProxyUri('/interaction_event'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'user_scope_id': currentAuthenticatedUserId ?? 'guest',
            'track_id': resolvedTrackId,
            'event_type': eventType.trim().toLowerCase(),
            'artist_name': payload['channel'] ?? payload['artist'],
            'source': 'app',
            'occurred_at':
                (occurredAt ?? DateTime.now().toUtc()).millisecondsSinceEpoch /
                    1000.0,
            'metadata': payload,
          }),
        )
        .timeout(const Duration(seconds: 3));
  } catch (_) {
    // Keep playback/search interactions non-blocking if the proxy is unavailable.
  }
}

Future<void> recordProxySearchEvent(
  String query, {
  required int resultCount,
  String searchScope = 'track',
  Map<String, dynamic>? metadata,
  DateTime? occurredAt,
}) async {
  final trimmedQuery = query.trim();
  if (trimmedQuery.isEmpty) return;

  final payload = <String, dynamic>{
    'search_scope': searchScope.trim().isEmpty ? 'track' : searchScope.trim(),
    if (metadata != null) ...metadata,
  };

  try {
    await proxyControlHttpClient
        .post(
          buildProxyUri('/search_interaction'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'user_scope_id': currentAuthenticatedUserId ?? 'guest',
            'query': trimmedQuery,
            'result_count': resultCount,
            'source': 'app_${payload['search_scope']}_search',
            'occurred_at':
                (occurredAt ?? DateTime.now().toUtc()).millisecondsSinceEpoch /
                    1000.0,
            'metadata': payload,
          }),
        )
        .timeout(const Duration(seconds: 3));
  } catch (_) {
    // Search logging should never block the UI.
  }
}

final StreamController<String> _recommendationSignalController =
    StreamController<String>.broadcast();

Stream<String> get recommendationSignalStream =>
    _recommendationSignalController.stream;

void notifyRecommendationSignal([String reason = '']) {
  if (_recommendationSignalController.isClosed) return;
  _recommendationSignalController.add(reason);
}

const String _disableRecommendationTimeoutsRaw = String.fromEnvironment(
  'AURALIS_DISABLE_TIMEOUTS',
  defaultValue: '0',
);
const bool _disableRecommendationTimeouts =
    _disableRecommendationTimeoutsRaw == '1' ||
        _disableRecommendationTimeoutsRaw == 'true' ||
        _disableRecommendationTimeoutsRaw == 'TRUE';
const Duration _recentCloudSearchQueryTimeout = Duration(seconds: 2);
const Duration _recentCloudSearchQueryCacheTtl = Duration(seconds: 45);
List<String> _recentCloudSearchQueryCache = const [];
DateTime? _recentCloudSearchQueryCacheAt;
String _recentCloudSearchQueryCacheUserId = '';

Future<List<String>> getRecentCloudSearchQueries({int limit = 8}) async {
  final userId = currentAuthenticatedUserId;
  final now = DateTime.now();
  final cachedAt = _recentCloudSearchQueryCacheAt;
  final cacheMatchesUser = userId != null &&
      userId.isNotEmpty &&
      _recentCloudSearchQueryCacheUserId == userId;
  if (cachedAt != null &&
      cacheMatchesUser &&
      now.difference(cachedAt) < _recentCloudSearchQueryCacheTtl &&
      _recentCloudSearchQueryCache.isNotEmpty) {
    return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
  }
  final client = supabaseClientOrNull;
  if (client == null || userId == null || userId.isEmpty) {
    return const [];
  }

  try {
    final queryFuture = client
        .from('search_events')
        .select('query,result_count,created_at')
        .eq('user_id', userId)
        .order('created_at', ascending: false)
        .limit(limit * 4);
    final rows = _disableRecommendationTimeouts
        ? await queryFuture
        : await queryFuture.timeout(_recentCloudSearchQueryTimeout);
    if (rows.isEmpty) return const [];

    final weighted = <String, double>{};
    for (var index = 0; index < rows.length; index++) {
      final query = rows[index]['query']?.toString().trim();
      if (query == null || query.isEmpty) continue;
      final normalized = query.toLowerCase();
      final resultCount = _parseInt(rows[index]['result_count']);
      final recencyWeight = (rows.length - index).toDouble();
      final resultWeight = resultCount > 0 ? 1.0 : 0.35;
      weighted.update(
        normalized,
        (value) => value + recencyWeight + resultWeight,
        ifAbsent: () => recencyWeight + resultWeight,
      );
    }

    final ranked = weighted.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final results =
        ranked.take(limit).map((entry) => entry.key).toList(growable: false);
    _recentCloudSearchQueryCache = results;
    _recentCloudSearchQueryCacheAt = now;
    _recentCloudSearchQueryCacheUserId = userId;
    return results;
  } catch (error) {
    debugPrint('Cloud search query lookup failed: $error');
    if (cacheMatchesUser && _recentCloudSearchQueryCache.isNotEmpty) {
      return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
    }
    return const [];
  }
}

class ResolvedStreamSource {
  final String url;
  final Map<String, String> headers;
  final DateTime fetchedAt;

  const ResolvedStreamSource({
    required this.url,
    required this.headers,
    required this.fetchedAt,
  });

  bool get isFresh =>
      DateTime.now().difference(fetchedAt) < const Duration(minutes: 25);
}

class TrackLyricsLine {
  final int index;
  final String text;
  final int? startTimeMs;
  final int? endTimeMs;

  const TrackLyricsLine({
    required this.index,
    required this.text,
    this.startTimeMs,
    this.endTimeMs,
  });

  factory TrackLyricsLine.fromJson(Map<String, dynamic> json) {
    return TrackLyricsLine(
      index: _parseInt(json['index']),
      text: (json['text'] ?? '').toString(),
      startTimeMs: json['start_time_ms'] == null
          ? null
          : _parseInt(json['start_time_ms']),
      endTimeMs:
          json['end_time_ms'] == null ? null : _parseInt(json['end_time_ms']),
    );
  }
}

class TrackLyricsState {
  final bool isLoading;
  final bool hasLyrics;
  final bool hasTimestamps;
  final String? videoId;
  final String? source;
  final String? error;
  final List<TrackLyricsLine> lines;

  const TrackLyricsState({
    this.isLoading = false,
    this.hasLyrics = false,
    this.hasTimestamps = false,
    this.videoId,
    this.source,
    this.error,
    this.lines = const [],
  });
}

class HistoryManager {
  static final _seedController = StreamController<String?>.broadcast();
  static final _trackController =
      StreamController<Map<String, dynamic>>.broadcast();
  static Stream<String?> get seedStream => _seedController.stream;
  static Stream<Map<String, dynamic>> get trackStream =>
      _trackController.stream;
  static String? _lastRecordedTrackId;
  static DateTime? _lastRecordedAt;
  static const int _legacyHistoryLimit = 50;
  static const int _historyEntryLimit = 180;
  static const Duration _duplicateSuppressWindow = Duration(seconds: 24);
  static const int _historySchemaVersion = 2;
  static Future<void>? _historySchemaFuture;

  static Future<File> get _file async {
    return getScopedDataFile('history.json');
  }

  static Future<File> get _entriesFile async {
    return getScopedDataFile('history_entries.json');
  }

  static Future<File> get _schemaFile async {
    return getScopedDataFile('history_schema.json');
  }

  static Future<void> _ensureHistorySchema() {
    return _historySchemaFuture ??= () async {
      try {
        final schemaFile = await _schemaFile;
        var currentVersion = 0;
        if (schemaFile.existsSync()) {
          try {
            final decoded = jsonDecode(await schemaFile.readAsString());
            if (decoded is Map) {
              currentVersion = _parseInt(decoded['version']);
            }
          } catch (_) {
            currentVersion = 0;
          }
        }

        if (currentVersion < _historySchemaVersion) {
          await clearLocalListeningHistory();
          await schemaFile.writeAsString(
            jsonEncode({'version': _historySchemaVersion}),
          );
        }
      } catch (error) {
        debugPrint('History schema migration failed: $error');
      }
    }();
  }

  static Future<void> clearLocalListeningHistory() async {
    try {
      final historyFile = await _file;
      final entriesFile = await _entriesFile;
      await historyFile.writeAsString('[]');
      await entriesFile.writeAsString('[]');
      _lastRecordedTrackId = null;
      _lastRecordedAt = null;
      if (!_seedController.isClosed) {
        _seedController.add(null);
      }
    } catch (error) {
      debugPrint('History reset failed: $error');
    }
  }

  static Future<List<String>> _readLocalHistory() async {
    await _ensureHistorySchema();
    final f = await _file;
    if (!f.existsSync()) return const [];
    try {
      final raw = (await f.readAsString()).trim();
      if (raw.isEmpty) return const [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return const [];
      return decoded
          .map((entry) => entry?.toString() ?? '')
          .where((entry) => entry.isNotEmpty)
          .toList(growable: false);
    } catch (error) {
      debugPrint('History cache parse failed, resetting local history: $error');
      try {
        await f.writeAsString('[]');
      } catch (_) {}
      return const [];
    }
  }

  static bool _looksMeaningfulText(String? value) {
    final text = value?.trim() ?? '';
    if (text.isEmpty) return false;
    return text.toLowerCase() != 'unknown track';
  }

  static bool _looksMeaningfulArtist(String? value) {
    final text = value?.trim() ?? '';
    if (text.isEmpty) return false;
    return text.toLowerCase() != 'unknown artist';
  }

  static Map<String, dynamic>? _normalizeHistoryEntry(dynamic rawEntry) {
    if (rawEntry is! Map) return null;
    final entry = Map<String, dynamic>.from(rawEntry);
    final snapshot = _lastTrackSnapshotFromRawTrack(
      entry,
      localPath: entry['local_path']?.toString(),
    );
    if (snapshot == null) return null;
    final playedAt = entry['played_at']?.toString().trim();
    return {
      ...snapshot,
      'played_at': playedAt?.isNotEmpty == true
          ? playedAt
          : DateTime.now().toUtc().toIso8601String(),
    };
  }

  static Future<List<Map<String, dynamic>>> _readLocalHistoryEntries() async {
    await _ensureHistorySchema();
    final file = await _entriesFile;
    if (!file.existsSync()) return const [];
    try {
      final raw = (await file.readAsString()).trim();
      if (raw.isEmpty) return const [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return const [];
      return decoded
          .map(_normalizeHistoryEntry)
          .whereType<Map<String, dynamic>>()
          .toList(growable: false);
    } catch (error) {
      debugPrint('History entry cache parse failed, resetting entries: $error');
      try {
        await file.writeAsString('[]');
      } catch (_) {}
      return const [];
    }
  }

  static Future<void> _writeLocalHistoryEntries(
    List<Map<String, dynamic>> entries,
  ) async {
    final file = await _entriesFile;
    await file.writeAsString(jsonEncode(entries));
  }

  static Map<String, dynamic> _mergeSnapshotWithExisting(
    Map<String, dynamic> snapshot,
    Map<String, dynamic>? existing,
  ) {
    if (existing == null) return snapshot;
    final merged = Map<String, dynamic>.from(snapshot);
    if (!_looksMeaningfulText(merged['title']?.toString()) &&
        _looksMeaningfulText(existing['title']?.toString())) {
      merged['title'] = existing['title'];
    }
    if (!_looksMeaningfulArtist(merged['channel']?.toString()) &&
        _looksMeaningfulArtist(existing['channel']?.toString())) {
      merged['channel'] = existing['channel'];
      merged['author'] = existing['author'] ?? existing['channel'];
      merged['artist'] = existing['artist'] ?? existing['channel'];
    }
    for (final key in const [
      'thumbnail',
      'album',
      'album_title',
      'album_id',
      'local_path',
    ]) {
      final current = merged[key]?.toString().trim() ?? '';
      final fallback = existing[key]?.toString().trim() ?? '';
      if (current.isEmpty && fallback.isNotEmpty) {
        merged[key] = existing[key];
      }
    }
    if (_parseInt(merged['duration']) <= 0 &&
        _parseInt(existing['duration']) > 0) {
      merged['duration'] = _parseInt(existing['duration']);
    }
    return merged;
  }

  static Future<void> _writeLegacySeedHistory(String videoId) async {
    final f = await _file;
    List<String> history = await _readLocalHistory();
    history.remove(videoId);
    history.insert(0, videoId);
    if (history.length > _legacyHistoryLimit) {
      history = history.sublist(0, _legacyHistoryLimit);
    }
    await f.writeAsString(jsonEncode(history));
  }

  static Future<void> _recordCloudPlayEvent(String videoId) async {
    final client = supabaseClientOrNull;
    final userId = currentAuthenticatedUserId;
    if (client == null || userId == null || userId.isEmpty) return;
    try {
      await client.from('play_events').insert({
        'user_id': userId,
        'track_id': videoId,
        'event_type': 'play',
      });
    } catch (e) {
      debugPrint("Cloud history write error: $e");
    }
  }

  static Future<void> addHistory(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    await addHistoryTrack({
      'id': videoId,
      'videoId': videoId,
      'title': 'Unknown Track',
    });
  }

  static Future<void> addHistoryTrack(
    dynamic rawTrack, {
    String? localPath,
  }) async {
    try {
      await _ensureHistorySchema();
      final snapshot = _lastTrackSnapshotFromRawTrack(
        rawTrack,
        localPath: localPath ??
            (rawTrack is Map ? rawTrack['local_path']?.toString() : null),
      );
      final videoId = extractTrackId(snapshot);
      if (snapshot == null || videoId == null || videoId.isEmpty) return;

      final now = DateTime.now().toUtc();
      if (_lastRecordedTrackId == videoId &&
          _lastRecordedAt != null &&
          now.difference(_lastRecordedAt!) < _duplicateSuppressWindow) {
        return;
      }
      _lastRecordedTrackId = videoId;
      _lastRecordedAt = now;

      final existingEntries = await _readLocalHistoryEntries();
      Map<String, dynamic>? previous;
      for (final entry in existingEntries) {
        if (extractTrackId(entry) == videoId) {
          previous = entry;
          break;
        }
      }
      final mergedSnapshot = _mergeSnapshotWithExisting(snapshot, previous);
      final entry = {
        ...mergedSnapshot,
        'played_at': now.toIso8601String(),
      };
      final nextEntries = <Map<String, dynamic>>[
        entry,
        ...existingEntries,
      ];
      if (nextEntries.length > _historyEntryLimit) {
        nextEntries.removeRange(_historyEntryLimit, nextEntries.length);
      }

      await Future.wait([
        _writeLegacySeedHistory(videoId),
        _writeLocalHistoryEntries(nextEntries),
      ]);
      _seedController.add(videoId);
      if (!_trackController.isClosed) {
        _trackController.add(Map<String, dynamic>.from(entry));
      }
      notifyRecommendationSignal(videoId);
      unawaited(
        recordProxyInteractionEvent(
          'play',
          trackId: videoId,
          rawTrack: entry,
          occurredAt: now,
        ),
      );
      unawaited(_recordCloudPlayEvent(videoId));
    } catch (e) {
      debugPrint("History Write Error: $e");
    }
  }

  static Future<void> removeHistoryTrack(
    String? videoId, {
    Map<String, dynamic>? rawTrack,
    bool recordNegativeSignal = true,
  }) async {
    final normalizedId = videoId?.trim() ?? '';
    if (normalizedId.isEmpty) return;
    try {
      await _ensureHistorySchema();
      final existingEntries = await _readLocalHistoryEntries();
      final remainingEntries = existingEntries
          .where((entry) => extractTrackId(entry) != normalizedId)
          .toList(growable: false);
      final remainingSeeds = remainingEntries
          .map(extractTrackId)
          .whereType<String>()
          .where((id) => id.isNotEmpty)
          .take(_legacyHistoryLimit)
          .toList(growable: false);
      await Future.wait([
        _writeLocalHistoryEntries(
            List<Map<String, dynamic>>.from(remainingEntries)),
        (await _file).writeAsString(jsonEncode(remainingSeeds)),
      ]);
      if (_lastRecordedTrackId == normalizedId) {
        _lastRecordedTrackId = null;
        _lastRecordedAt = null;
      }
      if (!_seedController.isClosed) {
        _seedController
            .add(remainingSeeds.isEmpty ? null : remainingSeeds.first);
      }
      notifyRecommendationSignal('history_remove:$normalizedId');
      if (recordNegativeSignal) {
        unawaited(
          recordProxyInteractionEvent(
            'skip',
            trackId: normalizedId,
            rawTrack: rawTrack,
            metadata: {
              'reason': 'history_delete',
              'removed_from': 'local_history',
            },
          ),
        );
      }
    } catch (error) {
      debugPrint('History delete failed: $error');
    }
  }

  static Future<String?> getLatestSeed() async {
    try {
      final entries = await _readLocalHistoryEntries();
      if (entries.isNotEmpty) {
        final latestTrackId = extractTrackId(entries.first);
        if (latestTrackId != null && latestTrackId.isNotEmpty) {
          return latestTrackId;
        }
      }

      final history = await _readLocalHistory();
      if (history.isNotEmpty) return history.first;
    } catch (e) {
      debugPrint("History Read Error: $e");
    }
    return null;
  }

  static Future<String?> getRecommendationSeed() async {
    try {
      final recentSnapshots = await getLastPlayedTrackSnapshots(limit: 6);
      if (recentSnapshots.isNotEmpty) {
        final recentTrackId = extractTrackId(recentSnapshots.first);
        if (recentTrackId != null && recentTrackId.isNotEmpty) {
          return recentTrackId;
        }
      }
    } catch (error) {
      debugPrint('Recommendation seed lookup failed: $error');
    }
    return getLatestSeed();
  }

  static Future<List<Map<String, dynamic>>> getRecentTrackSnapshots({
    int limit = 8,
    bool unique = true,
  }) async {
    final entries = await _readLocalHistoryEntries();
    if (entries.isEmpty) return const [];
    final results = <Map<String, dynamic>>[];
    final seen = <String>{};
    for (final entry in entries) {
      final trackId = extractTrackId(entry);
      if (trackId == null || trackId.isEmpty) continue;
      if (unique && !seen.add(trackId)) continue;
      results.add(normalizeTrack(entry));
      if (results.length >= limit) break;
    }
    return results;
  }

  static Future<List<Map<String, dynamic>>> getLastPlayedTrackSnapshots({
    int limit = 8,
  }) async {
    return getRecentTrackSnapshots(limit: limit, unique: true);
  }

  static Future<List<Map<String, dynamic>>> getFrequentlyPlayedTrackSnapshots({
    int limit = 8,
  }) async {
    final entries = await _readLocalHistoryEntries();
    if (entries.isEmpty) return const [];

    final scores = <String, double>{};
    final latestById = <String, Map<String, dynamic>>{};
    final total = entries.length;
    for (var index = 0; index < entries.length; index++) {
      final entry = entries[index];
      final trackId = extractTrackId(entry);
      if (trackId == null || trackId.isEmpty) continue;
      latestById.putIfAbsent(trackId, () => normalizeTrack(entry));
      final recencyWeight = ((total - index) / max(total, 1)) * 1.35;
      scores.update(
        trackId,
        (current) => current + 1.0 + recencyWeight,
        ifAbsent: () => 1.0 + recencyWeight,
      );
    }

    final ranked = scores.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final results = <Map<String, dynamic>>[];
    for (final entry in ranked) {
      final snapshot = latestById[entry.key];
      if (snapshot == null) continue;
      results.add(snapshot);
      if (results.length >= limit) break;
    }
    return results;
  }

  static Future<List<String>> getRecentSeeds({int limit = 8}) async {
    final ordered = <String>[];
    final seen = <String>{};

    void addTrackId(String? trackId) {
      if (trackId == null || trackId.isEmpty || !seen.add(trackId)) return;
      ordered.add(trackId);
    }

    try {
      final recentSnapshots = await getRecentTrackSnapshots(limit: limit * 2);
      for (final snapshot in recentSnapshots) {
        addTrackId(extractTrackId(snapshot));
        if (ordered.length >= limit) {
          return ordered;
        }
      }

      final history = await _readLocalHistory();
      for (final trackId in history) {
        addTrackId(trackId);
        if (ordered.length >= limit) {
          return ordered;
        }
      }
    } catch (error) {
      debugPrint('Recent seed lookup failed: $error');
    }

    return ordered;
  }

  static Future<List<String>> getFrequentlyPlayedTrackIds(
      {int limit = 8}) async {
    final scores = <String, double>{};

    void addWeight(String? trackId, double weight) {
      final normalized = trackId?.trim();
      if (normalized == null || normalized.isEmpty) return;
      scores.update(
        normalized,
        (current) => current + weight,
        ifAbsent: () => weight,
      );
    }

    try {
      final snapshotTracks =
          await getFrequentlyPlayedTrackSnapshots(limit: limit * 2);
      for (var index = 0; index < snapshotTracks.length; index++) {
        addWeight(
          extractTrackId(snapshotTracks[index]),
          (snapshotTracks.length - index) * 1.65,
        );
      }

      final localHistory = await _readLocalHistory();
      for (var index = 0; index < localHistory.length; index++) {
        addWeight(localHistory[index], (localHistory.length - index) * 0.65);
      }
    } catch (error) {
      debugPrint('Frequently played lookup failed: $error');
    }

    final ranked = scores.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    return ranked.take(limit).map((entry) => entry.key).toList(growable: false);
  }
}

// Provides the singleton FFI audio engine
final audioEngineProvider = Provider<AudioEngineFFI>((ref) {
  final engine = AudioEngineFFI();
  if (engine.isAvailable) {
    engine.initEngine();
  }
  return engine;
});

class PlayerState {
  final bool isPlaying;
  final bool isLooping;
  final String currentTrackName;
  final bool isDownloading;
  final String? thumbnail;
  final int duration;
  final int filesize;
  final int currentPosition;
  final int currentPositionMs;
  final String? artist;
  final String? videoId;
  final int sleepTimerRemainingSeconds;

  PlayerState({
    this.isPlaying = false,
    this.isLooping = false,
    this.currentTrackName = 'No track loaded',
    this.isDownloading = false,
    this.thumbnail,
    this.duration = 0,
    this.filesize = 0,
    this.currentPosition = 0,
    this.currentPositionMs = 0,
    this.artist,
    this.videoId,
    this.sleepTimerRemainingSeconds = 0,
  });

  PlayerState copyWith(
      {bool? isPlaying,
      bool? isLooping,
      String? currentTrackName,
      bool? isDownloading,
      String? thumbnail,
      int? duration,
      int? filesize,
      int? currentPosition,
      int? currentPositionMs,
      String? artist,
      String? videoId,
      int? sleepTimerRemainingSeconds}) {
    final nextCurrentPosition = currentPosition ?? this.currentPosition;
    return PlayerState(
      isPlaying: isPlaying ?? this.isPlaying,
      isLooping: isLooping ?? this.isLooping,
      currentTrackName: currentTrackName ?? this.currentTrackName,
      isDownloading: isDownloading ?? this.isDownloading,
      thumbnail: thumbnail ?? this.thumbnail,
      duration: duration ?? this.duration,
      filesize: filesize ?? this.filesize,
      currentPosition: nextCurrentPosition,
      currentPositionMs: currentPositionMs ??
          (currentPosition != null
              ? currentPosition * 1000
              : this.currentPositionMs),
      artist: artist ?? this.artist,
      videoId: videoId ?? this.videoId,
      sleepTimerRemainingSeconds:
          sleepTimerRemainingSeconds ?? this.sleepTimerRemainingSeconds,
    );
  }
}

class AudioPlayerNotifier extends StateNotifier<PlayerState> {
  static const int _initialQueuePrepareLookahead = 3;
  static const int _historyCommitMinSeconds = 12;
  static const double _historyCommitMinRatio = 0.35;
  final AudioEngineFFI audioEngine;
  Map<String, dynamic>? _restorableTrackMeta;
  Future<void> Function()? onTrackCompleted;
  Future<void> Function(String? videoId)? onTrackChanged;
  Timer? _playbackTimer;
  final streamPlayer = AudioPlayer(useLazyPreparation: false);
  final List<AudioPlayer> _prefetchPlayers = List<AudioPlayer>.generate(
    2,
    (_) => AudioPlayer(
      handleInterruptions: false,
      androidApplyAudioAttributes: false,
      handleAudioSessionActivation: false,
      useLazyPreparation: false,
    ),
  );
  bool _activeStream = false;
  final Map<String, ResolvedStreamSource> _streamCache = {};
  final Map<String, Future<ResolvedStreamSource>> _pendingStreamLookups = {};
  StreamSubscription<Duration>? _streamPositionSub;
  StreamSubscription<Duration?>? _streamDurationSub;
  StreamSubscription<dynamic>? _streamPlaybackSub;
  StreamSubscription<int?>? _streamCurrentIndexSub;
  Future<void> _streamCommandQueue = Future<void>.value();
  LockCachingAudioSource? _cachedStreamAudioSource;
  String? _cachedStreamVideoId;
  int _streamLoadVersion = 0;
  bool _desiredStreamPlaying = false;
  String? _completedTrackIdNotified;
  bool _streamTransitionInProgress = false;
  bool _managedQueueActive = false;
  List<Map<String, dynamic>> _managedQueueTracks = const [];
  final List<String?> _prefetchedTrackIds = <String?>[null, null];
  final Set<String> _unavailableStreamTrackIds = <String>{};
  final Map<String, String> _unavailableStreamReasons = <String, String>{};
  int _latencySummaryProbeCounter = 0;
  final Map<String, DateTime> _recentPrepareBatches = <String, DateTime>{};
  final Map<String, Future<void>> _pendingPrepareBatches =
      <String, Future<void>>{};
  Timer? _sleepTimer;
  DateTime? _sleepTimerEndsAt;
  final Map<String, Future<void>> _fullPrefetchTasks = {};
  bool _streamFailureRecoveryInProgress = false;
  Map<String, dynamic>? _pendingHistorySnapshot;
  String? _historyCommittedTrackId;

  AudioPlayerNotifier(this.audioEngine) : super(PlayerState()) {
    audioEngine.pause(); // Kill ghost audio surviving hot restarts
    _bindStreamPlayer();
    for (final player in _prefetchPlayers) {
      unawaited(player.setVolume(0));
    }

    if (globalAudioHandler is AuralisAudioHandler) {
      final handler = globalAudioHandler as AuralisAudioHandler;
      handler.onPlay = play;
      handler.onPause = pause;
      handler.onSeek = (Duration d) => seek(d.inSeconds);
      handler.onFastForward = () => seek(state.currentPosition + 10);
      handler.onRewind = () => seek(state.currentPosition - 10);
      handler.onStop = () => stopPlayback(resetState: true);
    }
    unawaited(_restoreRememberedTrack());
  }

  @override
  set state(PlayerState value) {
    final previousState = super.state;
    bool shouldBroadcast = super.state.isPlaying != value.isPlaying ||
        super.state.currentTrackName != value.currentTrackName ||
        super.state.isDownloading != value.isDownloading ||
        (super.state.currentPosition - value.currentPosition).abs() > 1;

    super.state = value;

    if (previousState.videoId != value.videoId) {
      _completedTrackIdNotified = null;
      _historyCommittedTrackId = null;
      final activeTrackId = value.videoId?.trim();
      final pendingTrackId = extractTrackId(_pendingHistorySnapshot);
      if (activeTrackId == null ||
          activeTrackId.isEmpty ||
          pendingTrackId != activeTrackId) {
        _pendingHistorySnapshot = null;
      }
      unawaited(onTrackChanged?.call(value.videoId) ?? Future<void>.value());
    }

    if (shouldBroadcast) {
      _broadcastState();
    }
  }

  void _notifyTrackCompletedIfNeeded() {
    if (_streamTransitionInProgress) return;
    if (_managedQueueActive) {
      final currentIndex = streamPlayer.currentIndex ?? 0;
      if (currentIndex >= 0 && currentIndex < _managedQueueTracks.length - 1) {
        return;
      }
    }
    final videoId = state.videoId;
    if (videoId == null || videoId.isEmpty) return;
    if (_completedTrackIdNotified == videoId) return;
    unawaited(_commitPendingHistoryIfEligible(force: true));
    _completedTrackIdNotified = videoId;
    unawaited(onTrackCompleted?.call() ?? Future<void>.value());
  }

  void _broadcastState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    final processingState = state.isDownloading
        ? AudioProcessingState.buffering
        : state.videoId == null || state.videoId!.isEmpty
            ? AudioProcessingState.idle
            : (!state.isPlaying &&
                    state.duration > 0 &&
                    state.currentPosition >= state.duration)
                ? AudioProcessingState.completed
                : AudioProcessingState.ready;
    final currentQueueIndex = _managedQueueActive
        ? (streamPlayer.currentIndex ?? 0)
            .clamp(
                0,
                _managedQueueTracks.isEmpty
                    ? 0
                    : _managedQueueTracks.length - 1)
            .toInt()
        : null;

    handler.broadcastState(PlaybackState(
      controls: [
        MediaControl.skipToPrevious,
        MediaControl.rewind,
        if (state.isPlaying) MediaControl.pause else MediaControl.play,
        MediaControl.fastForward,
        MediaControl.skipToNext,
      ],
      systemActions: const <MediaAction>{
        MediaAction.seek,
        MediaAction.seekForward,
        MediaAction.seekBackward,
      },
      androidCompactActionIndices: const [0, 2, 4],
      processingState: processingState,
      playing: state.isPlaying,
      updatePosition: Duration(seconds: state.currentPosition),
      bufferedPosition: Duration(seconds: state.currentPosition),
      speed: state.isPlaying ? 1.0 : 0.0,
      queueIndex: currentQueueIndex,
    ));

    handler.broadcastMediaItem(_currentMediaItem());
  }

  void _broadcastStoppedState() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    handler.broadcastState(
      PlaybackState(
        controls: [MediaControl.play],
        processingState: AudioProcessingState.idle,
        playing: false,
        updatePosition: Duration.zero,
        bufferedPosition: Duration.zero,
      ),
    );
    handler.broadcastQueue(const []);
  }

  void refreshMediaSession() {
    if (state.videoId == null || state.videoId!.isEmpty) {
      _broadcastStoppedState();
      return;
    }
    _broadcastManagedQueue();
    _broadcastState();
  }

  Future<File> _lastTrackSnapshotFile() =>
      getScopedDataFile('last_track_snapshot.json');

  Future<void> _persistRememberedTrack() async {
    final snapshot = _restorableTrackMeta;
    if (snapshot == null) return;
    try {
      final file = await _lastTrackSnapshotFile();
      await file.writeAsString(jsonEncode(snapshot));
    } catch (error) {
      debugPrint('Last track snapshot save failed: $error');
    }
  }

  Future<void> _restoreRememberedTrack() async {
    try {
      final file = await _lastTrackSnapshotFile();
      if (!file.existsSync()) return;
      final raw = (await file.readAsString()).trim();
      if (raw.isEmpty) return;
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return;
      final snapshot = Map<String, dynamic>.from(decoded);
      final trackId = extractTrackId(snapshot);
      if (trackId == null || trackId.isEmpty) return;
      final localPath = snapshot['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          !_hasValidDownloadedAudio(localPath)) {
        snapshot.remove('local_path');
      }
      _restorableTrackMeta = snapshot;
      if (!mounted) return;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: snapshot['title']?.toString() ?? 'Unknown Track',
        thumbnail: snapshot['thumbnail']?.toString(),
        artist:
            (snapshot['author'] ?? snapshot['artist'] ?? snapshot['channel'])
                ?.toString(),
        videoId: trackId,
        duration: _parseInt(snapshot['duration']),
        currentPosition: 0,
        currentPositionMs: 0,
      );
    } catch (error) {
      debugPrint('Last track snapshot restore failed: $error');
    }
  }

  void _rememberTrackMeta(
    dynamic rawTrack, {
    String? localPath,
  }) {
    final snapshot =
        _lastTrackSnapshotFromRawTrack(rawTrack, localPath: localPath);
    if (snapshot == null) return;
    _restorableTrackMeta = snapshot;
    _stageHistorySnapshot(snapshot);
    unawaited(_persistRememberedTrack());
  }

  void _stageHistorySnapshot(
    dynamic rawTrack, {
    String? localPath,
  }) {
    final snapshot =
        _lastTrackSnapshotFromRawTrack(rawTrack, localPath: localPath);
    final trackId = extractTrackId(snapshot);
    if (snapshot == null || trackId == null || trackId.isEmpty) return;
    _pendingHistorySnapshot = Map<String, dynamic>.from(snapshot);
    _historyCommittedTrackId = null;
  }

  bool _isCurrentTrackHistoryEligible() {
    final activeTrackId = state.videoId?.trim();
    final pendingTrackId = extractTrackId(_pendingHistorySnapshot);
    if (activeTrackId == null ||
        activeTrackId.isEmpty ||
        pendingTrackId == null ||
        pendingTrackId.isEmpty ||
        pendingTrackId != activeTrackId) {
      return false;
    }
    final positionSeconds = state.currentPosition;
    final durationSeconds = state.duration;
    if (positionSeconds <= 0) return false;
    if (durationSeconds <= 0) {
      return positionSeconds >= _historyCommitMinSeconds;
    }
    final requiredSeconds = max(
      8,
      min(
        20,
        (durationSeconds * _historyCommitMinRatio).round(),
      ),
    );
    return positionSeconds >= requiredSeconds ||
        positionSeconds >= durationSeconds - 1;
  }

  Future<void> _commitPendingHistoryIfEligible({
    bool force = false,
  }) async {
    final snapshot = _pendingHistorySnapshot;
    final pendingTrackId = extractTrackId(snapshot);
    if (snapshot == null ||
        pendingTrackId == null ||
        pendingTrackId.isEmpty ||
        _historyCommittedTrackId == pendingTrackId) {
      return;
    }
    final activeTrackId = state.videoId?.trim();
    if (!force &&
        (activeTrackId == null ||
            activeTrackId.isEmpty ||
            activeTrackId != pendingTrackId ||
            !_isCurrentTrackHistoryEligible())) {
      return;
    }
    if (force &&
        activeTrackId != null &&
        activeTrackId.isNotEmpty &&
        activeTrackId != pendingTrackId) {
      return;
    }
    _historyCommittedTrackId = pendingTrackId;
    await HistoryManager.addHistoryTrack(
      snapshot,
      localPath: snapshot['local_path']?.toString(),
    );
  }

  Future<bool> _restoreRememberedTrackForPlayback() async {
    final snapshot = _restorableTrackMeta;
    if (snapshot == null) return false;
    final localPath = snapshot['local_path']?.toString();
    if (localPath != null && localPath.isNotEmpty) {
      final loaded = await loadLocalWithMeta(localPath, snapshot);
      if (loaded) {
        return true;
      }
      _restorableTrackMeta = {
        ...snapshot,
      }..remove('local_path');
      unawaited(_persistRememberedTrack());
    }
    final trackId = extractTrackId(snapshot);
    if (trackId == null || trackId.isEmpty) return false;
    await streamYoutube(trackId, snapshot);
    return state.videoId == trackId;
  }

  MediaItem _currentMediaItem() {
    return MediaItem(
      id: state.videoId ?? 'current_track',
      album: state.artist ?? 'EBB',
      title: state.currentTrackName,
      artist: state.artist ?? 'Unknown Artist',
      duration: Duration(seconds: state.duration),
      artUri: state.thumbnail != null ? Uri.parse(state.thumbnail!) : null,
    );
  }

  MediaItem _mediaItemFromTrack(Map<String, dynamic> track) {
    final duration = _parseInt(track['duration']);
    final thumbnail = track['thumbnail']?.toString();
    return MediaItem(
      id: extractTrackId(track) ?? track['title']?.toString() ?? 'track',
      album:
          track['album']?.toString() ?? track['album_title']?.toString() ?? '',
      title: track['title']?.toString() ?? 'Unknown Track',
      artist: _trackArtist(track) ?? 'Unknown Artist',
      duration: duration > 0 ? Duration(seconds: duration) : null,
      artUri: thumbnail != null && thumbnail.isNotEmpty
          ? Uri.tryParse(thumbnail)
          : null,
    );
  }

  void _broadcastManagedQueue() {
    if (globalAudioHandler is! AuralisAudioHandler) return;
    final handler = globalAudioHandler as AuralisAudioHandler;
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) {
      handler.broadcastQueue(const []);
      return;
    }
    handler.broadcastQueue(
      _managedQueueTracks.map(_mediaItemFromTrack).toList(growable: false),
    );
  }

  void _bindStreamPlayer() {
    _streamDurationSub = streamPlayer.durationStream.listen((duration) {
      if (!_activeStream || duration == null) return;
      final seconds = duration.inSeconds;
      if (seconds > 0 && seconds != state.duration) {
        state = state.copyWith(duration: seconds);
      }
    });

    _streamCurrentIndexSub = streamPlayer.currentIndexStream.listen((index) {
      if (!_activeStream || !_managedQueueActive) return;
      if (index == null || index < 0 || index >= _managedQueueTracks.length) {
        return;
      }
      _applyManagedTrackMetadata(index, resetPosition: true);
      unawaited(_refreshManagedQueueWarmup());
    });

    _streamPositionSub = streamPlayer.positionStream.listen((position) {
      if (!_activeStream) return;
      final seconds = position.inSeconds;
      final milliseconds = position.inMilliseconds;
      if (seconds != state.currentPosition ||
          milliseconds != state.currentPositionMs) {
        state = state.copyWith(
          currentPosition: seconds,
          currentPositionMs: milliseconds,
        );
        unawaited(_commitPendingHistoryIfEligible());
      }
    });

    _streamPlaybackSub = streamPlayer.playerStateStream.listen(
      (playerState) {
        if (!_activeStream) return;

        final processingState = playerState.processingState;
        final isCompleted = processingState == ProcessingState.completed;
        final isBuffering = playerState.playing &&
            (processingState == ProcessingState.loading ||
                processingState == ProcessingState.buffering);
        final nextDuration = streamPlayer.duration?.inSeconds ?? state.duration;
        final nextDurationMs = nextDuration * 1000;
        final cappedPosition =
            isCompleted ? nextDuration : streamPlayer.position.inSeconds;
        final cappedPositionMs =
            isCompleted ? nextDurationMs : streamPlayer.position.inMilliseconds;

        _playbackTimer?.cancel();
        state = state.copyWith(
          isPlaying: playerState.playing && !isCompleted,
          isDownloading: isBuffering,
          duration: nextDuration > 0 ? nextDuration : state.duration,
          currentPosition: cappedPosition
              .clamp(
                0,
                nextDuration > 0 ? nextDuration : cappedPosition,
              )
              .toInt(),
          currentPositionMs: cappedPositionMs
              .clamp(
                0,
                nextDuration > 0 ? nextDurationMs : cappedPositionMs,
              )
              .toInt(),
        );
        final shouldNotifyCompletion = isCompleted &&
            !_streamTransitionInProgress &&
            (!_managedQueueActive || !streamPlayer.hasNext);

        if (shouldNotifyCompletion) {
          _notifyTrackCompletedIfNeeded();
        } else {
          _completedTrackIdNotified = null;
        }
      },
      onError: (Object error, StackTrace stackTrace) {
        if (!_activeStream) return;
        state = state.copyWith(
          isPlaying: false,
          isDownloading: false,
          currentTrackName: 'Stream failed: $error',
        );
        unawaited(_handleStreamFailure(error));
      },
    );
  }

  Future<T> _runStreamCommand<T>(Future<T> Function() action) {
    final completer = Completer<T>();
    _streamCommandQueue = _streamCommandQueue
        .catchError((Object _, StackTrace __) {})
        .then((_) async {
      try {
        completer.complete(await action());
      } catch (error, stackTrace) {
        completer.completeError(error, stackTrace);
      }
    });
    return completer.future;
  }

  Future<void> _clearOldCachedStream(LockCachingAudioSource? oldSource) async {
    if (oldSource == null) return;
    try {
      final cacheFile = await oldSource.cacheFile;
      if (cacheFile.path.contains(
          '${Platform.pathSeparator}stream_cache${Platform.pathSeparator}')) {
        return;
      }
      await oldSource.clearCache();
    } catch (_) {
      // Cache eviction should stay silent.
    }
  }

  void _updateSleepTimerState() {
    if (_sleepTimerEndsAt == null) {
      if (state.sleepTimerRemainingSeconds != 0) {
        state = state.copyWith(sleepTimerRemainingSeconds: 0);
      }
      return;
    }
    final remaining =
        _sleepTimerEndsAt!.difference(DateTime.now()).inSeconds.clamp(0, 86400);
    if (remaining != state.sleepTimerRemainingSeconds) {
      state = state.copyWith(sleepTimerRemainingSeconds: remaining);
    }
    if (remaining <= 0) {
      _sleepTimer?.cancel();
      _sleepTimer = null;
      _sleepTimerEndsAt = null;
      unawaited(stopPlayback());
    }
  }

  Future<void> setSleepTimer(Duration? duration) async {
    _sleepTimer?.cancel();
    _sleepTimer = null;
    if (duration == null || duration <= Duration.zero) {
      _sleepTimerEndsAt = null;
      state = state.copyWith(sleepTimerRemainingSeconds: 0);
      return;
    }
    _sleepTimerEndsAt = DateTime.now().add(duration);
    _updateSleepTimerState();
    _sleepTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _updateSleepTimerState();
    });
  }

  Future<void> _setStreamSource(
    String videoId,
    ResolvedStreamSource source,
  ) async {
    final previousSource = _cachedStreamAudioSource;
    final nextSource = await _buildCachingSourceForVideoId(
      videoId,
      headers: source.headers,
      urlOverride: source.url,
    );

    _cachedStreamAudioSource = nextSource;
    _cachedStreamVideoId = videoId;
    await streamPlayer.stop();
    await streamPlayer.setAudioSource(nextSource, preload: true);
    if (!identical(previousSource, nextSource)) {
      unawaited(_clearOldCachedStream(previousSource));
    }
  }

  Future<void> _setLocalSource(String path) async {
    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = null;
    await streamPlayer.stop();
    await streamPlayer.setFilePath(path, preload: true);
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }
  }

  Future<void> _resumeStreamPlayback() async {
    _desiredStreamPlaying = true;
    if (streamPlayer.audioSource == null) {
      return;
    }
    try {
      if (streamPlayer.processingState == ProcessingState.completed) {
        await streamPlayer.seek(Duration.zero);
      }
      unawaited(
        streamPlayer.play().catchError((Object error, StackTrace stackTrace) {
          if (!_activeStream || !_desiredStreamPlaying) return;
          state = state.copyWith(
            isPlaying: false,
            isDownloading: false,
            currentTrackName: 'Stream failed: $error',
          );
          unawaited(_handleStreamFailure(error));
        }),
      );
    } catch (error) {
      if (!_activeStream || !_desiredStreamPlaying) return;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Stream failed: $error',
      );
      unawaited(_handleStreamFailure(error));
    }
  }

  @override
  void dispose() {
    _playbackTimer?.cancel();
    _sleepTimer?.cancel();
    _streamPositionSub?.cancel();
    _streamDurationSub?.cancel();
    _streamPlaybackSub?.cancel();
    _streamCurrentIndexSub?.cancel();
    unawaited(stopPlayback(resetState: false));
    for (final player in _prefetchPlayers) {
      unawaited(player.dispose());
    }
    unawaited(streamPlayer.dispose());
    super.dispose();
  }

  Uri _proxyUri(String path) => buildProxyUri(path);

  ResolvedStreamSource? _freshStreamSource(String videoId) {
    final cached = _streamCache[videoId];
    if (cached != null && cached.isFresh) {
      return cached;
    }
    return null;
  }

  ResolvedStreamSource _buildProxyStreamSource(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    return ResolvedStreamSource(
      url: _proxyUri('/proxy_stream/$videoId').toString(),
      headers: const {},
      fetchedAt: fetchedAt ?? DateTime.now(),
    );
  }

  ResolvedStreamSource _markPreparedStream(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    final source = _buildProxyStreamSource(videoId, fetchedAt: fetchedAt);
    _streamCache[videoId] = source;
    return source;
  }

  bool _hasLocalFileForTrack(Map<String, dynamic> track) {
    final localPath = track['local_path']?.toString();
    return localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync();
  }

  void _registerUnavailableTrack(String? videoId, [String? reason]) {
    final normalizedId = videoId?.trim();
    if (normalizedId == null || normalizedId.isEmpty) return;
    _unavailableStreamTrackIds.add(normalizedId);
    if (reason != null && reason.trim().isNotEmpty) {
      _unavailableStreamReasons[normalizedId] = reason.trim();
    }
    _streamCache.remove(normalizedId);
  }

  bool _isKnownUnavailableRemoteTrack(Map<String, dynamic> track) {
    if (_hasLocalFileForTrack(track)) return false;
    final videoId = extractTrackId(track);
    return videoId != null && _unavailableStreamTrackIds.contains(videoId);
  }

  Future<Map<String, dynamic>> _resolveManagedQueueSources(
    List<Map<String, dynamic>> tracks, {
    required int preferredInitialIndex,
  }) async {
    final playableTracks = <Map<String, dynamic>>[];
    final sources = <AudioSource>[];
    var resolvedInitialIndex = preferredInitialIndex;

    for (var i = 0; i < tracks.length; i++) {
      final track = tracks[i];
      final videoId = extractTrackId(track);
      if (_isKnownUnavailableRemoteTrack(track)) {
        if (i < resolvedInitialIndex) {
          resolvedInitialIndex -= 1;
        }
        continue;
      }
      try {
        final source = await _buildManagedAudioSource(track);
        playableTracks.add(track);
        sources.add(source);
      } catch (error) {
        _registerUnavailableTrack(videoId, error.toString());
        if (i < resolvedInitialIndex) {
          resolvedInitialIndex -= 1;
        }
      }
    }

    if (playableTracks.isEmpty) {
      resolvedInitialIndex = 0;
    } else {
      resolvedInitialIndex =
          resolvedInitialIndex.clamp(0, playableTracks.length - 1).toInt();
    }

    return {
      'tracks': playableTracks,
      'sources': sources,
      'initialIndex': resolvedInitialIndex,
    };
  }

  Future<void> _handleStreamFailure(Object error) async {
    if (_streamFailureRecoveryInProgress || !_activeStream) return;
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) {
      return;
    }

    _streamFailureRecoveryInProgress = true;
    try {
      final failedIndex = streamPlayer.currentIndex ?? 0;
      if (failedIndex < 0 || failedIndex >= _managedQueueTracks.length) {
        return;
      }

      final failedTrack = _managedQueueTracks[failedIndex];
      final failedVideoId = extractTrackId(failedTrack) ?? state.videoId;
      _registerUnavailableTrack(failedVideoId, error.toString());

      final remainingTracks = _managedQueueTracks
          .where((track) => !_isKnownUnavailableRemoteTrack(track))
          .toList(growable: false);

      if (remainingTracks.isEmpty) {
        await stopPlayback(resetState: false);
        state = state.copyWith(
          isPlaying: false,
          isDownloading: false,
          currentTrackName: 'Track unavailable right now',
          videoId: failedVideoId,
        );
        _managedQueueActive = false;
        _managedQueueTracks = const [];
        _broadcastManagedQueue();
        return;
      }

      final nextIndex =
          failedIndex.clamp(0, remainingTracks.length - 1).toInt();
      await configureManagedQueue(
        remainingTracks,
        initialIndex: nextIndex,
        autoplay: _desiredStreamPlaying,
      );
    } finally {
      _streamFailureRecoveryInProgress = false;
    }
  }

  String? _trackArtist(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString();
  }

  String _sanitizeCacheKey(String value) {
    return value.replaceAll(RegExp(r'[^A-Za-z0-9_\-]'), '_');
  }

  Future<Directory> _streamCacheDirectory() async {
    final userDir = await getScopedUserDirectory();
    final cacheDir = Directory('${userDir.path}/stream_cache');
    if (!cacheDir.existsSync()) {
      cacheDir.createSync(recursive: true);
    }
    return cacheDir;
  }

  Future<File> _cacheFileForVideoId(String videoId) async {
    final cacheDir = await _streamCacheDirectory();
    return File('${cacheDir.path}/${_sanitizeCacheKey(videoId)}.audio');
  }

  Future<File> _cacheMarkerForVideoId(String videoId) async {
    final cacheFile = await _cacheFileForVideoId(videoId);
    return File('${cacheFile.path}.done');
  }

  Future<LockCachingAudioSource> _buildCachingSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) async {
    final cacheFile = await _cacheFileForVideoId(videoId);
    return LockCachingAudioSource(
      Uri.parse(urlOverride ?? _proxyUri('/proxy_stream/$videoId').toString()),
      headers: headers == null || headers.isEmpty ? null : headers,
      cacheFile: cacheFile,
      tag: tag,
    );
  }

  Future<void> _logLatencySummaryIfDue() async {
    if (!kDebugMode) return;
    _latencySummaryProbeCounter++;
    if (_latencySummaryProbeCounter % 24 != 0) return;
    try {
      final response = await appHttpClient
          .get(_proxyUri('/latency_summary'))
          .timeout(const Duration(seconds: 8));
      if (response.statusCode != 200) return;
      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final summary = payload['summary'];
      if (summary is! Map) return;
      debugPrint(
        '[EBB] latency-summary '
        'resolveAvg=${summary['avg_resolve_ms']}ms '
        'chunkAvg=${summary['avg_chunk_ms']}ms '
        'serverAvg=${summary['avg_server_ms']}ms '
        'prefixAvg=${summary['avg_cached_prefix_bytes']} '
        'resolveHit=${summary['resolve_cache_hit_rate']} '
        'chunkHit=${summary['chunk_cache_hit_rate']}',
      );
    } catch (_) {
      // Summary logging is best-effort only.
    }
  }

  String _prepareBatchKey(
    Iterable<String> ids, {
    String? currentVideoId,
    required bool activeQueue,
    required int lookahead,
  }) {
    return [
      activeQueue ? '1' : '0',
      currentVideoId ?? '',
      '$lookahead',
      ids.join(','),
    ].join('|');
  }

  void _prunePrepareBatchTracking() {
    final now = DateTime.now();
    _recentPrepareBatches.removeWhere(
      (_, startedAt) => now.difference(startedAt) > const Duration(minutes: 2),
    );
    if (_recentPrepareBatches.length <= 120) return;
    final ordered = _recentPrepareBatches.entries.toList()
      ..sort((a, b) => a.value.compareTo(b.value));
    final overflow = ordered.length - 80;
    for (var i = 0; i < overflow; i++) {
      _recentPrepareBatches.remove(ordered[i].key);
    }
  }

  Future<void> _prepareQueueSession(
    Iterable<String?> videoIds, {
    String? currentVideoId,
    bool activeQueue = false,
    int lookahead = 8,
  }) async {
    final idsToWarm = <String>[];
    final seen = <String>{};
    for (final rawId in videoIds) {
      final videoId = rawId?.toString();
      if (videoId == null || videoId.isEmpty || !seen.add(videoId)) {
        continue;
      }
      idsToWarm.add(videoId);
      if (idsToWarm.length >= lookahead) {
        break;
      }
    }
    if (idsToWarm.isEmpty) return;

    final idsNeedingWarm = idsToWarm
        .where((videoId) => _freshStreamSource(videoId) == null)
        .toList(growable: false);
    if (idsNeedingWarm.isEmpty) return;

    final batchKey = _prepareBatchKey(
      idsNeedingWarm,
      currentVideoId: currentVideoId,
      activeQueue: activeQueue,
      lookahead: lookahead,
    );

    final pendingBatch = _pendingPrepareBatches[batchKey];
    if (pendingBatch != null) {
      await pendingBatch;
      return;
    }

    final lastPreparedAt = _recentPrepareBatches[batchKey];
    final cooldown =
        activeQueue ? const Duration(seconds: 6) : const Duration(seconds: 12);
    if (lastPreparedAt != null &&
        DateTime.now().difference(lastPreparedAt) < cooldown) {
      return;
    }

    Future<void> runPrepareTask() async {
      final res = await appHttpClient
          .post(
            _proxyUri('/prepare_session'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'video_ids': idsNeedingWarm,
              'current_video_id': currentVideoId,
              'active_queue': activeQueue,
              'lookahead': lookahead,
            }),
          )
          .timeout(const Duration(seconds: 18));

      if (res.statusCode != 200) {
        throw Exception('Prepared session failed: ${res.statusCode}');
      }

      final data = jsonDecode(res.body) as Map<String, dynamic>;
      final prepared = data['prepared'] ?? data['streams'];
      final failed = data['failed'];
      if (failed is Map) {
        failed.forEach((key, dynamic value) {
          final failureReason = value is Map
              ? value['message']?.toString() ?? value['code']?.toString()
              : value?.toString();
          _registerUnavailableTrack(key.toString(), failureReason);
        });
      }
      if (prepared is Map) {
        prepared.forEach((key, dynamic value) {
          _markPreparedStream(key.toString());
          if (value is Map) {
            debugPrint(
              '[EBB] prepared ${key.toString()} '
              'resolveMs=${value['resolve_ms']} '
              'chunkMs=${value['chunk_ms']} '
              'targetBytes=${value['target_chunk_bytes']} '
              'chunkBytes=${value['cached_prefix_bytes']}',
            );
          }
        });
      }
      if (activeQueue) {
        unawaited(_logLatencySummaryIfDue());
      }
      _recentPrepareBatches[batchKey] = DateTime.now();
      _prunePrepareBatchTracking();
    }

    final pendingTask = runPrepareTask();
    _pendingPrepareBatches[batchKey] = pendingTask;
    try {
      await pendingTask;
    } catch (_) {
      final fallbackCount =
          idsNeedingWarm.length < 4 ? idsNeedingWarm.length : 4;
      for (final id in idsNeedingWarm.take(fallbackCount)) {
        unawaited(prewarmStream(id));
      }
    } finally {
      _pendingPrepareBatches.remove(batchKey);
    }
  }

  Future<void> _cleanupFullPrefetchCache(Set<String> keepIds) async {
    final cacheDir = await _streamCacheDirectory();
    if (!cacheDir.existsSync()) return;
    final keepNames = keepIds.map(_sanitizeCacheKey).toSet();
    for (final entity in cacheDir.listSync(followLinks: false)) {
      if (entity is! File) continue;
      final name =
          entity.uri.pathSegments.isEmpty ? '' : entity.uri.pathSegments.last;
      if (name.isEmpty) continue;
      final baseName = name.endsWith('.done')
          ? name.substring(0, name.length - 5)
          : name.replaceAll('.audio', '');
      if (keepNames.contains(baseName)) continue;
      try {
        entity.deleteSync();
      } catch (_) {}
    }
  }

  Future<void> _prefetchTrackToTempStorage(String videoId) async {
    final existing = _fullPrefetchTasks[videoId];
    if (existing != null) {
      return existing;
    }

    final task = () async {
      final cacheFile = await _cacheFileForVideoId(videoId);
      final markerFile = await _cacheMarkerForVideoId(videoId);
      if (cacheFile.existsSync() &&
          cacheFile.lengthSync() > 100000 &&
          markerFile.existsSync()) {
        return;
      }

      final tempFile = File('${cacheFile.path}.part');
      if (tempFile.existsSync()) {
        try {
          tempFile.deleteSync();
        } catch (_) {}
      }

      try {
        final request =
            http.Request('GET', _proxyUri('/proxy_stream/$videoId'));
        final response = await appHttpClient.send(request);
        if (response.statusCode != 200) {
          throw Exception('Prefetch failed: ${response.statusCode}');
        }
        final sink = tempFile.openWrite();
        await for (final chunk in response.stream) {
          sink.add(chunk);
        }
        await sink.close();
        if (cacheFile.existsSync()) {
          try {
            cacheFile.deleteSync();
          } catch (_) {}
        }
        await tempFile.rename(cacheFile.path);
        await markerFile.writeAsString(DateTime.now().toIso8601String());
        debugPrint(
            '[EBB] full-prefetched $videoId bytes=${cacheFile.lengthSync()}');
      } catch (error) {
        debugPrint('[EBB] full-prefetch failed for $videoId: $error');
        if (tempFile.existsSync()) {
          try {
            tempFile.deleteSync();
          } catch (_) {}
        }
      }
    }();

    _fullPrefetchTasks[videoId] = task;
    await task.whenComplete(() => _fullPrefetchTasks.remove(videoId));
  }

  Future<void> prefetchFixedQueueTracks(
    List<Map<String, dynamic>> tracks, {
    required int currentIndex,
    int count = 2,
  }) async {
    if (tracks.isEmpty || currentIndex < 0 || currentIndex >= tracks.length) {
      return;
    }

    final keepIds = <String>{};
    final currentTrackId = extractTrackId(tracks[currentIndex]);
    if (currentTrackId != null && currentTrackId.isNotEmpty) {
      keepIds.add(currentTrackId);
    }

    final targetIds = <String>[];
    for (var i = currentIndex + 1; i < tracks.length; i++) {
      final track = tracks[i];
      if (isTrackHidden(track)) continue;
      final id = extractTrackId(track);
      if (id == null || id.isEmpty) continue;
      targetIds.add(id);
      keepIds.add(id);
      if (targetIds.length >= count) break;
    }

    await _cleanupFullPrefetchCache(keepIds);
    for (final id in targetIds) {
      unawaited(_prefetchTrackToTempStorage(id));
    }
  }

  Future<void> _prefetchManagedQueueAhead({int lookahead = 2}) async {
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) return;
    final currentIndex = currentManagedQueueIndex ?? 0;
    final targets = <Map<String, dynamic>>[];
    for (var i = currentIndex + 1; i < _managedQueueTracks.length; i++) {
      final track = _managedQueueTracks[i];
      if (isTrackHidden(track)) continue;
      targets.add(track);
      if (targets.length >= lookahead) break;
    }

    for (var slot = 0; slot < _prefetchPlayers.length; slot++) {
      final track = slot < targets.length ? targets[slot] : null;
      final trackId = track == null ? null : extractTrackId(track);
      if (_prefetchedTrackIds[slot] == trackId) {
        continue;
      }

      if (track == null || trackId == null || trackId.isEmpty) {
        _prefetchedTrackIds[slot] = null;
        try {
          await _prefetchPlayers[slot].stop();
        } catch (_) {}
        continue;
      }

      final localPath = track['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          File(localPath).existsSync()) {
        _prefetchedTrackIds[slot] = trackId;
        continue;
      }

      try {
        final source = await _buildCachingSourceForVideoId(trackId, tag: track);
        _prefetchedTrackIds[slot] = trackId;
        await _prefetchPlayers[slot].stop();
        await _prefetchPlayers[slot].setAudioSource(source, preload: true);
      } catch (_) {
        _prefetchedTrackIds[slot] = null;
      }
    }
  }

  Future<void> _refreshManagedQueueWarmup({int lookahead = 12}) async {
    if (!_managedQueueActive || _managedQueueTracks.isEmpty) return;
    final currentIndex = currentManagedQueueIndex ?? 0;
    if (currentIndex < 0 || currentIndex >= _managedQueueTracks.length) return;
    final ids = <String?>[];
    for (var i = currentIndex; i < _managedQueueTracks.length; i++) {
      final track = _managedQueueTracks[i];
      if (i != currentIndex && isTrackHidden(track)) continue;
      ids.add(extractTrackId(track));
      if (ids.length >= lookahead) break;
    }
    await _prepareQueueSession(
      ids,
      currentVideoId: extractTrackId(_managedQueueTracks[currentIndex]),
      activeQueue: true,
      lookahead: lookahead,
    );
    unawaited(_prefetchManagedQueueAhead());
  }

  void _applyManagedTrackMetadata(
    int index, {
    bool resetPosition = false,
  }) {
    if (index < 0 || index >= _managedQueueTracks.length) return;
    final track = _managedQueueTracks[index];
    final videoId = extractTrackId(track);
    final hintedDuration = _parseInt(track['duration']);
    state = state.copyWith(
      currentTrackName: track['title']?.toString() ?? state.currentTrackName,
      thumbnail: track['thumbnail']?.toString() ?? state.thumbnail,
      artist: _trackArtist(track) ?? state.artist,
      videoId: videoId,
      duration: hintedDuration > 0 ? hintedDuration : state.duration,
      currentPosition: resetPosition ? 0 : state.currentPosition,
      currentPositionMs: resetPosition ? 0 : state.currentPositionMs,
    );
    _rememberTrackMeta(track, localPath: track['local_path']?.toString());
  }

  Future<AudioSource> _buildManagedAudioSource(
    Map<String, dynamic> track,
  ) async {
    final localPath = track['local_path']?.toString();
    if (localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync()) {
      return AudioSource.file(localPath, tag: track);
    }

    final videoId = extractTrackId(track);
    if (videoId == null || videoId.isEmpty) {
      throw Exception('Missing track id for managed queue source');
    }
    if (_unavailableStreamTrackIds.contains(videoId)) {
      final reason = _unavailableStreamReasons[videoId];
      throw Exception(reason == null || reason.isEmpty
          ? 'Track unavailable for streaming'
          : reason);
    }

    return _buildCachingSourceForVideoId(videoId, tag: track);
  }

  Future<void> configureManagedQueue(
    List<Map<String, dynamic>> tracks, {
    required int initialIndex,
    bool autoplay = true,
  }) async {
    if (tracks.isEmpty || initialIndex < 0 || initialIndex >= tracks.length) {
      return;
    }

    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final currentTrack = normalizedTracks[initialIndex];
    final currentTrackId = extractTrackId(currentTrack);
    final lookaheadIds = <String?>[
      extractTrackId(currentTrack),
      ...normalizedTracks
          .skip(initialIndex + 1)
          .where((track) => !isTrackHidden(track))
          .take(_initialQueuePrepareLookahead - 1)
          .map(extractTrackId),
    ];

    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = autoplay;
    _activeStream = true;
    _managedQueueActive = true;
    _managedQueueTracks = normalizedTracks;
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();
    _broadcastManagedQueue();

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName:
          currentTrack['title']?.toString() ?? 'Connecting Queue...',
      thumbnail: currentTrack['thumbnail']?.toString(),
      artist: _trackArtist(currentTrack),
      videoId: null,
      duration: 0,
      currentPosition: 0,
      currentPositionMs: 0,
    );

    await _prepareQueueSession(
      lookaheadIds,
      currentVideoId: currentTrackId,
      activeQueue: true,
      lookahead: _initialQueuePrepareLookahead,
    );
    if (loadVersion != _streamLoadVersion) return;

    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: initialIndex,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    final resolvedInitialIndex = (resolvedQueue['initialIndex'] as int?) ?? 0;
    if (playableTracks.isEmpty || sources.isEmpty) {
      _streamTransitionInProgress = false;
      _managedQueueActive = false;
      _managedQueueTracks = const [];
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'No playable tracks available',
        videoId: null,
      );
      _broadcastManagedQueue();
      return;
    }
    if (loadVersion != _streamLoadVersion) return;

    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = extractTrackId(playableTracks[resolvedInitialIndex]);
    _managedQueueTracks = playableTracks;

    await _runStreamCommand(() async {
      if (loadVersion != _streamLoadVersion) return;
      await streamPlayer.stop();
      await streamPlayer.setAudioSources(
        sources,
        initialIndex: resolvedInitialIndex,
        preload: true,
      );
      await streamPlayer.setLoopMode(
        state.isLooping ? LoopMode.one : LoopMode.off,
      );
    });
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }
    if (loadVersion != _streamLoadVersion) return;

    _streamTransitionInProgress = false;
    _applyManagedTrackMetadata(resolvedInitialIndex, resetPosition: true);
    state = state.copyWith(
      isDownloading: false,
      duration: streamPlayer.duration?.inSeconds ?? state.duration,
    );
    unawaited(_prefetchManagedQueueAhead());
    unawaited(_refreshManagedQueueWarmup());
    if (_desiredStreamPlaying) {
      await _resumeStreamPlayback();
    }
  }

  Future<void> appendManagedQueueTracks(
    List<Map<String, dynamic>> tracks,
  ) async {
    if (!_managedQueueActive || tracks.isEmpty) return;
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    await prewarmStreams(
      normalizedTracks.take(3).map(extractTrackId),
    );
    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: 0,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    if (playableTracks.isEmpty || sources.isEmpty) {
      return;
    }
    await _runStreamCommand(() async {
      await streamPlayer.addAudioSources(sources);
    });
    _managedQueueTracks = [..._managedQueueTracks, ...playableTracks];
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> insertManagedQueueTracks(
    int index,
    List<Map<String, dynamic>> tracks,
  ) async {
    if (!_managedQueueActive || tracks.isEmpty) return;
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final insertIndex = index.clamp(0, _managedQueueTracks.length).toInt();
    await prewarmStreams(
      normalizedTracks.take(3).map(extractTrackId),
    );
    final resolvedQueue = await _resolveManagedQueueSources(
      normalizedTracks,
      preferredInitialIndex: 0,
    );
    final playableTracks =
        List<Map<String, dynamic>>.from(resolvedQueue['tracks'] as List);
    final sources = List<AudioSource>.from(resolvedQueue['sources'] as List);
    if (playableTracks.isEmpty || sources.isEmpty) {
      return;
    }
    await _runStreamCommand(() async {
      for (var offset = 0; offset < sources.length; offset++) {
        await streamPlayer.insertAudioSource(
            insertIndex + offset, sources[offset]);
      }
    });
    final updated = [..._managedQueueTracks];
    updated.insertAll(insertIndex, playableTracks);
    _managedQueueTracks = updated;
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> moveManagedQueueItem(int oldIndex, int newIndex) async {
    if (!_managedQueueActive) return;
    if (oldIndex < 0 ||
        oldIndex >= _managedQueueTracks.length ||
        newIndex < 0 ||
        newIndex > _managedQueueTracks.length) {
      return;
    }

    var targetIndex = newIndex;
    if (targetIndex > oldIndex) {
      targetIndex -= 1;
    }
    targetIndex = targetIndex.clamp(0, _managedQueueTracks.length - 1);
    if (oldIndex == targetIndex) return;

    await _runStreamCommand(() async {
      await streamPlayer.moveAudioSource(oldIndex, targetIndex);
    });

    final updated = [..._managedQueueTracks];
    updated.insert(targetIndex, updated.removeAt(oldIndex));
    _managedQueueTracks = updated;
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> removeManagedQueueItem(int index) async {
    if (!_managedQueueActive) return;
    if (index < 0 || index >= _managedQueueTracks.length) return;
    await _runStreamCommand(() async {
      await streamPlayer.removeAudioSourceAt(index);
    });
    final updated = [..._managedQueueTracks]..removeAt(index);
    _managedQueueTracks = updated;
    if (_managedQueueTracks.isEmpty) {
      _managedQueueActive = false;
    }
    _broadcastManagedQueue();
    unawaited(_refreshManagedQueueWarmup());
  }

  Future<void> playManagedQueueIndex(int index) async {
    if (!_managedQueueActive) return;
    if (index < 0 || index >= _managedQueueTracks.length) return;
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seek(Duration.zero, index: index);
    });
    _streamTransitionInProgress = false;
    _applyManagedTrackMetadata(index, resetPosition: true);
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  Future<void> skipManagedQueueNext() async {
    if (!_managedQueueActive || !streamPlayer.hasNext) return;
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seekToNext();
    });
    _streamTransitionInProgress = false;
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  Future<void> skipManagedQueuePrevious() async {
    if (!_managedQueueActive) return;
    if (streamPlayer.position > const Duration(seconds: 3)) {
      await _runStreamCommand(() async {
        await streamPlayer.seek(Duration.zero);
      });
      return;
    }
    if (!streamPlayer.hasPrevious) {
      await _runStreamCommand(() async {
        await streamPlayer.seek(Duration.zero);
      });
      return;
    }
    _streamTransitionInProgress = true;
    await _runStreamCommand(() async {
      await streamPlayer.seekToPrevious();
    });
    _streamTransitionInProgress = false;
    unawaited(_refreshManagedQueueWarmup());
    await _resumeStreamPlayback();
  }

  bool get hasManagedQueue => _managedQueueActive;
  int? get currentManagedQueueIndex =>
      _managedQueueActive ? streamPlayer.currentIndex : null;

  Future<ResolvedStreamSource> _fetchStreamSource(String videoId) async {
    final cached = _freshStreamSource(videoId);
    if (cached != null) {
      return cached;
    }

    final stopwatch = Stopwatch()..start();
    final res = await appHttpClient
        .post(
          _proxyUri('/prepare_session'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'video_ids': [videoId],
          }),
        )
        .timeout(const Duration(seconds: 18));

    if (res.statusCode != 200) {
      throw Exception('Prepared stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final prepared = data['prepared'];
    final failed = data['failed'];
    if (failed is Map && failed.containsKey(videoId)) {
      final failure = failed[videoId];
      final failureReason = failure is Map
          ? failure['message']?.toString() ?? failure['code']?.toString()
          : failure?.toString();
      _registerUnavailableTrack(videoId, failureReason);
      throw Exception(failureReason ?? 'Track unavailable for streaming');
    }
    if (prepared is! Map || !prepared.containsKey(videoId)) {
      _registerUnavailableTrack(videoId, 'Track unavailable for streaming');
      throw Exception('Track unavailable for streaming');
    }
    final metrics = prepared[videoId];
    if (metrics is Map) {
      debugPrint(
        '[EBB] prepared $videoId '
        'resolveMs=${metrics['resolve_ms']} '
        'chunkMs=${metrics['chunk_ms']} '
        'chunkBytes=${metrics['cached_prefix_bytes']} '
        'serverMs=${data['server_ms']}',
      );
    }
    debugPrint(
      '[EBB] prepare_session[$videoId] totalMs=${stopwatch.elapsedMilliseconds}',
    );
    return _markPreparedStream(videoId);
  }

  Future<ResolvedStreamSource> _fetchDirectStreamSource(String videoId) async {
    final res = await appHttpClient
        .get(_proxyUri('/direct_url/$videoId'))
        .timeout(const Duration(seconds: 15));

    if (res.statusCode != 200) {
      throw Exception('Direct stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final directUrl = data['url']?.toString();
    if (directUrl == null || directUrl.isEmpty) {
      throw Exception('Proxy returned an empty direct stream URL');
    }

    return ResolvedStreamSource(
      url: directUrl,
      headers: _parseHeaders(data['headers']),
      fetchedAt: DateTime.now(),
    );
  }

  Future<ResolvedStreamSource> _resolveStreamSource(String videoId) {
    final cached = _freshStreamSource(videoId);
    if (cached != null) {
      return Future<ResolvedStreamSource>.value(cached);
    }

    final pending = _pendingStreamLookups[videoId];
    if (pending != null) {
      return pending;
    }

    final lookup = _fetchStreamSource(videoId);
    _pendingStreamLookups[videoId] = lookup;
    lookup.whenComplete(() {
      if (identical(_pendingStreamLookups[videoId], lookup)) {
        _pendingStreamLookups.remove(videoId);
      }
    });
    return lookup;
  }

  Future<void> prewarmStream(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    try {
      await _resolveStreamSource(videoId);
    } catch (_) {
      // Prewarm should stay silent. Playback will surface real errors later.
    }
  }

  Future<void> prewarmStreams(Iterable<String?> videoIds) async {
    await _prepareQueueSession(videoIds, lookahead: 18);
  }

  bool _hasValidDownloadedAudio(String path) {
    try {
      final file = File(path);
      return file.existsSync() && file.lengthSync() >= 10000;
    } catch (_) {
      return false;
    }
  }

  Future<bool> loadLocalFile(String path, String trackName) async {
    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = false;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName: trackName,
      thumbnail: null,
      artist: null,
      videoId: null,
      duration: 0,
      currentPosition: 0,
      currentPositionMs: 0,
    );

    try {
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setLocalSource(path);
      });
      if (loadVersion != _streamLoadVersion) return false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: trackName,
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: streamPlayer.duration?.inSeconds ?? 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      _rememberTrackMeta({
        'title': trackName,
      }, localPath: path);
      return true;
    } catch (error) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: $error',
        thumbnail: null,
        artist: null,
        videoId: null,
        duration: 0,
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }
  }

  Future<bool> loadLocalWithMeta(String path, Map<String, dynamic> meta) async {
    final loadVersion = ++_streamLoadVersion;
    _desiredStreamPlaying = false;
    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    _playbackTimer?.cancel();
    audioEngine.pause();

    if (!_hasValidDownloadedAudio(path)) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: downloaded file is invalid',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }

    state = state.copyWith(
      isPlaying: false,
      isDownloading: true,
      currentTrackName: meta['title'] ?? 'Unknown Track',
      thumbnail: meta['thumbnail'],
      duration: _parseInt(meta['duration']),
      filesize: _parseInt(meta['filesize']),
      artist: meta['author'] ?? meta['artist'],
      videoId: meta['video_id'] ?? meta['id'],
      currentPosition: 0,
      currentPositionMs: 0,
    );

    try {
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setLocalSource(path);
      });
      if (loadVersion != _streamLoadVersion) return false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: meta['title'] ?? 'Unknown Track',
        thumbnail: meta['thumbnail'],
        duration:
            streamPlayer.duration?.inSeconds ?? _parseInt(meta['duration']),
        filesize: _parseInt(meta['filesize']),
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        currentPosition: 0,
        currentPositionMs: 0,
      );
      _rememberTrackMeta(meta, localPath: path);
      return true;
    } catch (error) {
      _activeStream = false;
      _streamTransitionInProgress = false;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Playback failed: $error',
        thumbnail: meta['thumbnail'],
        artist: meta['author'] ?? meta['artist'],
        videoId: meta['video_id'] ?? meta['id'],
        duration: _parseInt(meta['duration']),
        currentPosition: 0,
        currentPositionMs: 0,
      );
      return false;
    }
  }

  Future<bool> downloadAndLoadYoutube(String videoId, String outPath) async {
    state = state.copyWith(
        isDownloading: true,
        currentTrackName: 'Initializing Proxy Connection...',
        thumbnail: null,
        artist: null,
        duration: 0,
        filesize: 0,
        currentPosition: 0);
    // print("Initiating Proxy Request for: $videoId");

    try {
      // 1. Ask the Proxy Server to fetch the Video and extract metadata
      final titleStr = outPath.split('/').last.replaceAll('.mp3', '');
      final res = await appHttpClient.post(_proxyUri('/download'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId, "title": titleStr}));

      if (res.statusCode != 200) {
        throw Exception(
            "Proxy Refused Connection or Failed to DL: ${res.body}");
      }

      final meta = Map<String, dynamic>.from(jsonDecode(res.body) as Map);
      state = state.copyWith(
        currentTrackName: meta['title'] ?? 'Downloading...',
        thumbnail: meta['thumbnail'],
        duration: _parseInt(meta['duration']),
        filesize: _parseInt(meta['filesize']),
        artist: meta['author'],
      );
      // 2. Stream the byte data back from the proxy to the local flutter device
      final req = http.Request('GET', _proxyUri('/stream/$videoId'));
      final streamRes = await appHttpClient.send(req);

      if (streamRes.statusCode != 200) {
        throw Exception("Stream Error: ${streamRes.statusCode}");
      }

      final file = File(outPath);
      final sink = file.openWrite();

      await for (var chunk in streamRes.stream) {
        sink.add(chunk);
      }
      await sink.close();

      if (!_hasValidDownloadedAudio(outPath)) {
        throw Exception('Downloaded file is empty or corrupted');
      }

      final jsonPath = outPath.replaceAll('.mp3', '.json');
      meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';
      await File(jsonPath).writeAsString(jsonEncode(meta));
      unawaited(
        upsertCloudLibraryTrack({
          ...Map<String, dynamic>.from(meta as Map),
          'id': videoId,
          'videoId': videoId,
        }),
      );

      final loaded = await loadLocalWithMeta(outPath, meta);
      if (!loaded) {
        throw Exception(
            'Downloaded file saved, but the player could not load it');
      }
      state = state.copyWith(isDownloading: false);
      await play();
      return true;
    } catch (e) {
      state = state.copyWith(
          isDownloading: false, currentTrackName: 'Download failed: $e');

      try {
        final f = File(outPath);
        if (f.existsSync()) f.deleteSync();
        final jf = File(outPath.replaceAll('.mp3', '.json'));
        if (jf.existsSync()) jf.deleteSync();
      } catch (_) {}
      return false;
    }
  }

  Future<bool> downloadYoutubeBackground(
      String videoId, String outPath, String titleStr) async {
    try {
      final res = await appHttpClient.post(_proxyUri('/download'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId, "title": titleStr}));

      if (res.statusCode != 200) {
        throw Exception("Proxy Refused Connection or Failed to DL");
      }

      final meta = Map<String, dynamic>.from(jsonDecode(res.body) as Map);
      final req = http.Request('GET', _proxyUri('/stream/$videoId'));
      final streamRes = await appHttpClient.send(req);

      if (streamRes.statusCode != 200) {
        throw Exception("Stream Error: ${streamRes.statusCode}");
      }

      final file = File(outPath);
      final sink = file.openWrite();

      await for (var chunk in streamRes.stream) {
        sink.add(chunk);
      }
      await sink.close();

      if (!_hasValidDownloadedAudio(outPath)) {
        throw Exception('Downloaded file is empty or corrupted');
      }

      final jsonPath = outPath.replaceAll('.mp3', '.json');
      meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';
      await File(jsonPath).writeAsString(jsonEncode(meta));
      unawaited(
        upsertCloudLibraryTrack({
          ...Map<String, dynamic>.from(meta as Map),
          'id': videoId,
          'videoId': videoId,
        }),
      );
      return true;
    } catch (e) {
      try {
        final f = File(outPath);
        if (f.existsSync()) f.deleteSync();
        final jf = File(outPath.replaceAll('.mp3', '.json'));
        if (jf.existsSync()) jf.deleteSync();
      } catch (_) {}
      return false;
    }
  }

  Future<void> streamYoutube(
    String videoId,
    Map<String, dynamic> fallbackMeta,
  ) async {
    final loadVersion = ++_streamLoadVersion;
    final totalStopwatch = Stopwatch()..start();
    _desiredStreamPlaying = true;
    if (_activeStream &&
        state.videoId == videoId &&
        _cachedStreamVideoId == videoId) {
      await seek(0);
      await play();
      return;
    }

    _activeStream = true;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    _streamTransitionInProgress = true;
    audioEngine.pause();
    _playbackTimer?.cancel();

    state = state.copyWith(
        isDownloading: true,
        currentTrackName: fallbackMeta['title'] ?? 'Connecting Stream...',
        thumbnail: fallbackMeta['thumbnail'],
        artist: fallbackMeta['channel'] ?? fallbackMeta['author'],
        videoId: null,
        duration: 0,
        currentPosition: 0);

    try {
      final cachedPrepared = _freshStreamSource(videoId);
      final prepareStopwatch = Stopwatch()..start();
      final source = cachedPrepared ?? _buildProxyStreamSource(videoId);
      final prepareMs = prepareStopwatch.elapsedMilliseconds;
      if (cachedPrepared == null) {
        unawaited(prewarmStream(videoId));
      }
      if (loadVersion != _streamLoadVersion) return;
      final attachStopwatch = Stopwatch()..start();
      await _runStreamCommand(() async {
        if (loadVersion != _streamLoadVersion) return;
        await _setStreamSource(videoId, source);
      });
      if (loadVersion != _streamLoadVersion) return;
      final attachMs = attachStopwatch.elapsedMilliseconds;
      final dur = streamPlayer.duration?.inSeconds ??
          _parseInt(fallbackMeta['duration']);
      _streamTransitionInProgress = false;
      state = state.copyWith(
          currentTrackName: fallbackMeta['title'],
          duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
          isDownloading: false,
          videoId: videoId);
      _rememberTrackMeta({
        ...fallbackMeta,
        'id': videoId,
        'videoId': videoId,
      });
      if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
        await _resumeStreamPlayback();
      }
      debugPrint(
        '[EBB] stream $videoId prepared=${cachedPrepared != null} '
        'prepareMs=$prepareMs attachMs=$attachMs totalMs=${totalStopwatch.elapsedMilliseconds}',
      );
    } catch (e) {
      if (loadVersion != _streamLoadVersion) return;
      try {
        final directSource = await _fetchDirectStreamSource(videoId);
        await _runStreamCommand(() async {
          if (loadVersion != _streamLoadVersion) return;
          await _setStreamSource(videoId, directSource);
        });
        if (loadVersion != _streamLoadVersion) return;
        final dur = streamPlayer.duration?.inSeconds ??
            _parseInt(fallbackMeta['duration']);
        _streamTransitionInProgress = false;
        state = state.copyWith(
            currentTrackName: fallbackMeta['title'],
            duration: dur > 0 ? dur : _parseInt(fallbackMeta['duration']),
            isDownloading: false,
            videoId: videoId);
        _rememberTrackMeta({
          ...fallbackMeta,
          'id': videoId,
          'videoId': videoId,
        });
        if (loadVersion == _streamLoadVersion && _desiredStreamPlaying) {
          await _resumeStreamPlayback();
        }
        debugPrint(
          '[EBB] stream $videoId fell back to direct_url totalMs=${totalStopwatch.elapsedMilliseconds}',
        );
      } catch (fallbackError) {
        if (loadVersion != _streamLoadVersion) return;
        _streamTransitionInProgress = false;
        state = state.copyWith(
            isDownloading: false,
            currentTrackName: 'Stream failed: $fallbackError',
            videoId: null);
      }
    }
  }

  void _startTimer() {
    _playbackTimer?.cancel();
    _playbackTimer = Timer.periodic(const Duration(milliseconds: 200), (timer) {
      if (_activeStream) {
        timer.cancel();
        return;
      }
      if (state.duration == 0 || state.currentPosition < state.duration) {
        final pos = _activeStream
            ? streamPlayer.position.inSeconds
            : audioEngine.getPosition() ~/ 1000;
        if (pos != state.currentPosition) {
          state = state.copyWith(
            currentPosition: pos,
            currentPositionMs: pos * 1000,
          );
          unawaited(_commitPendingHistoryIfEligible());
        }
      } else {
        if (state.isLooping) {
          if (_activeStream) {
            streamPlayer.seek(Duration.zero);
          }
          state = state.copyWith(currentPosition: 0, currentPositionMs: 0);
        } else {
          _notifyTrackCompletedIfNeeded();
          pause();
        }
      }
    });
  }

  Future<void> play() async {
    if (!_activeStream &&
        streamPlayer.audioSource == null &&
        _restorableTrackMeta != null) {
      final restored = await _restoreRememberedTrackForPlayback();
      if (restored) {
        if (_activeStream) {
          await _resumeStreamPlayback();
        } else {
          audioEngine.play();
          state = state.copyWith(isPlaying: true);
          _startTimer();
        }
      }
      return;
    }

    if (_activeStream) {
      await _resumeStreamPlayback();
      return;
    }

    audioEngine.play();
    state = state.copyWith(isPlaying: true);
    _startTimer();
  }

  Future<void> pause() async {
    if (_activeStream) {
      _desiredStreamPlaying = false;
      try {
        await streamPlayer.pause();
      } catch (_) {}
      state = state.copyWith(isPlaying: false, isDownloading: false);
      return;
    }

    audioEngine.pause();
    state = state.copyWith(isPlaying: false);
    _playbackTimer?.cancel();
  }

  Future<void> stopPlayback({bool resetState = true}) async {
    _desiredStreamPlaying = false;
    _playbackTimer?.cancel();
    _sleepTimer?.cancel();
    _sleepTimer = null;
    _sleepTimerEndsAt = null;
    _completedTrackIdNotified = null;
    _streamTransitionInProgress = false;
    _managedQueueActive = false;
    _managedQueueTracks = const [];
    for (var i = 0; i < _prefetchedTrackIds.length; i++) {
      _prefetchedTrackIds[i] = null;
    }

    try {
      await streamPlayer.stop();
    } catch (_) {}
    for (final player in _prefetchPlayers) {
      try {
        await player.stop();
      } catch (_) {}
    }
    try {
      audioEngine.pause();
    } catch (_) {}

    final previousSource = _cachedStreamAudioSource;
    _cachedStreamAudioSource = null;
    _cachedStreamVideoId = null;
    _activeStream = false;
    if (previousSource != null) {
      unawaited(_clearOldCachedStream(previousSource));
    }

    if (resetState) {
      state = PlayerState(isLooping: state.isLooping);
    } else {
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentPosition: 0,
        currentPositionMs: 0,
        sleepTimerRemainingSeconds: 0,
      );
    }
    _broadcastStoppedState();
  }

  Future<void> seek(int seconds) async {
    final boundedSeconds = state.duration > 0
        ? seconds.clamp(0, state.duration).toInt()
        : (seconds < 0 ? 0 : seconds);
    if (_activeStream) {
      state = state.copyWith(
        currentPosition: boundedSeconds,
        currentPositionMs: boundedSeconds * 1000,
      );
      try {
        await streamPlayer.seek(Duration(seconds: boundedSeconds));
      } catch (_) {}
      return;
    }

    audioEngine.seek(boundedSeconds * 1000); // Send MS to C++
    state = state.copyWith(
      currentPosition: boundedSeconds,
      currentPositionMs: boundedSeconds * 1000,
    );
  }

  Future<void> toggleLoop(int startMs, int endMs) async {
    final newState = !state.isLooping;
    if (_activeStream) {
      await _runStreamCommand(() async {
        await streamPlayer.setLoopMode(newState ? LoopMode.one : LoopMode.off);
      });
    } else {
      audioEngine.setLoop(newState, startMs, endMs);
    }
    state = state.copyWith(isLooping: newState);
  }
}

// Removed isolateDownload as Flutter now uses HTTP asyncio over the Dart event loop.

final audioPlayerProvider =
    StateNotifierProvider<AudioPlayerNotifier, PlayerState>((ref) {
  final audioE = ref.watch(audioEngineProvider);
  return AudioPlayerNotifier(audioE);
});

// Search & Recommendation State Management
void _bumpSearchSignalWeight(
  Map<String, double> weights,
  String? value,
  double weight,
) {
  final normalized = value?.trim().toLowerCase();
  if (normalized == null || normalized.isEmpty) return;
  weights.update(
    normalized,
    (current) => current + weight,
    ifAbsent: () => weight,
  );
}

String? _extractSearchArtistHint(dynamic rawTrack) {
  if (rawTrack is! Map) return null;
  return rawTrack['channel']?.toString() ??
      rawTrack['author']?.toString() ??
      rawTrack['artist']?.toString();
}

String? _extractSearchAlbumHint(dynamic rawTrack) {
  if (rawTrack is! Map) return null;
  return rawTrack['album']?.toString() ?? rawTrack['album_title']?.toString();
}

bool _looksLikeSemanticTasteQuery(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return false;
  if (normalized.length > 38) return true;
  if (normalized.contains(' mix') ||
      normalized.contains(' playlist') ||
      normalized.contains(' songs') ||
      normalized.contains(' music')) {
    return true;
  }
  return _sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
}

bool _isMetadataHeavyQuery(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return false;
  const noisyPhrases = <String>[
    'deluxe remastered',
    'deluxe edition',
    'original soundtrack',
    'motion picture',
    'tribute to',
    'karaoke version',
    'radio edit',
  ];
  if (noisyPhrases.any(normalized.contains)) {
    return true;
  }
  const noisyTokens = <String>{
    'bonus',
    'deluxe',
    'edition',
    'karaoke',
    'mono',
    'original',
    'remaster',
    'remastered',
    'soundtrack',
    'stereo',
    'tribute',
    'version',
  };
  final hits =
      normalized.split(RegExp(r'\s+')).where(noisyTokens.contains).length;
  return hits >= 2;
}

Future<Map<String, dynamic>> _buildSemanticSearchRequestBody(
  Ref ref,
  String query, {
  required int limit,
}) async {
  final normalizedQuery = query.trim();
  final playlists = ref.read(playlistProvider);
  final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
  final storageScopeId = ref.read(authProvider).storageScopeId;
  final searchSignals = await Future.wait<Object?>([
    HistoryManager.getRecentTrackSnapshots(limit: 12),
    HistoryManager.getLastPlayedTrackSnapshots(limit: 8),
    HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 10),
    HistoryManager.getRecentSeeds(limit: 12),
    HistoryManager.getFrequentlyPlayedTrackIds(limit: 10),
    getRecentCloudSearchQueries(limit: 8),
  ]);

  final recentTrackSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[0] as List);
  final lastPlayedSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[1] as List);
  final topTrackSnapshots =
      List<Map<String, dynamic>>.from(searchSignals[2] as List);
  final recentTrackIds = List<String>.from(searchSignals[3] as List);
  final topTrackIds = List<String>.from(searchSignals[4] as List);
  final recentQueries = List<String>.from(searchSignals[5] as List);

  final artistWeights = <String, double>{};
  final albumWeights = <String, double>{};
  final queryWeights = <String, double>{};
  final blendedRecentQueries = <String>[];
  final libraryTrackIds = <String>[];
  final offlineTrackIds = <String>[];

  void addRecentQuery(String? value) {
    final normalized = value?.trim();
    if (normalized == null || normalized.isEmpty) return;
    if (!blendedRecentQueries.contains(normalized)) {
      blendedRecentQueries.add(normalized);
    }
  }

  void addAlbumWeight(String? value, double weight) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    albumWeights.update(
      normalized,
      (current) => current + weight,
      ifAbsent: () => weight,
    );
  }

  void addTrackHints(
    Iterable<Map<String, dynamic>> tracks, {
    required double artistWeight,
    required double albumWeight,
  }) {
    for (final track in tracks) {
      _bumpSearchSignalWeight(
        artistWeights,
        _extractSearchArtistHint(track),
        artistWeight,
      );
      _bumpSearchSignalWeight(
        queryWeights,
        _extractSearchAlbumHint(track),
        albumWeight * 0.8,
      );
      addAlbumWeight(_extractSearchAlbumHint(track), albumWeight);
    }
  }

  addRecentQuery(normalizedQuery);
  for (final queryValue in recentQueries) {
    addRecentQuery(queryValue);
  }

  addTrackHints(
    lastPlayedSnapshots,
    artistWeight: 1.45,
    albumWeight: 1.0,
  );
  addTrackHints(
    recentTrackSnapshots,
    artistWeight: 1.1,
    albumWeight: 0.82,
  );
  addTrackHints(
    topTrackSnapshots,
    artistWeight: 1.7,
    albumWeight: 1.18,
  );

  for (final playlist in playlists) {
    _bumpSearchSignalWeight(queryWeights, playlist.name, 1.25);
    for (final track in playlist.tracks.take(16)) {
      _bumpSearchSignalWeight(
        artistWeights,
        _extractSearchArtistHint(track),
        1.28,
      );
      _bumpSearchSignalWeight(
        queryWeights,
        _extractSearchAlbumHint(track),
        0.84,
      );
      addAlbumWeight(_extractSearchAlbumHint(track), 1.0);
    }
  }

  for (final track in libraryTracks.take(24)) {
    final normalizedTrack = normalizeTrack(track);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId != null && trackId.isNotEmpty) {
      libraryTrackIds.add(trackId);
      if (normalizedTrack['is_downloaded_locally'] == true) {
        offlineTrackIds.add(trackId);
      }
    }
    _bumpSearchSignalWeight(
      artistWeights,
      _extractSearchArtistHint(track),
      1.12,
    );
    _bumpSearchSignalWeight(
      queryWeights,
      _extractSearchAlbumHint(track),
      0.7,
    );
    addAlbumWeight(_extractSearchAlbumHint(track), 0.88);
  }

  _bumpSearchSignalWeight(queryWeights, normalizedQuery, 2.45);
  if (_looksLikeSemanticTasteQuery(normalizedQuery)) {
    _bumpSearchSignalWeight(queryWeights, normalizedQuery, 1.25);
  }

  for (final queryValue in blendedRecentQueries) {
    if (_looksLikeSemanticTasteQuery(queryValue)) {
      _bumpSearchSignalWeight(queryWeights, queryValue, 1.18);
    }
  }

  final rankedArtists = artistWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));
  final rankedAlbums = albumWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));
  final rankedQueries = queryWeights.entries.toList()
    ..sort((a, b) => b.value.compareTo(a.value));

  return {
    'query': normalizedQuery,
    'limit': limit,
    'user_scope_id': storageScopeId,
    'recent_track_ids': recentTrackIds,
    'top_track_ids': topTrackIds,
    'recent_track_snapshots': recentTrackSnapshots,
    'top_track_snapshots': topTrackSnapshots,
    'last_played_tracks': lastPlayedSnapshots,
    'recent_queries': blendedRecentQueries,
    'taste_queries':
        rankedQueries.take(8).map((entry) => entry.key).toList(growable: false),
    'artist_hints':
        rankedArtists.take(8).map((entry) => entry.key).toList(growable: false),
    'album_hints':
        rankedAlbums.take(6).map((entry) => entry.key).toList(growable: false),
    'playlist_names': playlists
        .map((playlist) => playlist.name)
        .take(10)
        .toList(growable: false),
    'library_track_ids': libraryTrackIds.take(28).toList(growable: false),
    'offline_track_ids': offlineTrackIds.take(28).toList(growable: false),
  };
}

Map<String, dynamic> _buildSuggestRequestBody(
  Ref ref,
  String query, {
  required int limit,
}) {
  final normalizedQuery = query.trim();
  final storageScopeId = ref.read(authProvider).storageScopeId;
  return {
    'query': normalizedQuery,
    'limit': limit,
    'user_scope_id': storageScopeId,
    'recent_queries':
        _recentCloudSearchQueryCache.take(4).toList(growable: false),
    'context_surface': 'suggest',
    'force_refresh': false,
  };
}

class _CachedSearchPayload {
  final Map<String, dynamic> payload;
  final DateTime storedAt;

  const _CachedSearchPayload({
    required this.payload,
    required this.storedAt,
  });
}

class _SearchFetchResult {
  final Map<String, dynamic>? payload;
  final String status;
  final int? statusCode;

  const _SearchFetchResult({
    required this.status,
    this.payload,
    this.statusCode,
  });

  bool get hasPayload => payload != null;
}

final Map<String, _CachedSearchPayload> _searchPayloadCache =
    <String, _CachedSearchPayload>{};
final Map<String, Future<_SearchFetchResult>> _searchPayloadForegroundInflight =
    <String, Future<_SearchFetchResult>>{};
final Map<String, Future<_SearchFetchResult>> _searchPayloadBackgroundInflight =
    <String, Future<_SearchFetchResult>>{};
const Duration _searchCacheFreshTtl = Duration(minutes: 4);
const Duration _searchCacheMaxTtl = Duration(minutes: 12);

String _searchPayloadCacheKey(
  Ref ref,
  String query, {
  required int limit,
}) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = ref.read(authProvider).storageScopeId;
  return '$storageScopeId|$limit|$normalizedQuery';
}

void _storeSearchPayloadCache(
  String cacheKey,
  Map<String, dynamic> payload,
) {
  _searchPayloadCache[cacheKey] = _CachedSearchPayload(
    payload: Map<String, dynamic>.from(payload),
    storedAt: DateTime.now(),
  );
  if (_searchPayloadCache.length > 36) {
    final oldestEntries = _searchPayloadCache.entries.toList()
      ..sort((a, b) => a.value.storedAt.compareTo(b.value.storedAt));
    for (final entry in oldestEntries.take(_searchPayloadCache.length - 24)) {
      _searchPayloadCache.remove(entry.key);
    }
  }
}

_CachedSearchPayload? _lookupSearchPayloadCache(
  String cacheKey, {
  bool allowStale = false,
}) {
  final cached = _searchPayloadCache[cacheKey];
  if (cached == null) return null;
  final age = DateTime.now().difference(cached.storedAt);
  final ttl = allowStale ? _searchCacheMaxTtl : _searchCacheFreshTtl;
  if (age > ttl) {
    if (allowStale) {
      _searchPayloadCache.remove(cacheKey);
    }
    return null;
  }
  return cached;
}

Future<_SearchFetchResult> _fetchSearchPayload(
  Ref ref,
  String query, {
  required int limit,
  required Duration timeout,
  bool preferCache = true,
  bool backgroundRefresh = false,
}) async {
  final normalizedQuery = query.trim();
  if (normalizedQuery.isEmpty) {
    return const _SearchFetchResult(status: 'empty_query');
  }
  final cacheKey = _searchPayloadCacheKey(
    ref,
    normalizedQuery,
    limit: limit,
  );
  final freshCached = _lookupSearchPayloadCache(cacheKey);
  if (preferCache && freshCached != null && !backgroundRefresh) {
    return _SearchFetchResult(
      status: 'cache_hit',
      payload: Map<String, dynamic>.from(freshCached.payload),
    );
  }
  if (!backgroundRefresh) {
    final staleCached = _lookupSearchPayloadCache(cacheKey, allowStale: true);
    if (staleCached != null) {
      unawaited(
        _fetchSearchPayload(
          ref,
          normalizedQuery,
          limit: limit,
          timeout: timeout,
          preferCache: false,
          backgroundRefresh: true,
        ),
      );
      return _SearchFetchResult(
        status: 'stale_cache',
        payload: Map<String, dynamic>.from(staleCached.payload),
      );
    }
  }
  final inflightMap = backgroundRefresh
      ? _searchPayloadBackgroundInflight
      : _searchPayloadForegroundInflight;
  final inflight = inflightMap[cacheKey];
  if (inflight != null) {
    return inflight;
  }

  final requestFuture = () async {
    try {
      final body = await _buildSemanticSearchRequestBody(
        ref,
        normalizedQuery,
        limit: limit,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/search'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(timeout);
      if (res.statusCode != 200) {
        if (!backgroundRefresh) {
          final staleCached =
              _lookupSearchPayloadCache(cacheKey, allowStale: true);
          if (staleCached != null) {
            return _SearchFetchResult(
              status: 'stale_cache',
              payload: Map<String, dynamic>.from(staleCached.payload),
              statusCode: res.statusCode,
            );
          }
        }
        return _SearchFetchResult(
          status: 'http_error',
          statusCode: res.statusCode,
        );
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _storeSearchPayloadCache(cacheKey, payload);
      return _SearchFetchResult(status: 'network_success', payload: payload);
    } on TimeoutException {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return _SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const _SearchFetchResult(status: 'timeout');
    } catch (_) {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return _SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const _SearchFetchResult(status: 'exception');
    } finally {
      inflightMap.remove(cacheKey);
    }
  }();
  inflightMap[cacheKey] = requestFuture;
  return requestFuture;
}

class SearchNotifier extends StateNotifier<List<dynamic>> {
  final Ref ref;
  int _requestVersion = 0;
  static const Duration _searchDebounce = Duration(milliseconds: 280);
  static const Duration _searchTimeout = Duration(seconds: 8);

  SearchNotifier(this.ref) : super([]);
  bool isLoading = false;

  void _primeSearchResults(List<dynamic> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      await Future<void>.delayed(_searchDebounce);
      if (requestVersion != _requestVersion) return;
      if (requestVersion != _requestVersion) return;
      final fetchResult = await _fetchSearchPayload(
        ref,
        normalizedQuery,
        limit: 16,
        timeout: _searchTimeout,
      );
      if (requestVersion != _requestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final rawResults = (payload['results'] as List<dynamic>? ?? const []);
        state = rawResults
            .whereType<Map>()
            .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
            .where((track) => (extractTrackId(track)?.isNotEmpty ?? false))
            .toList(growable: false);
        _debugProxyLog(
          'search',
          'track query="$normalizedQuery" status=200 results=${state.length} similar=${((payload['similar_artists'] as List?) ?? const []).length} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
        );
        _primeSearchResults(state);
        unawaited(
          recordCloudSearchEvent(
            normalizedQuery,
            resultCount: state.length,
          ),
        );
        unawaited(
          recordProxySearchEvent(
            normalizedQuery,
            resultCount: state.length,
            searchScope: 'track',
          ),
        );
        notifyRecommendationSignal(normalizedQuery);
      } else {
        _debugProxyLog(
          'search',
          'track query="$normalizedQuery" status=${fetchResult.status}',
        );
        state = [];
      }
    } on TimeoutException catch (error) {
      _debugProxyLog('search', 'track query="$normalizedQuery" timeout=$error');
      state = [];
    } catch (e) {
      _debugProxyLog('search', 'track query="$normalizedQuery" error=$e');
    } finally {
      final isLatestRequest = requestVersion == _requestVersion;
      if (isLatestRequest) {
        isLoading = false;
        state = [...state]; // trigger rebuild
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = [];
  }
}

final searchProvider =
    StateNotifierProvider<SearchNotifier, List<dynamic>>((ref) {
  return SearchNotifier(ref);
});

class SearchPageState {
  final String requestState;
  final String requestId;
  final String modelVersion;
  final String queryIntent;
  final Map<String, dynamic>? topResult;
  final List<Map<String, dynamic>> tracks;
  final List<Map<String, dynamic>> artists;
  final List<Map<String, dynamic>> albums;
  final List<Map<String, dynamic>> similarArtists;
  final Map<String, dynamic> diagnostics;
  final String? errorMessage;

  const SearchPageState({
    this.requestState = 'idle',
    this.requestId = '',
    this.modelVersion = '',
    this.queryIntent = 'mixed',
    this.topResult,
    this.tracks = const [],
    this.artists = const [],
    this.albums = const [],
    this.similarArtists = const [],
    this.diagnostics = const {},
    this.errorMessage,
  });

  bool get hasResults =>
      topResult != null ||
      tracks.isNotEmpty ||
      artists.isNotEmpty ||
      albums.isNotEmpty ||
      similarArtists.isNotEmpty;

  factory SearchPageState.fromJson(Map<String, dynamic> json) {
    final rawTopResult = json['top_result'];
    Map<String, dynamic>? normalizedTopResult;
    if (rawTopResult is Map) {
      final topResultMap = Map<String, dynamic>.from(rawTopResult);
      final entityType = topResultMap['entity_type']?.toString() ?? 'track';
      final rawItem = topResultMap['item'];
      if (rawItem is Map) {
        final item = Map<String, dynamic>.from(rawItem);
        topResultMap['item'] =
            entityType == 'track' ? normalizeTrack(item) : item;
        normalizedTopResult = topResultMap;
      }
    }
    final rawTracks = (json['tracks'] as List<dynamic>? ??
        json['results'] as List<dynamic>? ??
        const []);
    final rawArtists = (json['artists'] as List<dynamic>? ?? const []);
    final rawAlbums = (json['albums'] as List<dynamic>? ?? const []);
    final rawSimilarArtists =
        (json['similar_artists'] as List<dynamic>? ?? const []);
    final normalizedTracks = rawTracks
        .whereType<Map>()
        .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .toList(growable: false);
    if (normalizedTopResult == null && normalizedTracks.isNotEmpty) {
      normalizedTopResult = <String, dynamic>{
        'entity_type': 'track',
        'item': normalizedTracks.first,
      };
    }
    return SearchPageState(
      requestState: (json['request_state'] ?? '').toString().trim().isNotEmpty
          ? json['request_state'].toString()
          : ((json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? 'failed'
              : 'complete'),
      requestId: (json['request_id'] ?? '').toString(),
      modelVersion: (json['model_version'] ?? '').toString(),
      queryIntent: (json['query_intent'] ??
              (normalizedTracks.isNotEmpty ? 'track' : 'mixed'))
          .toString(),
      topResult: normalizedTopResult,
      tracks: normalizedTracks,
      artists: rawArtists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((artist) =>
              (artist['id']?.toString().trim().isNotEmpty ?? false) ||
              (artist['name']?.toString().trim().isNotEmpty ?? false))
          .toList(growable: false),
      albums: rawAlbums
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((album) =>
              (album['id']?.toString().trim().isNotEmpty ?? false) ||
              (album['title']?.toString().trim().isNotEmpty ?? false))
          .toList(growable: false),
      similarArtists: rawSimilarArtists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((artist) =>
              (artist['id']?.toString().trim().isNotEmpty ?? false) ||
              (artist['name']?.toString().trim().isNotEmpty ?? false))
          .toList(growable: false),
      diagnostics: json['diagnostics'] is Map
          ? Map<String, dynamic>.from(json['diagnostics'] as Map)
          : const {},
      errorMessage:
          (json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? json['error_message'].toString().trim()
              : null,
    );
  }

  SearchPageState copyWith({
    String? requestState,
    String? requestId,
    String? modelVersion,
    String? queryIntent,
    Map<String, dynamic>? topResult,
    bool clearTopResult = false,
    List<Map<String, dynamic>>? tracks,
    List<Map<String, dynamic>>? artists,
    List<Map<String, dynamic>>? albums,
    List<Map<String, dynamic>>? similarArtists,
    Map<String, dynamic>? diagnostics,
    String? errorMessage,
    bool clearError = false,
  }) {
    return SearchPageState(
      requestState: requestState ?? this.requestState,
      requestId: requestId ?? this.requestId,
      modelVersion: modelVersion ?? this.modelVersion,
      queryIntent: queryIntent ?? this.queryIntent,
      topResult: clearTopResult ? null : topResult ?? this.topResult,
      tracks: tracks ?? this.tracks,
      artists: artists ?? this.artists,
      albums: albums ?? this.albums,
      similarArtists: similarArtists ?? this.similarArtists,
      diagnostics: diagnostics ?? this.diagnostics,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
    );
  }
}

class SearchPageNotifier extends StateNotifier<SearchPageState> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;
  static const Duration _searchDebounce = Duration.zero;
  static const Duration _searchTimeout = Duration(seconds: 8);

  SearchPageNotifier(this.ref) : super(const SearchPageState());

  void _primeSearchResults(List<Map<String, dynamic>> tracks) {
    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            tracks.map((track) => track['id'] ?? track['videoId']),
          ),
    );
  }

  Future<void> search(String query) async {
    final normalizedQuery = query.trim();
    if (normalizedQuery.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = state.copyWith(requestState: 'loading', clearError: true);
    try {
      if (_searchDebounce > Duration.zero) {
        await Future<void>.delayed(_searchDebounce);
        if (requestVersion != _requestVersion) return;
      }
      if (requestVersion != _requestVersion) return;
      final fetchResult = await _fetchSearchPayload(
        ref,
        normalizedQuery,
        limit: 16,
        timeout: _searchTimeout,
      );
      if (requestVersion != _requestVersion) return;
      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final nextState = SearchPageState.fromJson(payload);
        state = nextState.copyWith(
          requestState: 'complete',
          clearError: true,
        );
        _debugProxyLog(
          'search',
          'page query="$normalizedQuery" status=200 tracks=${nextState.tracks.length} artists=${nextState.artists.length} albums=${nextState.albums.length} similar=${nextState.similarArtists.length} diagnostics=${_compactDiagnosticValue(nextState.diagnostics)}',
        );
        _primeSearchResults(nextState.tracks);
        unawaited(
          recordCloudSearchEvent(
            normalizedQuery,
            resultCount: nextState.tracks.length,
          ),
        );
        unawaited(
          recordProxySearchEvent(
            normalizedQuery,
            resultCount: nextState.tracks.length,
            searchScope: 'search_page',
          ),
        );
        notifyRecommendationSignal(normalizedQuery);
      } else {
        final errorMessage = fetchResult.status == 'timeout'
            ? _searchTimeoutMessage
            : _searchUnavailableMessage;
        _debugProxyLog(
          'search',
          'page query="$normalizedQuery" status=${fetchResult.status}',
        );
        state = SearchPageState(
          requestState: 'failed',
          errorMessage: errorMessage,
          diagnostics: <String, dynamic>{
            'fetch_status': fetchResult.status,
            if (fetchResult.statusCode != null)
              'http_status': fetchResult.statusCode,
          },
        );
      }
    } on TimeoutException catch (error) {
      _debugProxyLog('search', 'page query="$normalizedQuery" timeout=$error');
      if (requestVersion != _requestVersion) return;
      state = const SearchPageState(
        requestState: 'failed',
        errorMessage: _searchTimeoutMessage,
      );
    } catch (error) {
      _debugProxyLog('search', 'page query="$normalizedQuery" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const SearchPageState(
        requestState: 'failed',
        errorMessage: _searchUnavailableMessage,
      );
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = state.copyWith();
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const SearchPageState(requestState: 'idle');
  }
}

final searchPageProvider =
    StateNotifierProvider<SearchPageNotifier, SearchPageState>((ref) {
  return SearchPageNotifier(ref);
});

class RecommendationFeedRowState {
  final String id;
  final String title;
  final String kind;
  final String itemType;
  final List<Map<String, dynamic>> items;
  final int nextOffset;
  final bool hasMore;

  const RecommendationFeedRowState({
    required this.id,
    required this.title,
    required this.kind,
    required this.itemType,
    required this.items,
    required this.nextOffset,
    required this.hasMore,
  });

  factory RecommendationFeedRowState.fromJson(Map<String, dynamic> json) {
    final kind = (json['kind'] ?? json['id'] ?? 'tracks').toString();
    final inferredItemType = kind == 'recommended_albums'
        ? 'album'
        : kind == 'recommended_artists'
            ? 'artist'
            : 'track';
    final itemType = (json['item_type'] ?? inferredItemType).toString();
    final rawItems = (json['items'] as List<dynamic>? ?? const []);
    final items = rawItems.whereType<Map>().map((entry) {
      final map = Map<String, dynamic>.from(entry);
      if (itemType != 'track') {
        return map;
      }
      return normalizeTrack(map);
    }).where((item) {
      if (itemType == 'album') {
        final albumId = item['id']?.toString().trim() ?? '';
        final albumTitle = item['title']?.toString().trim() ?? '';
        return albumId.isNotEmpty || albumTitle.isNotEmpty;
      }
      if (itemType == 'artist') {
        final artistId = item['id']?.toString().trim() ?? '';
        final artistName = item['name']?.toString().trim() ?? '';
        return artistId.isNotEmpty || artistName.isNotEmpty;
      }
      return extractTrackId(item)?.isNotEmpty ?? false;
    }).toList(growable: false);
    return RecommendationFeedRowState(
      id: (json['id'] ?? kind).toString(),
      title: (json['title'] ?? 'Recommended').toString(),
      kind: kind,
      itemType: itemType,
      items: items,
      nextOffset: (json['next_offset'] as num?)?.toInt() ?? items.length,
      hasMore: json['has_more'] == true,
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'kind': kind,
        'item_type': itemType,
        'items': items,
        'next_offset': nextOffset,
        'has_more': hasMore,
      };

  RecommendationFeedRowState copyWith({
    String? id,
    String? title,
    String? kind,
    String? itemType,
    List<Map<String, dynamic>>? items,
    int? nextOffset,
    bool? hasMore,
  }) {
    return RecommendationFeedRowState(
      id: id ?? this.id,
      title: title ?? this.title,
      kind: kind ?? this.kind,
      itemType: itemType ?? this.itemType,
      items: items ?? this.items,
      nextOffset: nextOffset ?? this.nextOffset,
      hasMore: hasMore ?? this.hasMore,
    );
  }
}

class RecommendationFeedState {
  final String requestState;
  final String sessionId;
  final List<RecommendationFeedRowState> rows;
  final double? generatedAt;
  final double? expiresAt;
  final String? errorMessage;
  final Map<String, dynamic> diagnostics;

  const RecommendationFeedState({
    this.requestState = 'idle',
    this.sessionId = '',
    this.rows = const [],
    this.generatedAt,
    this.expiresAt,
    this.errorMessage,
    this.diagnostics = const <String, dynamic>{},
  });

  bool get isEmpty => rows.every((row) => row.items.isEmpty);
  bool get hasRows => rows.any((row) => row.items.isNotEmpty);

  List<Map<String, dynamic>> get visibleTracks => rows
      .where((row) => row.itemType == 'track')
      .expand((row) => row.items)
      .toList(growable: false);

  factory RecommendationFeedState.fromJson(Map<String, dynamic> json) {
    final rawRows = (json['rows'] as List<dynamic>? ?? const []);
    return RecommendationFeedState(
      requestState: (json['request_state'] ?? '').toString().trim().isNotEmpty
          ? json['request_state'].toString()
          : ((json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? 'failed'
              : 'complete'),
      sessionId: (json['session_id'] ?? '').toString(),
      rows: rawRows
          .whereType<Map>()
          .map((row) => RecommendationFeedRowState.fromJson(
              Map<String, dynamic>.from(row)))
          .toList(growable: false),
      generatedAt: (json['generated_at'] as num?)?.toDouble(),
      expiresAt: (json['expires_at'] as num?)?.toDouble(),
      errorMessage:
          (json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? json['error_message'].toString().trim()
              : null,
      diagnostics: json['diagnostics'] is Map
          ? Map<String, dynamic>.from(json['diagnostics'] as Map)
          : const <String, dynamic>{},
    );
  }

  Map<String, dynamic> toJson() => {
        'request_state': requestState,
        'session_id': sessionId,
        'generated_at': generatedAt,
        'expires_at': expiresAt,
        'rows': rows.map((row) => row.toJson()).toList(growable: false),
        if (errorMessage != null) 'error_message': errorMessage,
        if (diagnostics.isNotEmpty) 'diagnostics': diagnostics,
      };

  RecommendationFeedState copyWith({
    String? requestState,
    String? sessionId,
    List<RecommendationFeedRowState>? rows,
    double? generatedAt,
    double? expiresAt,
    String? errorMessage,
    Map<String, dynamic>? diagnostics,
    bool clearError = false,
  }) {
    return RecommendationFeedState(
      requestState: requestState ?? this.requestState,
      sessionId: sessionId ?? this.sessionId,
      rows: rows ?? this.rows,
      generatedAt: generatedAt ?? this.generatedAt,
      expiresAt: expiresAt ?? this.expiresAt,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
      diagnostics: diagnostics ?? this.diagnostics,
    );
  }
}

String _recommendationRowItemKey(
  String itemType,
  Map<String, dynamic> item,
) {
  if (itemType == 'album') {
    final albumId = item['id']?.toString().trim() ?? '';
    if (albumId.isNotEmpty) return 'album:$albumId';
    final title = item['title']?.toString().trim().toLowerCase() ?? '';
    final artist = item['artist']?.toString().trim().toLowerCase() ?? '';
    return 'album:$title|$artist';
  }
  if (itemType == 'artist') {
    final artistId = item['id']?.toString().trim() ?? '';
    if (artistId.isNotEmpty) return 'artist:$artistId';
    final name = item['name']?.toString().trim().toLowerCase() ?? '';
    return 'artist:$name';
  }
  final trackId = extractTrackId(item)?.trim() ?? '';
  if (trackId.isNotEmpty) return 'track:$trackId';
  final title = item['title']?.toString().trim().toLowerCase() ?? '';
  final artist = (item['channel'] ?? item['author'] ?? item['artist'])
          ?.toString()
          .trim()
          .toLowerCase() ??
      '';
  return 'track:$title|$artist';
}

Future<List<Map<String, dynamic>>> _hydrateTracksFromIds(
  Ref ref,
  Iterable<String> trackIds, {
  int limit = 8,
}) async {
  final orderedTrackIds = <String>[];
  final seenTrackIds = <String>{};
  for (final rawTrackId in trackIds) {
    final trackId = rawTrackId.trim();
    if (trackId.isEmpty || !seenTrackIds.add(trackId)) continue;
    orderedTrackIds.add(trackId);
    if (orderedTrackIds.length >= limit) break;
  }
  if (orderedTrackIds.isEmpty) return const [];

  final libraryTracks = await ref
      .read(libraryProvider.future)
      .catchError((_) => <Map<String, dynamic>>[]);
  final libraryById = <String, Map<String, dynamic>>{};
  for (final rawTrack in libraryTracks) {
    final track = normalizeTrack(rawTrack);
    final trackId = extractTrackId(track);
    if (trackId == null || trackId.isEmpty) continue;
    libraryById[trackId] = track;
  }

  final resolved = <Map<String, dynamic>>[];
  final resolvedIds = <String>{};
  final missingIds = <String>[];
  final localFallbacks = <String, Map<String, dynamic>>{};
  for (final trackId in orderedTrackIds) {
    final localTrack = libraryById[trackId];
    if (localTrack != null) {
      if (_isTrackMetadataIncomplete(localTrack)) {
        localFallbacks[trackId] = localTrack;
        missingIds.add(trackId);
      } else {
        resolved.add(localTrack);
        resolvedIds.add(trackId);
      }
      continue;
    }
    missingIds.add(trackId);
  }

  if (missingIds.isNotEmpty) {
    final futures = missingIds.take(limit).map((trackId) async {
      try {
        final response = await appHttpClient
            .post(
              buildProxyUri('/track_details'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'video_id': trackId}),
            )
            .timeout(const Duration(seconds: 10));
        if (response.statusCode != 200) return null;
        return _trackFromDetailsPayload(jsonDecode(response.body));
      } catch (_) {
        return null;
      }
    }).toList(growable: false);
    final fetched = await Future.wait(futures);
    for (final track in fetched.whereType<Map<String, dynamic>>()) {
      final trackId = extractTrackId(track);
      if (trackId == null || trackId.isEmpty || resolvedIds.contains(trackId)) {
        continue;
      }
      resolved.add(track);
      resolvedIds.add(trackId);
      if (resolved.length >= limit) break;
    }
  }

  for (final trackId in orderedTrackIds) {
    if (resolved.length >= limit) break;
    if (resolvedIds.contains(trackId)) continue;
    final fallback = localFallbacks[trackId];
    if (fallback == null) continue;
    resolved.add(fallback);
    resolvedIds.add(trackId);
  }

  return resolved.take(limit).toList(growable: false);
}

class RecommendationNotifier extends StateNotifier<RecommendationFeedState> {
  final Ref ref;
  RecommendationNotifier(this.ref) : super(const RecommendationFeedState());
  bool isLoading = true;
  final Set<String> _paginatingRows = <String>{};
  int _requestVersion = 0;
  final Set<String> _prewarmedRecommendationIds = <String>{};
  bool _startupHealthChecked = false;
  Timer? _heavyHydrationTimer;
  String _heavyHydrationKey = '';
  int _backgroundRefreshToken = 0;

  bool get isPaginating => _paginatingRows.isNotEmpty;
  bool get hasMorePages => state.rows.any((row) => row.hasMore);
  bool isRowPaginating(String rowId) => _paginatingRows.contains(rowId);

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<bool> _ensureProxyHealthyAtStartup() async {
    if (_startupHealthChecked) return true;
    _startupHealthChecked = true;
    final healthy = await probeProxyHealth();
    if (!healthy) {
      _debugProxyLog(
        'recommend',
        'startup health check failed for candidates=${_compactDiagnosticValue(proxyBaseUrlCandidates)}',
      );
    }
    return healthy;
  }

  Future<void> bootstrap() async {
    final authState = ref.read(authProvider);
    if (authState.isConfigured && !authState.isInitialized) {
      isLoading = false;
      if (mounted) {
        state = state.copyWith(requestState: 'idle');
      }
      _debugProxyLog(
        'recommend',
        'bootstrap deferred until auth initialization completes for scope=${authState.storageScopeId}',
      );
      return;
    }
    final proxyHealthy = await _ensureProxyHealthyAtStartup();
    if (!mounted) return;
    if (!proxyHealthy) {
      isLoading = false;
      state = state.copyWith(
        requestState: 'failed',
        errorMessage:
            'Recommendation engine is unreachable. Check proxy/server connection and refresh.',
      );
      return;
    }

    await refreshFromSignals(forceRefresh: false);
  }

  Future<void> refreshFromSignals({bool forceRefresh = false}) async {
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    if (forceRefresh) {
      await loadRecommendations(
        seed,
        false,
        const <String>[],
        const <String>[],
        const <String>[],
        true,
      );
      unawaited(_refreshRecommendationsInBackground(seed));
      return;
    }
    await loadRecommendations(seed, forceRefresh);
  }

  Future<void> _refreshRecommendationsInBackground(String? seedId) async {
    final refreshToken = ++_backgroundRefreshToken;
    final requestVersion = ++_requestVersion;
    try {
      final body = await _buildRecommendationRequestBody(
        seedId,
        limit: 8,
        forceRefresh: true,
      );
      if (!mounted ||
          requestVersion != _requestVersion ||
          refreshToken != _backgroundRefreshToken) {
        return;
      }
      _debugProxyLog(
        'recommend',
        'background refresh start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''}',
      );
      final res = await _runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        _recommendRequestTimeout,
      );
      if (!mounted ||
          requestVersion != _requestVersion ||
          refreshToken != _backgroundRefreshToken) {
        return;
      }
      if (res.statusCode != 200) {
        _debugProxyLog(
          'recommend',
          'background refresh status=${res.statusCode} body=${res.body}',
        );
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _logRecommendationDiagnostics('background', payload);
      final nextState = _feedStateFromPayload(payload);
      if (!nextState.hasRows) {
        _debugProxyLog('recommend', 'background refresh returned empty rows');
        return;
      }
      state = nextState.copyWith(
        requestState: 'complete',
        clearError: true,
      );
      _primeRecommendationRows(state.rows);
    } on TimeoutException catch (error) {
      _debugProxyLog('recommend', 'background refresh timeout=$error');
    } catch (error) {
      _debugProxyLog('recommend', 'background refresh error=$error');
    }
  }

  @override
  void dispose() {
    _requestVersion++;
    _heavyHydrationTimer?.cancel();
    super.dispose();
  }

  RecommendationFeedState _feedStateFromPayload(Map<String, dynamic> payload) {
    final rows = (payload['rows'] as List<dynamic>? ?? const []);
    if (rows.isNotEmpty) {
      final nextState = RecommendationFeedState.fromJson(payload);
      if (nextState.hasRows) {
        return nextState;
      }
    }
    final recommendations =
        (payload['recommendations'] as List<dynamic>? ?? const [])
            .whereType<Map>()
            .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
            .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
            .toList(growable: false);
    if (recommendations.isNotEmpty) {
      final rebuiltPayload = <String, dynamic>{
        ...payload,
        'rows': <Map<String, dynamic>>[
          {
            'id': 'recommended_tracks',
            'title': 'Recommended for you',
            'kind': 'recommended_tracks',
            'item_type': 'track',
            'items': recommendations,
            'next_offset': recommendations.length,
            'has_more': false,
          }
        ],
      };
      final nextState = RecommendationFeedState.fromJson(rebuiltPayload);
      if (nextState.hasRows) {
        return nextState;
      }
    }
    return const RecommendationFeedState(requestState: 'complete');
  }

  void _logRecommendationDiagnostics(
    String phase,
    Map<String, dynamic> payload,
  ) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return;
    final diagnostics = Map<String, dynamic>.from(diagnosticsRaw);
    final rowStatusSummary = <String, String>{};
    final rowStatusRaw = diagnostics['row_status'];
    if (rowStatusRaw is Map) {
      for (final entry in rowStatusRaw.entries) {
        final key = entry.key?.toString() ?? '';
        if (key.isEmpty) continue;
        final value = entry.value;
        if (value is Map) {
          rowStatusSummary[key] = value['status']?.toString() ?? '';
        }
      }
    }
    _debugProxyLog(
      'recommend',
      '$phase timing requestMs=${diagnostics['request_ms']} profileMs=${diagnostics['profile_build_ms']} rowMs=${diagnostics['row_assembly_ms']} stageMs=${_compactDiagnosticValue(diagnostics['stage_timings_ms'])} rowStatus=${_compactDiagnosticValue(rowStatusSummary)} requestId=${payload['request_id'] ?? diagnostics['request_id'] ?? ''}',
    );
  }

  void _primeRecommendationResults(Iterable<dynamic> tracks) {
    final ids = <String>[];
    for (final track in tracks) {
      final id = extractTrackId(track);
      if (id == null || id.isEmpty || !_prewarmedRecommendationIds.add(id)) {
        continue;
      }
      ids.add(id);
      if (ids.length >= 6) {
        break;
      }
    }
    if (ids.isEmpty) return;
    if (_prewarmedRecommendationIds.length > 160) {
      _prewarmedRecommendationIds.removeAll(
        _prewarmedRecommendationIds
            .take(_prewarmedRecommendationIds.length - 80),
      );
    }
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(ids));
  }

  void _primeRecommendationRows(List<RecommendationFeedRowState> rows) {
    final visibleTracks = <Map<String, dynamic>>[];
    for (final row in rows) {
      if (row.itemType != 'track') continue;
      visibleTracks.addAll(row.items.take(3));
      if (visibleTracks.length >= 12) {
        break;
      }
    }
    if (visibleTracks.isEmpty) return;
    _primeRecommendationResults(visibleTracks);
  }

  bool _diagnosticFlag(
    Map<String, dynamic> payload,
    String key,
  ) {
    final diagnosticsRaw = payload['diagnostics'];
    if (diagnosticsRaw is! Map) return false;
    final value = diagnosticsRaw[key];
    return value == true || value?.toString().toLowerCase() == 'true';
  }

  void _scheduleHeavyRowsHydration(
    String? seedId, {
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
  }) {
    final stateKey = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      state.sessionId,
      state.generatedAt?.toString() ?? '',
    ].join('|');
    if (stateKey.trim().isEmpty || _heavyHydrationKey == stateKey) {
      return;
    }
    _heavyHydrationKey = stateKey;
    _heavyHydrationTimer?.cancel();
    _heavyHydrationTimer = Timer(const Duration(milliseconds: 900), () {
      unawaited(
        _hydrateHeavyRows(
          seedId,
          extraArtistHints: extraArtistHints,
          extraTasteQueries: extraTasteQueries,
          expectedStateKey: stateKey,
        ),
      );
    });
  }

  Future<void> _hydrateHeavyRows(
    String? seedId, {
    required String expectedStateKey,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
  }) async {
    if (!mounted) return;
    final currentStateKey = [
      ref.read(authProvider).storageScopeId,
      seedId ?? '',
      state.sessionId,
      state.generatedAt?.toString() ?? '',
    ].join('|');
    if (currentStateKey != expectedStateKey || isLoading) {
      return;
    }
    try {
      final body = await _buildRecommendationRequestBody(
        seedId,
        limit: 8,
        forceRefresh: false,
        hydrateHeavyRows: true,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
      );
      if (!mounted) return;
      _debugProxyLog(
        'recommend',
        'heavy hydrate start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''}',
      );
      final res = await _runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        _recommendRequestTimeout,
      );
      if (!mounted || currentStateKey != expectedStateKey) return;
      if (res.statusCode != 200) return;
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final nextState = _feedStateFromPayload(payload);
      if (!nextState.hasRows || nextState.rows.length <= state.rows.length) {
        return;
      }
      state = nextState.copyWith(
        requestState: 'complete',
        clearError: true,
      );
      _primeRecommendationRows(state.rows);
      _debugProxyLog(
        'recommend',
        'heavy hydrate rows=${state.rows.length} session=${state.sessionId}',
      );
    } catch (_) {
      return;
    }
  }

  void _bumpTasteWeight(
    Map<String, double> weights,
    String? value,
    double weight,
  ) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    weights.update(normalized, (current) => current + weight,
        ifAbsent: () => weight);
  }

  String? _extractArtistHint(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString();
  }

  String? _extractAlbumHint(dynamic rawTrack) {
    if (rawTrack is! Map) return null;
    return rawTrack['album']?.toString() ?? rawTrack['album_title']?.toString();
  }

  bool _looksLikeTasteQuery(String query) {
    final normalized = query.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    if (_isMetadataHeavyQuery(normalized)) return false;
    if (normalized.length > 38) {
      return _sharedTasteKeywords
          .any((keyword) => normalized.contains(keyword));
    }
    if (normalized.contains(' mix') ||
        normalized.contains(' playlist') ||
        normalized.contains(' songs') ||
        normalized.contains(' music')) {
      return true;
    }
    return _sharedTasteKeywords.any((keyword) => normalized.contains(keyword));
  }

  Future<Map<String, dynamic>> _buildRecommendationRequestBody(
    String? seedId, {
    required int limit,
    int offset = 0,
    Set<String> avoidIds = const <String>{},
    bool forceRefresh = false,
    bool preferFreshRows = false,
    bool hydrateHeavyRows = false,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
  }) async {
    final seedIds = <String>[];
    final playlists = ref.read(playlistProvider);
    final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
    final storageScopeId = ref.read(authProvider).storageScopeId;
    final requestSignals = await Future.wait<Object?>([
      HistoryManager.getRecentTrackSnapshots(limit: forceRefresh ? 14 : 10),
      HistoryManager.getLastPlayedTrackSnapshots(limit: 8),
      HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 10),
      HistoryManager.getRecentSeeds(limit: forceRefresh ? 14 : 10),
      HistoryManager.getFrequentlyPlayedTrackIds(limit: 10),
      getRecentCloudSearchQueries(limit: 8),
    ]);
    final recentTrackSnapshots =
        List<Map<String, dynamic>>.from(requestSignals[0] as List);
    final lastPlayedSnapshots =
        List<Map<String, dynamic>>.from(requestSignals[1] as List);
    final topTrackSnapshots =
        List<Map<String, dynamic>>.from(requestSignals[2] as List);
    final recentTrackIds = List<String>.from(requestSignals[3] as List);
    final topTrackIds = List<String>.from(requestSignals[4] as List);
    final recentQueries = List<String>.from(requestSignals[5] as List);
    final blendedRecentQueries = <String>[];
    final softSessionQueries = <String>{};

    void addRecentQuery(String? value) {
      final normalized = value?.trim();
      if (normalized == null || normalized.isEmpty) return;
      if (!blendedRecentQueries.contains(normalized)) {
        blendedRecentQueries.add(normalized);
      }
    }

    for (final query in extraTasteQueries) {
      addRecentQuery(query);
    }
    for (final query in extraSessionQueries) {
      final normalized = query.trim();
      if (normalized.isEmpty) continue;
      softSessionQueries.add(normalized);
      addRecentQuery(normalized);
    }
    for (final query in recentQueries) {
      addRecentQuery(query);
    }

    void addSeed(String? id) {
      final normalized = id?.trim();
      if (normalized == null || normalized.isEmpty) return;
      if (!seedIds.contains(normalized)) {
        seedIds.add(normalized);
      }
    }

    addSeed(seedId);
    for (final snapshot in recentTrackSnapshots) {
      addSeed(extractTrackId(snapshot));
    }
    for (final recentSeed in recentTrackIds) {
      addSeed(recentSeed);
    }
    for (final frequentSeed in topTrackIds.take(6)) {
      addSeed(frequentSeed);
    }

    final artistWeights = <String, double>{};
    final queryWeights = <String, double>{};
    final albumWeights = <String, double>{};
    final libraryTrackIds = <String>[];
    final offlineTrackIds = <String>[];

    void addAlbumWeight(String? value, double weight) {
      final normalized = value?.trim().toLowerCase();
      if (normalized == null || normalized.isEmpty) return;
      albumWeights.update(
        normalized,
        (current) => current + weight,
        ifAbsent: () => weight,
      );
    }

    void addTrackHints(
      Iterable<Map<String, dynamic>> tracks, {
      required double artistWeight,
      required double albumWeight,
    }) {
      for (final track in tracks) {
        _bumpTasteWeight(
            artistWeights, _extractArtistHint(track), artistWeight);
        addAlbumWeight(_extractAlbumHint(track), albumWeight);
      }
    }

    addTrackHints(
      lastPlayedSnapshots,
      artistWeight: 1.35,
      albumWeight: 0.95,
    );
    addTrackHints(
      recentTrackSnapshots,
      artistWeight: 1.05,
      albumWeight: 0.8,
    );
    addTrackHints(
      topTrackSnapshots,
      artistWeight: 1.65,
      albumWeight: 1.15,
    );

    for (final artistHint in extraArtistHints) {
      _bumpTasteWeight(artistWeights, artistHint, 1.9);
    }

    for (final query in extraTasteQueries) {
      _bumpTasteWeight(queryWeights, query, 1.95);
    }
    for (final query in extraSessionQueries) {
      if (_looksLikeTasteQuery(query)) {
        _bumpTasteWeight(queryWeights, query, 0.65);
      }
    }

    for (final playlist in playlists) {
      if (_looksLikeTasteQuery(playlist.name)) {
        _bumpTasteWeight(queryWeights, playlist.name, 1.4);
      }
      for (final track in playlist.tracks.take(18)) {
        _bumpTasteWeight(artistWeights, _extractArtistHint(track), 1.4);
        addAlbumWeight(_extractAlbumHint(track), 1.1);
      }
    }

    for (final track in libraryTracks.take(24)) {
      final normalizedTrack = normalizeTrack(track);
      final trackId = extractTrackId(normalizedTrack);
      if (trackId != null && trackId.isNotEmpty) {
        libraryTrackIds.add(trackId);
        if (normalizedTrack['is_downloaded_locally'] == true) {
          offlineTrackIds.add(trackId);
        }
      }
      _bumpTasteWeight(artistWeights, _extractArtistHint(track), 1.2);
      addAlbumWeight(_extractAlbumHint(track), 0.9);
    }

    for (final query in blendedRecentQueries) {
      if (softSessionQueries.contains(query)) {
        continue;
      }
      if (_looksLikeTasteQuery(query)) {
        _bumpTasteWeight(queryWeights, query, 1.35);
      }
    }

    final rankedArtists = artistWeights.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final rankedQueries = queryWeights.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    final rankedAlbums = albumWeights.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));

    return {
      'query': '',
      'limit': limit,
      'offset': offset,
      'user_scope_id': storageScopeId,
      'force_refresh': forceRefresh,
      'prefer_fresh_rows': preferFreshRows,
      if (preferFreshRows)
        'refresh_token': DateTime.now().millisecondsSinceEpoch.toString(),
      'hydrate_heavy_rows': hydrateHeavyRows,
      if (seedIds.isNotEmpty) 'seed_id': seedIds.first,
      'seed_ids': seedIds.take(forceRefresh ? 6 : 5).toList(growable: false),
      'recent_tracks': recentTrackSnapshots,
      'top_tracks': topTrackSnapshots,
      'recent_track_ids': recentTrackIds,
      'top_track_ids': topTrackIds,
      'recent_track_snapshots': recentTrackSnapshots,
      'top_track_snapshots': topTrackSnapshots,
      'last_played_tracks': lastPlayedSnapshots,
      'recent_queries': blendedRecentQueries,
      'playlist_names': playlists
          .map((playlist) => playlist.name)
          .take(10)
          .toList(growable: false),
      'library_track_ids': libraryTrackIds.take(28).toList(growable: false),
      'offline_track_ids': offlineTrackIds.take(28).toList(growable: false),
      'artist_hints': rankedArtists
          .take(6)
          .map((entry) => entry.key)
          .toList(growable: false),
      'album_hints': rankedAlbums
          .take(6)
          .map((entry) => entry.key)
          .toList(growable: false),
      'taste_queries': rankedQueries
          .take(8)
          .map((entry) => entry.key)
          .toList(growable: false),
      'avoid_ids': avoidIds.take(40).toList(growable: false),
    };
  }

  Future<void> loadQuickRecommendations(String seedId) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
    isLoading = true;
    if (_isRequestCurrent(requestVersion)) {
      state = state.copyWith(requestState: 'loading', clearError: true);
    }
    try {
      final body = await _buildRecommendationRequestBody(seedId, limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      _debugProxyLog(
        'recommend',
        'quick request start scope=${body['user_scope_id']} seed=$seedId force=false',
      );
      final res = await _runRecommendationRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/recommend'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        _recommendRequestTimeout,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode != 200) {
        _debugProxyLog(
          'recommend',
          'quick request status=${res.statusCode} body=${res.body}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage: _proxyUnavailableMessage,
          );
        }
        return;
      }
      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      _logRecommendationDiagnostics('quick', payload);
      final nextState = _feedStateFromPayload(payload);
      _debugProxyLog(
        'recommend',
        'quick response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
      );
      if (!nextState.hasRows) {
        _debugProxyLog(
          'recommend',
          'quick response parsed empty rows rawRows=${((payload['rows'] as List?) ?? const []).length}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage:
                'Recommendation engine returned no rows. Pull to refresh and try again.',
          );
        }
        return;
      }
      if (_isRequestCurrent(requestVersion)) {
        state = nextState.copyWith(
          requestState: 'complete',
          clearError: true,
        );
        _primeRecommendationRows(nextState.rows);
      }
    } on TimeoutException catch (error) {
      _debugProxyLog('recommend', 'quick request timeout=$error');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: _recommendTimeoutMessage,
        );
      }
    } catch (error) {
      _debugProxyLog('recommend', 'quick request error=$error');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: _proxyUnavailableMessage,
        );
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = state.copyWith();
      }
    }
  }

  Future<void> loadRecommendations([
    String? seedId,
    bool forceRefresh = false,
    List<String> extraArtistHints = const <String>[],
    List<String> extraTasteQueries = const <String>[],
    List<String> extraSessionQueries = const <String>[],
    bool preferFreshRows = false,
  ]) async {
    final requestVersion = ++_requestVersion;
    final previousState = state;
    isLoading = true;
    final preserveVisibleRows = preferFreshRows && state.hasRows;
    if (_isRequestCurrent(requestVersion)) {
      state = preserveVisibleRows
          ? state.copyWith(clearError: true)
          : state.copyWith(requestState: 'loading', clearError: true);
    }
    try {
      final body = await _buildRecommendationRequestBody(
        seedId,
        limit: 8,
        forceRefresh: forceRefresh,
        preferFreshRows: preferFreshRows,
        extraArtistHints: extraArtistHints,
        extraTasteQueries: extraTasteQueries,
        extraSessionQueries: extraSessionQueries,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      _debugProxyLog(
        'recommend',
        'request start scope=${body['user_scope_id']} seed=${body['seed_id'] ?? ''} force=$forceRefresh preferFresh=$preferFreshRows artistHints=${_compactDiagnosticValue(body['artist_hints'])} tasteQueries=${_compactDiagnosticValue(body['taste_queries'])}',
      );
      final res = await _runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        _recommendRequestTimeout,
      );
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        _logRecommendationDiagnostics('main', payload);
        final nextState = _feedStateFromPayload(payload);
        _debugProxyLog(
          'recommend',
          'response rows=${nextState.rows.length} hasRows=${nextState.hasRows} firstRow=${nextState.rows.isEmpty ? '' : nextState.rows.first.id} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
        );
        if (nextState.hasRows) {
          state = nextState.copyWith(
            requestState: 'complete',
            clearError: true,
          );
          _primeRecommendationRows(state.rows);
          if (!forceRefresh &&
              !preferFreshRows &&
              _diagnosticFlag(payload, 'heavy_rows_pending')) {
            _scheduleHeavyRowsHydration(
              seedId,
              extraArtistHints: extraArtistHints,
              extraTasteQueries: extraTasteQueries,
            );
          }
          return;
        }
        _debugProxyLog(
          'recommend',
          'response parsed empty rows rawRows=${((payload['rows'] as List?) ?? const []).length}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage:
                'Recommendation engine returned no rows. Pull to refresh and try again.',
          );
        }
      } else {
        _debugProxyLog(
          'recommend',
          'request status=${res.statusCode} body=${res.body}',
        );
        if (_isRequestCurrent(requestVersion)) {
          state = previousState.copyWith(
            requestState: 'failed',
            errorMessage: _proxyUnavailableMessage,
          );
        }
      }
    } on TimeoutException catch (e) {
      _debugProxyLog('recommend', 'request timeout=$e');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: _recommendTimeoutMessage,
        );
      }
    } catch (e) {
      _debugProxyLog('recommend', 'request error=$e');
      if (_isRequestCurrent(requestVersion)) {
        state = previousState.copyWith(
          requestState: 'failed',
          errorMessage: _proxyUnavailableMessage,
        );
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = state.copyWith();
      }
    }
  }

  Future<void> loadMoreRow(String rowId) async {
    RecommendationFeedRowState? targetRow;
    for (final row in state.rows) {
      if (row.id == rowId) {
        targetRow = row;
        break;
      }
    }
    if (targetRow == null ||
        !targetRow.hasMore ||
        state.sessionId.isEmpty ||
        _paginatingRows.contains(rowId)) {
      return;
    }
    final currentRow = targetRow;
    _paginatingRows.add(rowId);
    if (mounted) {
      state = state.copyWith();
    }
    try {
      final pageLimit = targetRow.kind == 'quiet_picks' ? 10 : 8;
      final body = await _buildRecommendationRequestBody(
        null,
        limit: pageLimit,
        offset: targetRow.nextOffset,
      );
      if (!mounted) return;
      body['session_id'] = state.sessionId;
      body['row_id'] = rowId;
      final res = await _runRecommendationRequest(
        proxyControlHttpClient.post(buildProxyUri('/recommend'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body)),
        _recommendRowPageTimeout,
      );
      if (!mounted) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final rowPayload = payload['row'];
        if (rowPayload is Map<String, dynamic>) {
          final newRow = RecommendationFeedRowState.fromJson(rowPayload);
          final mergedItems = <Map<String, dynamic>>[];
          final seen = <String>{};
          for (final track in [...currentRow.items, ...newRow.items]) {
            final key = _recommendationRowItemKey(currentRow.itemType, track);
            if (key.trim().isEmpty || !seen.add(key)) continue;
            mergedItems.add(track);
          }
          final progressed = mergedItems.length > currentRow.items.length ||
              newRow.nextOffset > currentRow.nextOffset;
          final updatedRows = state.rows
              .map(
                (row) => row.id == rowId
                    ? row.copyWith(
                        itemType: newRow.itemType,
                        items: mergedItems,
                        nextOffset: progressed
                            ? newRow.nextOffset
                            : currentRow.nextOffset,
                        hasMore: progressed ? newRow.hasMore : false,
                      )
                    : row,
              )
              .toList(growable: false);
          state = state.copyWith(rows: updatedRows);
          _primeRecommendationResults(newRow.items);
        }
      }
    } on TimeoutException {
      if (mounted) {
        state = state.copyWith(
          requestState: 'failed',
          errorMessage: _recommendTimeoutMessage,
        );
      }
    } catch (e) {
      if (mounted) {
        state = state.copyWith(
          requestState: 'failed',
          errorMessage: _proxyUnavailableMessage,
        );
      }
    } finally {
      _paginatingRows.remove(rowId);
      if (mounted) {
        state = state.copyWith();
      }
    }
  }

  Future<void> loadMore([String? _]) async {
    final nextRow = state.rows.firstWhere(
      (row) => row.hasMore && !_paginatingRows.contains(row.id),
      orElse: () => const RecommendationFeedRowState(
        id: '',
        title: '',
        kind: '',
        itemType: 'track',
        items: [],
        nextOffset: 0,
        hasMore: false,
      ),
    );
    if (nextRow.id.isEmpty) {
      return;
    }
    await loadMoreRow(nextRow.id);
  }
}

final recommendationProvider =
    StateNotifierProvider<RecommendationNotifier, RecommendationFeedState>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  final notifier = RecommendationNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class FrequentlyPlayedNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  late final StreamSubscription<Map<String, dynamic>> _historySubscription;
  bool isLoading = false;
  int _requestVersion = 0;

  FrequentlyPlayedNotifier._internal(this.ref) : super(const []);

  factory FrequentlyPlayedNotifier(Ref ref) {
    final notifier = FrequentlyPlayedNotifier._internal(ref);
    notifier._historySubscription = HistoryManager.trackStream.listen((track) {
      notifier._applyImmediateTrack(track);
      unawaited(notifier.loadTracks());
    });
    return notifier;
  }

  void _applyImmediateTrack(Map<String, dynamic> rawTrack) {
    if (!mounted) return;
    final normalizedTrack = normalizeTrack(rawTrack);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId == null || trackId.isEmpty) return;
    final next = <Map<String, dynamic>>[normalizedTrack];
    for (final existing in state) {
      if (extractTrackId(existing) == trackId) continue;
      next.add(existing);
      if (next.length >= 8) break;
    }
    state = next;
  }

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<void> bootstrap() async {
    await loadTracks(allowPrewarm: false);
  }

  Future<void> loadTracks({
    bool forceRefresh = false,
    bool allowPrewarm = true,
  }) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      final frequentSnapshots =
          await HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      final resolved = <Map<String, dynamic>>[];
      final seen = <String>{};
      final snapshotFallbacks = <String, Map<String, dynamic>>{};
      for (final track in frequentSnapshots) {
        final trackId = extractTrackId(track);
        if (trackId == null || trackId.isEmpty || !seen.add(trackId)) continue;
        final normalizedTrack = normalizeTrack(track);
        if (_isTrackMetadataIncomplete(normalizedTrack)) {
          snapshotFallbacks[trackId] = normalizedTrack;
        } else {
          resolved.add(normalizedTrack);
        }
        if (seen.length >= 8) break;
      }

      final trackIds =
          await HistoryManager.getFrequentlyPlayedTrackIds(limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      if (trackIds.isEmpty && resolved.isEmpty) {
        if (!_isRequestCurrent(requestVersion)) return;
        state = const [];
        return;
      }
      for (final trackId in trackIds) {
        if (seen.length >= 8) break;
        if (!seen.contains(trackId)) {
          seen.add(trackId);
        }
      }
      final missingIds = {
        ...trackIds.where((trackId) {
          return !resolved.any((track) => extractTrackId(track) == trackId);
        }),
        ...snapshotFallbacks.keys.where((trackId) {
          return !resolved.any((track) => extractTrackId(track) == trackId);
        }),
      }.toList(growable: false);

      if (missingIds.isNotEmpty && resolved.length < 8) {
        final hydrated = await _hydrateTracksFromIds(
          ref,
          missingIds,
          limit: 8 - resolved.length,
        );
        if (!_isRequestCurrent(requestVersion)) return;
        for (final track in hydrated) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId.isEmpty) continue;
          if (resolved.any((entry) => extractTrackId(entry) == trackId)) {
            continue;
          }
          resolved.add(track);
        }
      }

      for (final entry in snapshotFallbacks.entries) {
        if (resolved.any((track) => extractTrackId(track) == entry.key)) {
          continue;
        }
        resolved.add(entry.value);
        if (resolved.length >= 8) break;
      }

      if (!_isRequestCurrent(requestVersion)) return;
      state = resolved.take(8).toList(growable: false);
      if (_isRequestCurrent(requestVersion) &&
          allowPrewarm &&
          state.isNotEmpty) {
        unawaited(
          ref.read(audioPlayerProvider.notifier).prewarmStreams(
                state.take(6).map(extractTrackId),
              ),
        );
      }
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  @override
  void dispose() {
    _historySubscription.cancel();
    _requestVersion++;
    super.dispose();
  }
}

final frequentlyPlayedProvider =
    StateNotifierProvider<FrequentlyPlayedNotifier, List<Map<String, dynamic>>>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = FrequentlyPlayedNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class LastPlayedNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  late final StreamSubscription<Map<String, dynamic>> _historySubscription;
  bool isLoading = false;
  int _requestVersion = 0;

  LastPlayedNotifier._internal(this.ref) : super(const []);

  factory LastPlayedNotifier(Ref ref) {
    final notifier = LastPlayedNotifier._internal(ref);
    notifier._historySubscription = HistoryManager.trackStream.listen((track) {
      notifier._applyImmediateTrack(track);
      unawaited(notifier.loadTracks());
    });
    return notifier;
  }

  void _applyImmediateTrack(Map<String, dynamic> rawTrack) {
    if (!mounted) return;
    final normalizedTrack = normalizeTrack(rawTrack);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId == null || trackId.isEmpty) return;
    final next = <Map<String, dynamic>>[normalizedTrack];
    for (final existing in state) {
      if (extractTrackId(existing) == trackId) continue;
      next.add(existing);
      if (next.length >= 8) break;
    }
    state = next;
  }

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<void> bootstrap() async {
    await loadTracks();
  }

  Future<void> loadTracks({bool forceRefresh = false}) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      final tracks = await HistoryManager.getLastPlayedTrackSnapshots(limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      final resolved = <Map<String, dynamic>>[];
      final pendingIds = <String>[];
      final fallbacks = <String, Map<String, dynamic>>{};
      for (final track in tracks) {
        final normalizedTrack = normalizeTrack(track);
        final trackId = extractTrackId(normalizedTrack);
        if (trackId == null || trackId.isEmpty) continue;
        if (_isTrackMetadataIncomplete(normalizedTrack)) {
          pendingIds.add(trackId);
          fallbacks[trackId] = normalizedTrack;
        } else {
          resolved.add(normalizedTrack);
        }
        if (resolved.length + pendingIds.length >= 8) break;
      }
      if (pendingIds.isNotEmpty) {
        final hydrated = await _hydrateTracksFromIds(
          ref,
          pendingIds,
          limit: pendingIds.length,
        );
        if (!_isRequestCurrent(requestVersion)) return;
        for (final track in hydrated) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId.isEmpty) continue;
          fallbacks.remove(trackId);
          resolved.add(track);
        }
      }
      if (!_isRequestCurrent(requestVersion)) return;
      for (final fallback in fallbacks.values) {
        resolved.add(fallback);
        if (resolved.length >= 8) break;
      }
      state = resolved.take(8).toList(growable: false);
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  @override
  void dispose() {
    _historySubscription.cancel();
    _requestVersion++;
    super.dispose();
  }
}

final lastPlayedProvider =
    StateNotifierProvider<LastPlayedNotifier, List<Map<String, dynamic>>>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = LastPlayedNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class SuggestNotifier extends StateNotifier<List<String>> {
  final Ref ref;
  int _requestVersion = 0;
  static const Duration _suggestDebounce = Duration(milliseconds: 160);

  SuggestNotifier(this.ref) : super([]);

  Future<void> fetchSuggestions(String query) async {
    if (query.isEmpty) {
      state = [];
      return;
    }
    final requestVersion = ++_requestVersion;
    try {
      await Future<void>.delayed(_suggestDebounce);
      if (requestVersion != _requestVersion) return;
      final body = _buildSuggestRequestBody(
        ref,
        query,
        limit: 5,
      );
      final res = await proxyControlHttpClient
          .post(buildProxyUri('/suggest'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 4));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        state = List<String>.from(jsonDecode(res.body)['results']);
      }
    } catch (e) {
      // print("Suggest failed: $e");
    }
  }

  void clear() {
    _requestVersion++;
    state = [];
  }
}

final suggestProvider =
    StateNotifierProvider<SuggestNotifier, List<String>>((ref) {
  return SuggestNotifier(ref);
});

class TrackDetailsNotifier extends StateNotifier<Map<String, dynamic>?> {
  final Ref ref;

  TrackDetailsNotifier(this.ref) : super(null);

  Future<void> fetchDetails(String videoId) async {
    state = null; // show loading
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(videoId));
    try {
      final res = await appHttpClient.post(buildProxyUri('/track_details'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({"video_id": videoId}));
      if (res.statusCode == 200) {
        state = jsonDecode(res.body);
        final similarTracks =
            (state?['similar_tracks'] as List<dynamic>?) ?? const [];
        unawaited(
          ref.read(audioPlayerProvider.notifier).prewarmStreams([
            videoId,
            ...similarTracks
                .take(8)
                .map((track) => track['id'] ?? track['videoId']),
          ]),
        );
      }
    } catch (e) {
      // print("Track Details failed: $e");
    }
  }
}

final trackDetailsProvider =
    StateNotifierProvider<TrackDetailsNotifier, Map<String, dynamic>?>((ref) {
  return TrackDetailsNotifier(ref);
});

class AlbumSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;

  AlbumSearchNotifier(this.ref) : super(const []);
  bool isLoading = false;

  Future<void> search(String query) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final body = await _buildSemanticSearchRequestBody(
        ref,
        query,
        limit: 12,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/search_albums'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final albums = (payload['albums'] as List<dynamic>? ?? const []);
        state = albums
            .map((album) => Map<String, dynamic>.from(album as Map))
            .toList(growable: false);
        _debugProxyLog(
          'search',
          'album query="$query" status=200 results=${state.length} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            query,
            resultCount: state.length,
            searchScope: 'album',
          ),
        );
      } else {
        _debugProxyLog(
          'search',
          'album query="$query" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      _debugProxyLog('search', 'album query="$query" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final albumSearchProvider =
    StateNotifierProvider<AlbumSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return AlbumSearchNotifier(ref);
});

class AlbumDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? album;
  final String? error;

  const AlbumDetailsState({
    this.isLoading = false,
    this.album,
    this.error,
  });
}

class AlbumDetailsNotifier extends StateNotifier<AlbumDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  AlbumDetailsNotifier(this.ref) : super(const AlbumDetailsState());

  Future<void> fetchAlbum(String? albumId) async {
    if (albumId == null || albumId.isEmpty) {
      state = const AlbumDetailsState(error: 'Album unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const AlbumDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/album/$albumId'))
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const AlbumDetailsState(error: 'Album unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final tracks = (payload['tracks'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              tracks.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
      state = AlbumDetailsState(album: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const AlbumDetailsState(error: 'Album unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const AlbumDetailsState();
  }
}

final albumDetailsProvider =
    StateNotifierProvider<AlbumDetailsNotifier, AlbumDetailsState>((ref) {
  return AlbumDetailsNotifier(ref);
});

class ArtistSearchNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;

  ArtistSearchNotifier(this.ref) : super(const []);

  Future<void> search(String query) async {
    final trimmed = query.trim();
    if (trimmed.isEmpty) {
      clear();
      return;
    }
    final requestVersion = ++_requestVersion;
    isLoading = true;
    state = [...state];
    try {
      final body = await _buildSemanticSearchRequestBody(
        ref,
        trimmed,
        limit: 12,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/search_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 10));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
        _debugProxyLog(
          'search',
          'artist query="$trimmed" status=200 results=${state.length} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
        );
        unawaited(
          recordProxySearchEvent(
            trimmed,
            resultCount: state.length,
            searchScope: 'artist',
          ),
        );
      } else {
        _debugProxyLog(
          'search',
          'artist query="$trimmed" status=${res.statusCode} body=${res.body}',
        );
        state = const [];
      }
    } catch (error) {
      _debugProxyLog('search', 'artist query="$trimmed" error=$error');
      if (requestVersion != _requestVersion) return;
      state = const [];
    } finally {
      if (requestVersion == _requestVersion) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final artistSearchProvider =
    StateNotifierProvider<ArtistSearchNotifier, List<Map<String, dynamic>>>(
        (ref) {
  return ArtistSearchNotifier(ref);
});

class RecommendedArtistsNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  int _requestVersion = 0;
  bool isLoading = false;

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  RecommendedArtistsNotifier(this.ref) : super(const []);

  void _bumpWeight(Map<String, double> weights, String? value, double weight) {
    final normalized = value?.trim().toLowerCase();
    if (normalized == null || normalized.isEmpty) return;
    weights.update(
      normalized,
      (current) => current + weight,
      ifAbsent: () => weight,
    );
  }

  void _bumpTrackArtists(
    Map<String, double> weights,
    dynamic rawTrack,
    double weight,
  ) {
    for (final artist in extractTrackArtists(rawTrack)) {
      _bumpWeight(weights, artist, weight);
    }
  }

  bool _looksLikeArtistOrSongQuery(String query) {
    final normalized = query.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    if (_isMetadataHeavyQuery(normalized)) return false;
    if (normalized.contains(' playlist') ||
        normalized.contains(' mix') ||
        normalized.contains(' mood') ||
        normalized.contains(' songs like')) {
      return false;
    }
    return true;
  }

  List<Map<String, dynamic>> _normalizeAnchorTracks(
    Iterable<dynamic> rawTracks, {
    int limit = 6,
  }) {
    final tracks = <Map<String, dynamic>>[];
    final seen = <String>{};
    for (final rawTrack in rawTracks) {
      if (rawTrack is! Map) continue;
      final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
      final key = _recommendationRowItemKey('track', track);
      if (key.trim().isEmpty || !seen.add(key)) continue;
      tracks.add(track);
      if (tracks.length >= limit) {
        break;
      }
    }
    return tracks;
  }

  Future<void> bootstrap() async {
    final authState = ref.read(authProvider);
    if (authState.isConfigured && !authState.isInitialized) {
      isLoading = false;
      if (mounted) {
        state = [...state];
      }
      _debugProxyLog(
        'artists',
        'bootstrap deferred until auth initialization completes for scope=${authState.storageScopeId}',
      );
      return;
    }
    final recState = ref.read(recommendationProvider);
    if (!recState.hasRows) {
      isLoading = false;
      if (mounted) {
        state = [...state];
      }
      _debugProxyLog(
        'artists',
        'bootstrap deferred until recommendation rows are available for scope=${authState.storageScopeId}',
      );
      return;
    }
    await loadRecommendedArtists(
      seedArtistHints: recState.visibleTracks
          .expand(extractTrackArtists)
          .take(6)
          .toSet()
          .toList(growable: false),
      surface: 'home_feed',
    );
  }

  Future<void> loadRecommendedArtists({
    List<String> seedArtistHints = const [],
    List<String> seedTasteQueries = const [],
    String surface = 'home_feed',
    List<Map<String, dynamic>> anchorTracks = const [],
    bool forceRefresh = false,
  }) async {
    final requestVersion = ++_requestVersion;
    final previousState = List<Map<String, dynamic>>.from(state);
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      final artistWeights = <String, double>{};
      final recState = ref.read(recommendationProvider);
      final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
      final artistSignals = await Future.wait<Object?>([
        HistoryManager.getLastPlayedTrackSnapshots(limit: 10),
        HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 12),
        HistoryManager.getRecentTrackSnapshots(limit: 12),
        getRecentCloudSearchQueries(limit: 8),
      ]);
      if (!_isRequestCurrent(requestVersion)) return;
      final lastPlayedSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[0] as List);
      final frequentSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[1] as List);
      final recentSnapshots =
          List<Map<String, dynamic>>.from(artistSignals[2] as List);
      final recentQueries = List<String>.from(artistSignals[3] as List);
      final normalizedAnchorTracks = _normalizeAnchorTracks(anchorTracks);
      final effectiveSeedArtistHints = seedArtistHints.isNotEmpty
          ? seedArtistHints
          : surface == 'home_feed'
              ? recState.visibleTracks
                  .expand(extractTrackArtists)
                  .take(6)
                  .toSet()
                  .toList(growable: false)
              : const <String>[];

      for (final artist in effectiveSeedArtistHints) {
        _bumpWeight(artistWeights, artist, 2.8);
      }
      for (final track in lastPlayedSnapshots) {
        _bumpTrackArtists(artistWeights, track, 2.2);
      }
      for (final track in frequentSnapshots) {
        _bumpTrackArtists(artistWeights, track, 1.9);
      }
      for (final track in recentSnapshots) {
        _bumpTrackArtists(artistWeights, track, 1.4);
      }

      for (final track in libraryTracks.take(24)) {
        _bumpTrackArtists(artistWeights, track, 1.1);
      }

      final canonicalQueries = surface == 'home_feed'
          ? <String>[
              ...seedTasteQueries.where(_looksLikeArtistOrSongQuery),
            ]
          : <String>[
              ...seedTasteQueries.where(_looksLikeArtistOrSongQuery),
              ...recentQueries.where(_looksLikeArtistOrSongQuery),
            ];

      final rankedArtists = artistWeights.entries.toList()
        ..sort((a, b) => b.value.compareTo(a.value));
      final requestArtistHints = rankedArtists
          .take(8)
          .map((entry) => entry.key)
          .toList(growable: false);
      if (surface == 'home_feed' &&
          requestArtistHints.isEmpty &&
          normalizedAnchorTracks.isEmpty) {
        _debugProxyLog(
          'artists',
          'recommended deferred for home_feed because stable feed seeds are not available yet',
        );
        if (!_isRequestCurrent(requestVersion)) return;
        state = previousState;
        return;
      }
      _debugProxyLog(
        'artists',
        'recommended start surface=$surface force=$forceRefresh hints=${_compactDiagnosticValue(requestArtistHints)} anchors=${normalizedAnchorTracks.length}',
      );

      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/recommended_artists'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'query': canonicalQueries.isEmpty ? '' : canonicalQueries.first,
              'limit': 8,
              'surface': surface,
              'force_refresh': forceRefresh,
              'artist_hints': requestArtistHints,
              'anchor_artist_hints':
                  effectiveSeedArtistHints.take(6).toList(growable: false),
              'anchor_track_snapshots': normalizedAnchorTracks,
              'recent_queries':
                  canonicalQueries.take(6).toList(growable: false),
              'recent_track_snapshots': recentSnapshots,
              'top_track_snapshots': frequentSnapshots,
              'last_played_tracks': lastPlayedSnapshots,
            }),
          )
          .timeout(const Duration(seconds: 10));
      if (!_isRequestCurrent(requestVersion)) return;
      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final artists = (payload['artists'] as List<dynamic>? ?? const []);
        state = artists
            .map((artist) => Map<String, dynamic>.from(artist as Map))
            .toList(growable: false);
        _debugProxyLog(
          'artists',
          'recommended surface=$surface status=200 results=${state.length} diagnostics=${_compactDiagnosticValue(payload['diagnostics'])}',
        );
      } else {
        _debugProxyLog(
          'artists',
          'recommended surface=$surface status=${res.statusCode} body=${res.body}',
        );
        state = previousState;
      }
    } catch (error) {
      _debugProxyLog('artists', 'recommended surface=$surface error=$error');
      if (!_isRequestCurrent(requestVersion)) return;
      state = previousState;
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  void clear() {
    _requestVersion++;
    isLoading = false;
    state = const [];
  }
}

final recommendedArtistsProvider = StateNotifierProvider<
    RecommendedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  return RecommendedArtistsNotifier(ref);
});

final searchSimilarArtistsProvider = StateNotifierProvider<
    RecommendedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  return RecommendedArtistsNotifier(ref);
});

class ArtistDetailsState {
  final bool isLoading;
  final Map<String, dynamic>? artist;
  final String? error;

  const ArtistDetailsState({
    this.isLoading = false,
    this.artist,
    this.error,
  });
}

class ArtistDetailsNotifier extends StateNotifier<ArtistDetailsState> {
  final Ref ref;
  int _requestVersion = 0;

  ArtistDetailsNotifier(this.ref) : super(const ArtistDetailsState());

  Future<void> fetchArtist(String? artistId) async {
    if (artistId == null || artistId.isEmpty) {
      state = const ArtistDetailsState(error: 'Artist unavailable.');
      return;
    }

    final requestVersion = ++_requestVersion;
    state = const ArtistDetailsState(isLoading: true);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/artist/$artistId'))
          .timeout(const Duration(seconds: 14));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode != 200) {
        state = const ArtistDetailsState(error: 'Artist unavailable.');
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final topSongs = (payload['top_songs'] as List<dynamic>? ?? const []);
      unawaited(
        ref.read(audioPlayerProvider.notifier).prewarmStreams(
              topSongs.take(8).map((track) => track['id'] ?? track['videoId']),
            ),
      );
      state = ArtistDetailsState(artist: payload);
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = const ArtistDetailsState(error: 'Artist unavailable.');
    }
  }

  void clear() {
    _requestVersion++;
    state = const ArtistDetailsState();
  }
}

final artistDetailsProvider =
    StateNotifierProvider<ArtistDetailsNotifier, ArtistDetailsState>((ref) {
  return ArtistDetailsNotifier(ref);
});

class LyricsNotifier extends StateNotifier<TrackLyricsState> {
  int _requestVersion = 0;

  LyricsNotifier() : super(const TrackLyricsState());

  Future<void> fetchLyrics(String? videoId) async {
    if (videoId == null || videoId.isEmpty) {
      clear();
      return;
    }
    if (state.videoId == videoId) {
      return;
    }

    final requestVersion = ++_requestVersion;
    state = TrackLyricsState(isLoading: true, videoId: videoId);
    try {
      final res = await appHttpClient
          .get(buildProxyUri('/lyrics/$videoId'))
          .timeout(const Duration(seconds: 12));
      if (requestVersion != _requestVersion) return;

      if (res.statusCode != 200) {
        state = TrackLyricsState(
          videoId: videoId,
          error: 'Lyrics unavailable right now.',
        );
        return;
      }

      final payload = jsonDecode(res.body) as Map<String, dynamic>;
      final rawLines = (payload['lines'] as List<dynamic>? ?? const []);
      final lines = rawLines
          .map((raw) =>
              TrackLyricsLine.fromJson(Map<String, dynamic>.from(raw as Map)))
          .where((line) => line.text.trim().isNotEmpty)
          .toList(growable: false);

      state = TrackLyricsState(
        videoId: videoId,
        hasLyrics: payload['has_lyrics'] == true && lines.isNotEmpty,
        hasTimestamps: payload['has_timestamps'] == true,
        source: payload['source']?.toString(),
        lines: lines,
      );
    } catch (_) {
      if (requestVersion != _requestVersion) return;
      state = TrackLyricsState(
        videoId: videoId,
        error: 'Lyrics unavailable right now.',
      );
    }
  }

  void clear() {
    _requestVersion++;
    state = const TrackLyricsState();
  }
}

final lyricsProvider =
    StateNotifierProvider<LyricsNotifier, TrackLyricsState>((ref) {
  return LyricsNotifier();
});

enum PlaybackQueueMode { none, radio, playlist }

class PlaybackQueueState {
  final PlaybackQueueMode mode;
  final List<Map<String, dynamic>> queue;
  final int currentIndex;
  final String? currentTrackId;
  final List<String> playedTrackIds;
  final String? playlistId;
  final String? playlistName;
  final List<Map<String, dynamic>> recommendations;
  final bool isLoadingQueue;
  final bool isLoadingRecommendations;

  const PlaybackQueueState({
    this.mode = PlaybackQueueMode.none,
    this.queue = const [],
    this.currentIndex = 0,
    this.currentTrackId,
    this.playedTrackIds = const [],
    this.playlistId,
    this.playlistName,
    this.recommendations = const [],
    this.isLoadingQueue = false,
    this.isLoadingRecommendations = false,
  });

  PlaybackQueueState copyWith({
    PlaybackQueueMode? mode,
    List<Map<String, dynamic>>? queue,
    int? currentIndex,
    String? currentTrackId,
    List<String>? playedTrackIds,
    String? playlistId,
    String? playlistName,
    List<Map<String, dynamic>>? recommendations,
    bool? isLoadingQueue,
    bool? isLoadingRecommendations,
    bool clearPlaylist = false,
    bool clearRecommendations = false,
  }) {
    return PlaybackQueueState(
      mode: mode ?? this.mode,
      queue: queue ?? this.queue,
      currentIndex: currentIndex ?? this.currentIndex,
      currentTrackId: currentTrackId ?? this.currentTrackId,
      playedTrackIds: playedTrackIds ?? this.playedTrackIds,
      playlistId: clearPlaylist ? null : playlistId ?? this.playlistId,
      playlistName: clearPlaylist ? null : playlistName ?? this.playlistName,
      recommendations: clearRecommendations
          ? const []
          : recommendations ?? this.recommendations,
      isLoadingQueue: isLoadingQueue ?? this.isLoadingQueue,
      isLoadingRecommendations:
          isLoadingRecommendations ?? this.isLoadingRecommendations,
    );
  }
}

class PlaybackQueueNotifier extends StateNotifier<PlaybackQueueState> {
  final Ref ref;
  final Random _random = Random();
  static const int _playedTrackMinSeconds = 12;
  static const double _playedTrackMinRatio = 0.35;

  PlaybackQueueNotifier(this.ref) : super(const PlaybackQueueState());

  void _primeTracks(
    Iterable<dynamic> tracks, {
    int limit = 8,
  }) {
    final ids = <String>[];
    for (final track in tracks) {
      if (isTrackHidden(track)) continue;
      final id = extractTrackId(track);
      if (id == null || id.isEmpty) continue;
      ids.add(id);
      if (ids.length >= limit) break;
    }
    if (ids.isEmpty) return;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(ids));
  }

  void _primeUpcomingQueue({int lookahead = 10}) {
    final upcoming = <Map<String, dynamic>>[];
    final resolvedCurrentIndex = _resolvedCurrentIndex();
    for (var i = resolvedCurrentIndex + 1; i < state.queue.length; i++) {
      final track = state.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      upcoming.add(track);
      if (upcoming.length >= lookahead) {
        break;
      }
    }
    if (upcoming.isNotEmpty) {
      _primeTracks(upcoming, limit: lookahead);
    }
    if (state.recommendations.isNotEmpty) {
      _primeTracks(state.recommendations, limit: 6);
    }
    if (state.mode == PlaybackQueueMode.playlist) {
      unawaited(
        ref.read(audioPlayerProvider.notifier).prefetchFixedQueueTracks(
              state.queue,
              currentIndex: resolvedCurrentIndex,
              count: 2,
            ),
      );
    }
  }

  int _resolvedCurrentIndex() {
    if (state.queue.isEmpty) return 0;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final managedIndex = audioNotifier.currentManagedQueueIndex;
    if (audioNotifier.hasManagedQueue &&
        managedIndex != null &&
        managedIndex >= 0 &&
        managedIndex < state.queue.length) {
      return managedIndex;
    }

    final activeTrackId = state.currentTrackId;
    if (activeTrackId != null && activeTrackId.isNotEmpty) {
      final matchingIndex = state.queue
          .indexWhere((track) => extractTrackId(track) == activeTrackId);
      if (matchingIndex >= 0) {
        return matchingIndex;
      }
    }

    return state.currentIndex.clamp(0, state.queue.length - 1).toInt();
  }

  int _nextPlayableIndex(int fromIndex) {
    for (var i = fromIndex + 1; i < state.queue.length; i++) {
      final track = state.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      return i;
    }
    return -1;
  }

  List<String> _withPlayedTrack(String? trackId) {
    if (trackId == null || trackId.isEmpty) return state.playedTrackIds;
    if (state.playedTrackIds.contains(trackId)) return state.playedTrackIds;
    return [...state.playedTrackIds, trackId];
  }

  bool _shouldCountTrackAsPlayed(String? trackId) {
    if (trackId == null || trackId.isEmpty) return false;
    final playerState = ref.read(audioPlayerProvider);
    final activeTrackId = playerState.videoId?.trim();
    if (activeTrackId == null ||
        activeTrackId.isEmpty ||
        activeTrackId != trackId) {
      return false;
    }
    final durationSeconds = playerState.duration;
    final positionSeconds = playerState.currentPosition;
    if (durationSeconds <= 0) {
      return positionSeconds >= _playedTrackMinSeconds;
    }
    final requiredSeconds = max(
      8,
      min(
        20,
        (durationSeconds * _playedTrackMinRatio).round(),
      ),
    );
    return positionSeconds >= requiredSeconds ||
        positionSeconds >= durationSeconds - 1;
  }

  List<String> _playedTrackIdsAfterCurrentProgress(String? trackId) {
    if (!_shouldCountTrackAsPlayed(trackId)) {
      return state.playedTrackIds;
    }
    return _withPlayedTrack(trackId);
  }

  void _markCurrentTrackPlayedIfEligible() {
    final nextPlayedTrackIds = _playedTrackIdsAfterCurrentProgress(
      state.currentTrackId,
    );
    if (!listEquals(nextPlayedTrackIds, state.playedTrackIds)) {
      state = state.copyWith(playedTrackIds: nextPlayedTrackIds);
    }
  }

  List<Map<String, dynamic>> _uniqueTracks(
    Iterable<dynamic> tracks, {
    Set<String>? excludedIds,
  }) {
    final seen = <String>{...?excludedIds};
    final normalized = <Map<String, dynamic>>[];
    for (final rawTrack in tracks) {
      final track = normalizeTrack(rawTrack);
      final id = extractTrackId(track);
      if (id == null || !seen.add(id)) continue;
      normalized.add(track);
    }
    return normalized;
  }

  void _refreshHistorySignalConsumers() {
    unawaited(
        ref.read(lastPlayedProvider.notifier).loadTracks(forceRefresh: true));
    unawaited(
      ref
          .read(frequentlyPlayedProvider.notifier)
          .loadTracks(forceRefresh: true),
    );
  }

  Future<void> _recordCurrentQueueInteraction(String eventType) async {
    final trackId = state.currentTrackId;
    if (trackId == null || trackId.isEmpty) return;
    final currentIndex = _resolvedCurrentIndex();
    final rawTrack = currentIndex >= 0 && currentIndex < state.queue.length
        ? state.queue[currentIndex]
        : null;
    await recordProxyInteractionEvent(
      eventType,
      trackId: trackId,
      rawTrack: rawTrack,
    );
  }

  Future<List<Map<String, dynamic>>> _fetchRecommendations(
    String seedId, {
    int limit = 12,
  }) async {
    final body =
        await RecommendationNotifier(ref)._buildRecommendationRequestBody(
      seedId,
      limit: limit,
      avoidIds: state.queue
          .map((track) => extractTrackId(track))
          .whereType<String>()
          .toSet(),
    );
    final res = await _runRecommendationRequest(
      proxyControlHttpClient.post(
        buildProxyUri('/recommend'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ),
      _recommendRequestTimeout,
    );

    if (res.statusCode != 200) {
      throw Exception('Recommendation lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks =
        payload['recommendations'] as List<dynamic>? ?? const <dynamic>[];
    return _uniqueTracks(rawTracks, excludedIds: {seedId});
  }

  Future<List<Map<String, dynamic>>> _fetchSimilarTracks(
    String seedId, {
    int limit = 12,
  }) async {
    final res = await appHttpClient
        .post(
          buildProxyUri('/track_details'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'video_id': seedId}),
        )
        .timeout(const Duration(seconds: 12));

    if (res.statusCode != 200) {
      throw Exception('Track detail lookup failed: ${res.statusCode}');
    }

    final payload = jsonDecode(res.body) as Map<String, dynamic>;
    final rawTracks =
        payload['similar_tracks'] as List<dynamic>? ?? const <dynamic>[];
    return _uniqueTracks(rawTracks, excludedIds: {seedId})
        .take(limit)
        .toList(growable: false);
  }

  Future<void> startRadioSession(dynamic track) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null) return;

    state = PlaybackQueueState(
      mode: PlaybackQueueMode.radio,
      queue: [normalizedTrack],
      currentIndex: 0,
      currentTrackId: videoId,
      playedTrackIds: const [],
    );

    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
      [normalizedTrack],
      initialIndex: 0,
    );
    _refreshHistorySignalConsumers();
    _primeUpcomingQueue();
    unawaited(_appendRadioRecommendations(seedId: videoId));
  }

  Future<void> startDiscoverySession(
    dynamic track, {
    String? sessionName,
  }) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null || videoId.isEmpty) return;

    final frequentSnapshots =
        await HistoryManager.getFrequentlyPlayedTrackSnapshots(
      limit: 2,
    );
    List<Map<String, dynamic>> similarTracks = const [];
    List<Map<String, dynamic>> fillerTracks = const [];
    try {
      similarTracks = await _fetchSimilarTracks(videoId, limit: 10);
    } catch (_) {
      similarTracks = const [];
    }
    if (similarTracks.length < 6) {
      try {
        fillerTracks = await _fetchRecommendations(
          videoId,
          limit: 8 - similarTracks.length,
        );
      } catch (_) {
        fillerTracks = const [];
      }
    }

    final queue = _uniqueTracks(
      [
        normalizedTrack,
        ...similarTracks,
        if (similarTracks.isNotEmpty) ...frequentSnapshots,
        ...fillerTracks,
      ],
      excludedIds: const {},
    );

    await startPlaylistSession(
      playlistId: 'discovery:$videoId',
      playlistName: sessionName ??
          'Inspired by ${normalizedTrack['title']?.toString() ?? 'this track'}',
      tracks: queue,
      currentTrack: normalizedTrack,
    );
  }

  Future<bool> startLocalSession({
    required dynamic track,
    required String path,
  }) async {
    final normalizedTrack = normalizeTrack(track);
    final localTrack = {
      ...normalizedTrack,
      'local_path': path,
    };
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final loaded =
        localTrack['thumbnail'] != null || extractTrackId(localTrack) != null
            ? await audioNotifier.loadLocalWithMeta(path, localTrack)
            : await audioNotifier.loadLocalFile(
                path,
                localTrack['title']?.toString() ?? 'Unknown Track',
              );
    if (!loaded) {
      clearSession();
      return false;
    }

    final videoId = extractTrackId(localTrack);
    if (videoId != null) {
      state = PlaybackQueueState(
        mode: PlaybackQueueMode.radio,
        queue: [localTrack],
        currentIndex: 0,
        currentTrackId: videoId,
        playedTrackIds: const [],
      );
      await audioNotifier.configureManagedQueue(
        [localTrack],
        initialIndex: 0,
      );
      _refreshHistorySignalConsumers();
      _primeTracks([localTrack], limit: 1);
      unawaited(_appendRadioRecommendations(seedId: videoId));
    } else {
      clearSession();
    }

    return true;
  }

  Future<void> startPlaylistSession({
    required String playlistId,
    required String playlistName,
    required Iterable<dynamic> tracks,
    required dynamic currentTrack,
    bool shuffle = false,
  }) async {
    final normalizedQueue = _uniqueTracks(tracks);
    final currentTrackMap = normalizeTrack(currentTrack);
    final currentTrackId = extractTrackId(currentTrackMap);
    if (currentTrackId == null) return;

    if (shuffle && normalizedQueue.length > 1) {
      normalizedQueue.shuffle(_random);
    }

    var currentIndex = normalizedQueue
        .indexWhere((track) => extractTrackId(track) == currentTrackId);
    if (currentIndex < 0) {
      normalizedQueue.insert(0, currentTrackMap);
      currentIndex = 0;
    }

    state = PlaybackQueueState(
      mode: PlaybackQueueMode.playlist,
      queue: normalizedQueue,
      currentIndex: currentIndex,
      currentTrackId: currentTrackId,
      playedTrackIds: const [],
      playlistId: playlistId,
      playlistName: playlistName,
    );

    await ref.read(audioPlayerProvider.notifier).configureManagedQueue(
          normalizedQueue,
          initialIndex: currentIndex,
        );
    _refreshHistorySignalConsumers();
    _primeUpcomingQueue();
    unawaited(_refreshPlaylistRecommendations(currentTrackId));
  }

  void clearSession() {
    state = const PlaybackQueueState();
  }

  Future<void> playShuffled() async {
    if (state.mode == PlaybackQueueMode.none) return;

    final playableIndexes = <int>[];
    for (var i = 0; i < state.queue.length; i++) {
      if (isTrackHidden(state.queue[i])) continue;
      playableIndexes.add(i);
    }

    if (playableIndexes.isEmpty) {
      if (state.mode == PlaybackQueueMode.radio &&
          state.recommendations.isNotEmpty) {
        final track = state
            .recommendations[_random.nextInt(state.recommendations.length)];
        await startRadioSession(track);
      }
      return;
    }

    if (playableIndexes.length > 1) {
      playableIndexes.remove(_resolvedCurrentIndex());
    }
    if (playableIndexes.isEmpty) {
      playableIndexes.add(_resolvedCurrentIndex());
    }

    final index = playableIndexes[_random.nextInt(playableIndexes.length)];
    await playQueueIndex(index);
  }

  Future<void> handleTrackChanged(String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    if (state.mode == PlaybackQueueMode.none) return;

    final previousTrackId = state.currentTrackId;
    final nextPlayedTrackIds =
        previousTrackId != null && previousTrackId != videoId
            ? _playedTrackIdsAfterCurrentProgress(previousTrackId)
            : state.playedTrackIds;

    final matchingIndex =
        state.queue.indexWhere((track) => extractTrackId(track) == videoId);
    if (matchingIndex >= 0) {
      state = state.copyWith(
        currentIndex: matchingIndex,
        currentTrackId: videoId,
        playedTrackIds: nextPlayedTrackIds,
      );
      if (isTrackHidden(state.queue[matchingIndex])) {
        await playNext();
        return;
      }
    } else {
      state = state.copyWith(
        currentTrackId: videoId,
        playedTrackIds: nextPlayedTrackIds,
      );
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: videoId);
    } else if (state.mode == PlaybackQueueMode.playlist) {
      await _refreshPlaylistRecommendations(videoId);
    }
    _primeUpcomingQueue();
  }

  Future<void> playNext({bool logCurrentSkip = true}) async {
    if (state.mode == PlaybackQueueMode.none) return;

    final audioNotifier = ref.read(audioPlayerProvider.notifier);

    if (audioNotifier.hasManagedQueue) {
      final resolvedCurrentIndex = _resolvedCurrentIndex();
      final nextIndex = _nextPlayableIndex(resolvedCurrentIndex);
      if (nextIndex >= 0) {
        if (logCurrentSkip) {
          unawaited(_recordCurrentQueueInteraction('skip'));
        }
        if (nextIndex == resolvedCurrentIndex + 1 &&
            !isTrackHidden(state.queue[nextIndex])) {
          await audioNotifier.skipManagedQueueNext();
        } else {
          await audioNotifier.playManagedQueueIndex(nextIndex);
        }
        return;
      }
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    if (resolvedCurrentIndex != state.currentIndex) {
      state = state.copyWith(currentIndex: resolvedCurrentIndex);
    }

    final nextIndex = _nextPlayableIndex(resolvedCurrentIndex);
    if (nextIndex >= 0) {
      if (logCurrentSkip) {
        unawaited(_recordCurrentQueueInteraction('skip'));
      }
      await playQueueIndex(nextIndex);
      return;
    }

    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      final refreshedNextIndex = _nextPlayableIndex(_resolvedCurrentIndex());
      if (refreshedNextIndex >= 0) {
        if (logCurrentSkip) {
          unawaited(_recordCurrentQueueInteraction('skip'));
        }
        await playQueueIndex(refreshedNextIndex);
      }
    }
  }

  Future<void> playPrevious({bool logCurrentSkip = true}) async {
    if (state.mode == PlaybackQueueMode.none) return;
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (audioNotifier.hasManagedQueue) {
      if (logCurrentSkip && _resolvedCurrentIndex() > 0) {
        unawaited(_recordCurrentQueueInteraction('skip'));
      }
      await audioNotifier.skipManagedQueuePrevious();
      return;
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    final previousIndex = max(0, resolvedCurrentIndex - 1);
    if (logCurrentSkip && previousIndex != resolvedCurrentIndex) {
      unawaited(_recordCurrentQueueInteraction('skip'));
    }
    await playQueueIndex(previousIndex);
  }

  Future<void> playQueueIndex(int index) async {
    if (index < 0 || index >= state.queue.length) return;
    final track = state.queue[index];
    final videoId = extractTrackId(track);
    if (videoId == null) return;

    final previousTrackId = state.currentTrackId;
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (audioNotifier.hasManagedQueue) {
      await audioNotifier.playManagedQueueIndex(index);
    } else {
      final localPath = track['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          File(localPath).existsSync()) {
        final loaded = await audioNotifier.loadLocalWithMeta(localPath, track);
        if (!loaded) return;
        await audioNotifier.play();
      } else {
        await audioNotifier.streamYoutube(videoId, track);
      }
    }
    state = state.copyWith(
      currentIndex: index,
      currentTrackId: videoId,
      playedTrackIds: previousTrackId != null && previousTrackId != videoId
          ? _playedTrackIdsAfterCurrentProgress(previousTrackId)
          : state.playedTrackIds,
    );
    _primeUpcomingQueue();
  }

  void removeFromQueue(String videoId) {
    if (state.mode != PlaybackQueueMode.radio) return;

    final index =
        state.queue.indexWhere((track) => extractTrackId(track) == videoId);
    if (index < 0) return;

    final updatedQueue = [...state.queue]..removeAt(index);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    final resolvedCurrentIndex = _resolvedCurrentIndex();
    if (index == resolvedCurrentIndex) {
      final replacementIndex = index.clamp(0, updatedQueue.length - 1).toInt();
      state = state.copyWith(
        queue: updatedQueue,
        currentIndex: replacementIndex,
        currentTrackId: extractTrackId(updatedQueue[replacementIndex]),
        playedTrackIds:
            state.playedTrackIds.where((id) => id != videoId).toList(),
      );
      unawaited(audioNotifier.removeManagedQueueItem(index));
      unawaited(playQueueIndex(replacementIndex));
      return;
    }

    var nextCurrentIndex = resolvedCurrentIndex;
    if (index < nextCurrentIndex) {
      nextCurrentIndex -= 1;
    }
    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex,
      playedTrackIds:
          state.playedTrackIds.where((id) => id != videoId).toList(),
    );
    unawaited(audioNotifier.removeManagedQueueItem(index));
    _primeUpcomingQueue();
  }

  void setQueueTrackHidden(String trackId, bool isHidden) {
    if (state.mode == PlaybackQueueMode.none) return;
    final updatedQueue = state.queue.map((track) {
      final id = extractTrackId(track);
      if (id != trackId) return track;
      return copyTrackWithHidden(track, isHidden);
    }).toList(growable: false);
    state = state.copyWith(queue: updatedQueue);
    _primeUpcomingQueue();
  }

  void removePlaylistTrack(String trackId) {
    if (state.mode != PlaybackQueueMode.playlist) return;
    final index =
        state.queue.indexWhere((track) => extractTrackId(track) == trackId);
    if (index < 0) return;

    final updatedQueue = [...state.queue]..removeAt(index);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (updatedQueue.isEmpty) {
      clearSession();
      return;
    }

    var nextCurrentIndex = _resolvedCurrentIndex();
    if (index < nextCurrentIndex) {
      nextCurrentIndex -= 1;
    }
    nextCurrentIndex =
        nextCurrentIndex.clamp(0, updatedQueue.length - 1).toInt();
    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex,
      currentTrackId: extractTrackId(updatedQueue[nextCurrentIndex]),
      playedTrackIds:
          state.playedTrackIds.where((id) => id != trackId).toList(),
    );
    unawaited(audioNotifier.removeManagedQueueItem(index));
    _primeUpcomingQueue();
  }

  Future<void> enqueueTrack(dynamic track, {bool playNext = true}) async {
    final normalizedTrack = normalizeTrack(track);
    final videoId = extractTrackId(normalizedTrack);
    if (videoId == null) return;

    if (state.mode == PlaybackQueueMode.none || state.queue.isEmpty) {
      await startRadioSession(normalizedTrack);
      return;
    }

    if (state.queue.any((entry) => extractTrackId(entry) == videoId)) {
      return;
    }

    final nextRecommendations = state.recommendations
        .where((entry) => extractTrackId(entry) != videoId)
        .toList(growable: false);
    final resolvedCurrentIndex = _resolvedCurrentIndex();
    final insertIndex = playNext
        ? (resolvedCurrentIndex + 1).clamp(0, state.queue.length).toInt()
        : state.queue.length;
    final updatedQueue = [...state.queue];
    updatedQueue.insert(insertIndex, normalizedTrack);
    state = state.copyWith(
      queue: updatedQueue,
      recommendations: nextRecommendations,
    );
    unawaited(ref.read(audioPlayerProvider.notifier).insertManagedQueueTracks(
      insertIndex,
      [normalizedTrack],
    ));
    _primeTracks([normalizedTrack], limit: 1);
  }

  void reorderQueue(int oldIndex, int newIndex) {
    if (oldIndex < 0 || oldIndex >= state.queue.length) return;
    final normalizedNewIndex = newIndex.clamp(0, state.queue.length);
    final updatedQueue = [...state.queue];
    final movedTrack = updatedQueue.removeAt(oldIndex);
    var targetIndex = normalizedNewIndex;
    if (targetIndex > oldIndex) {
      targetIndex -= 1;
    }
    targetIndex = targetIndex.clamp(0, updatedQueue.length);
    updatedQueue.insert(targetIndex, movedTrack);

    final activeTrackId = state.currentTrackId;
    final nextCurrentIndex = activeTrackId == null
        ? state.currentIndex.clamp(0, updatedQueue.length - 1).toInt()
        : updatedQueue
            .indexWhere((track) => extractTrackId(track) == activeTrackId);

    state = state.copyWith(
      queue: updatedQueue,
      currentIndex: nextCurrentIndex < 0 ? 0 : nextCurrentIndex,
    );
    unawaited(
      ref.read(audioPlayerProvider.notifier).moveManagedQueueItem(
            oldIndex,
            normalizedNewIndex,
          ),
    );

    if (state.mode == PlaybackQueueMode.playlist && state.playlistId != null) {
      ref
          .read(playlistProvider.notifier)
          .replaceTracks(state.playlistId!, updatedQueue);
    }
    _primeUpcomingQueue();
  }

  void reorderUpcomingQueue(int oldIndex, int newIndex) {
    final activeIndex = _resolvedCurrentIndex();
    final upcomingStartIndex = activeIndex + 1;
    if (upcomingStartIndex >= state.queue.length) return;

    final upcomingLength = state.queue.length - upcomingStartIndex;
    if (oldIndex < 0 || oldIndex >= upcomingLength) return;

    final actualOldIndex = upcomingStartIndex + oldIndex;
    final actualNewIndex = upcomingStartIndex + newIndex;
    reorderQueue(actualOldIndex, actualNewIndex);
  }

  Future<void> loadMore() async {
    if (state.mode == PlaybackQueueMode.radio) {
      await _appendRadioRecommendations(seedId: state.currentTrackId);
      return;
    }
    if (state.mode == PlaybackQueueMode.playlist) {
      await _refreshPlaylistRecommendations(
        state.currentTrackId,
        append: true,
      );
    }
  }

  Future<void> _appendRadioRecommendations({String? seedId}) async {
    if (state.mode != PlaybackQueueMode.radio) return;
    final effectiveSeedId = seedId ?? state.currentTrackId;
    if (effectiveSeedId == null || effectiveSeedId.isEmpty) return;
    if (state.isLoadingQueue) return;

    final remaining = state.queue.length - _resolvedCurrentIndex() - 1;
    if (remaining > 5) return;

    state = state.copyWith(isLoadingQueue: true);
    try {
      final existingIds =
          state.queue.map(extractTrackId).whereType<String>().toSet();
      final fetchedTracks = await _fetchRecommendations(effectiveSeedId);
      final newTracks = fetchedTracks
          .where((track) => !existingIds.contains(extractTrackId(track)))
          .toList(growable: false);
      if (newTracks.isNotEmpty) {
        state = state.copyWith(queue: [...state.queue, ...newTracks]);
        unawaited(
          ref.read(audioPlayerProvider.notifier).appendManagedQueueTracks(
                newTracks,
              ),
        );
        _primeTracks(newTracks);
      }
    } catch (_) {
      // Radio queue loading stays silent.
    } finally {
      if (state.mode == PlaybackQueueMode.radio) {
        state = state.copyWith(isLoadingQueue: false);
      }
    }
  }

  Future<void> _refreshPlaylistRecommendations(
    String? seedId, {
    bool append = false,
  }) async {
    if (state.mode != PlaybackQueueMode.playlist) return;
    if (seedId == null || seedId.isEmpty) return;
    if (state.isLoadingRecommendations) return;

    final existingRecommendations = state.recommendations;
    state = state.copyWith(
      isLoadingRecommendations: true,
    );
    try {
      final excludedIds = <String>{
        ...state.queue.map(extractTrackId).whereType<String>(),
        if (append)
          ...state.recommendations.map(extractTrackId).whereType<String>(),
      };
      final targetCount = append ? 8 : 10;

      List<Map<String, dynamic>> similarTracks = const [];
      try {
        similarTracks = await _fetchSimilarTracks(seedId, limit: targetCount);
      } catch (_) {
        similarTracks = const [];
      }

      List<Map<String, dynamic>> fallbackTracks = const [];
      if (similarTracks.length < targetCount) {
        try {
          fallbackTracks = await _fetchRecommendations(
            seedId,
            limit: targetCount - similarTracks.length,
          );
        } catch (_) {
          fallbackTracks = const [];
        }
      }

      final fetchedTracks = _uniqueTracks(
        [
          ...similarTracks,
          ...fallbackTracks,
        ],
        excludedIds: excludedIds,
      );
      final nextRecommendations = fetchedTracks
          .where((track) => !excludedIds.contains(extractTrackId(track)))
          .toList(growable: false);
      if (!append && nextRecommendations.isEmpty) {
        state = state.copyWith(recommendations: existingRecommendations);
        return;
      }
      state = state.copyWith(
        recommendations: append
            ? [...state.recommendations, ...nextRecommendations]
            : nextRecommendations,
      );
      _primeTracks(nextRecommendations);
    } catch (_) {
      // Recommendation shelf loading stays silent.
    } finally {
      if (state.mode == PlaybackQueueMode.playlist) {
        state = state.copyWith(isLoadingRecommendations: false);
      }
    }
  }
}

final playbackQueueProvider =
    StateNotifierProvider<PlaybackQueueNotifier, PlaybackQueueState>((ref) {
  final notifier = PlaybackQueueNotifier(ref);
  final audioNotifier = ref.read(audioPlayerProvider.notifier);

  Future<void> handleTrackCompleted() async {
    notifier._markCurrentTrackPlayedIfEligible();
    await notifier._recordCurrentQueueInteraction('complete');
    await notifier.playNext(logCurrentSkip: false);
  }

  Future<void> handleTrackChanged(String? videoId) =>
      notifier.handleTrackChanged(videoId);

  audioNotifier.onTrackCompleted = handleTrackCompleted;
  audioNotifier.onTrackChanged = handleTrackChanged;
  if (globalAudioHandler is AuralisAudioHandler) {
    final handler = globalAudioHandler as AuralisAudioHandler;
    handler.onSkipToNext = notifier.playNext;
    handler.onSkipToPrevious = notifier.playPrevious;
  }

  ref.onDispose(() {
    if (identical(audioNotifier.onTrackCompleted, handleTrackCompleted)) {
      audioNotifier.onTrackCompleted = null;
    }
    if (identical(audioNotifier.onTrackChanged, handleTrackChanged)) {
      audioNotifier.onTrackChanged = null;
    }
    if (globalAudioHandler is AuralisAudioHandler) {
      final handler = globalAudioHandler as AuralisAudioHandler;
      if (identical(handler.onSkipToNext, notifier.playNext)) {
        handler.onSkipToNext = null;
      }
      if (identical(handler.onSkipToPrevious, notifier.playPrevious)) {
        handler.onSkipToPrevious = null;
      }
    }
  });

  return notifier;
});

Future<List<Map<String, dynamic>>> _loadLocalLibraryTracks(
    String scopeId) async {
  final dir = await getScopedDownloadsDirectory(scopeId);
  final files = dir.listSync().where((f) => f.path.endsWith('.mp3')).toList()
    ..sort((a, b) {
      final aTime = a.statSync().modified;
      final bTime = b.statSync().modified;
      return bTime.compareTo(aTime);
    });

  final tracks = <Map<String, dynamic>>[];
  for (final file in files) {
    if (file is File) {
      try {
        if (file.lengthSync() < 10000) {
          file.deleteSync();
          final jsonPath = file.path.replaceAll('.mp3', '.json');
          if (File(jsonPath).existsSync()) File(jsonPath).deleteSync();
          continue;
        }
      } catch (_) {}
    }

    final jsonPath = file.path.replaceAll('.mp3', '.json');
    if (File(jsonPath).existsSync()) {
      final meta = Map<String, dynamic>.from(
        jsonDecode(File(jsonPath).readAsStringSync()) as Map,
      );
      meta['local_path'] = file.path;
      meta['is_downloaded_locally'] = true;
      tracks.add(normalizeTrack(meta));
    } else {
      tracks.add({
        'title': file.path.split('/').last.replaceAll('.mp3', ''),
        'local_path': file.path,
        'duration': 0,
        'is_downloaded_locally': true,
      });
    }
  }
  return tracks;
}

Future<List<Map<String, dynamic>>> _loadCloudLibraryTracks() async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  if (client == null || userId == null || userId.isEmpty) {
    return const [];
  }

  try {
    final rows = await client
        .from('library_tracks')
        .select('track_id,track_data,added_at')
        .eq('user_id', userId)
        .order('added_at', ascending: false);
    return (rows as List<dynamic>).map((rawRow) {
      final row = Map<String, dynamic>.from(rawRow as Map);
      final payload = normalizeTrack(
        Map<String, dynamic>.from(row['track_data'] as Map),
      );
      final trackId = row['track_id']?.toString();
      if (trackId != null && trackId.isNotEmpty) {
        payload['id'] = trackId;
        payload['videoId'] = trackId;
      }
      payload['is_cloud_saved'] = true;
      return payload;
    }).toList(growable: false);
  } catch (error) {
    debugPrint('Cloud library load failed: $error');
    return const [];
  }
}

final libraryProvider = FutureProvider<List<Map<String, dynamic>>>((ref) async {
  final authState = ref.watch(authProvider);
  final scopeId = authState.storageScopeId;
  ref.watch(storageRefreshTickProvider);

  final localTracks = await _loadLocalLibraryTracks(scopeId);
  if (!authState.isAuthenticated || !isSupabaseConfigured) {
    return localTracks;
  }

  final cloudTracks = await _loadCloudLibraryTracks();
  final localById = <String, Map<String, dynamic>>{};
  for (final track in localTracks) {
    final trackId = extractTrackId(track);
    if (trackId != null && trackId.isNotEmpty) {
      localById[trackId] = track;
    }
  }

  final merged = <Map<String, dynamic>>[];
  for (final cloudTrack in cloudTracks) {
    final trackId = extractTrackId(cloudTrack);
    final localTrack = trackId == null ? null : localById.remove(trackId);
    merged.add({
      ...cloudTrack,
      if (localTrack != null) ...localTrack,
      'is_cloud_saved': true,
      'is_downloaded_locally': localTrack != null,
    });
  }

  for (final localTrack in localById.values) {
    merged.add({
      ...localTrack,
      'is_cloud_saved': false,
      'is_downloaded_locally': true,
    });
    unawaited(upsertCloudLibraryTrack(localTrack));
  }

  return merged;
});
