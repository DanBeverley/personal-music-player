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
import 'cloud_search_queries.dart';
import 'playback_models.dart';
import 'playlist_provider.dart';
import 'proxy_runtime.dart';
import 'search_semantics.dart';
import 'track_metadata.dart';
import 'package:audio_service/audio_service.dart';
import 'package:just_audio/just_audio.dart' hide PlayerState;

export 'cloud_search_queries.dart';
export 'playback_models.dart';
export 'proxy_runtime.dart';
export 'recommendation_feed_models.dart';
export 'search_semantics.dart';
export 'suggest_provider.dart';
export 'track_metadata.dart';

final AuralisAudioHandler _bootstrapAudioHandler = AuralisAudioHandler();
AudioHandler globalAudioHandler = _bootstrapAudioHandler;

Future<T> _runSearchRequest<T>(Future<T> future, Duration timeout) {
  return runSearchRequest(future, timeout);
}

void logProxyDiagnostic(String area, String message) {
  debugProxyLog(area, message);
}

String compactProxyDiagnosticValue(Object? value) {
  return compactDiagnosticValue(value);
}

Future<void> initAudioService() async {
  try {
    globalAudioHandler = await AudioService.init(
      builder: () => _bootstrapAudioHandler,
      config: AudioServiceConfig(
        androidNotificationChannelId: 'com.danbeverley.ebb.channel.audio',
        androidNotificationChannelName: 'EBB Audio Playback',
        androidNotificationChannelDescription:
            'Playback controls for EBB on notifications and the Android lock screen.',
        androidNotificationOngoing: true,
        androidNotificationClickStartsActivity: true,
        androidResumeOnClick: true,
        androidStopForegroundOnPause: false,
        fastForwardInterval: const Duration(seconds: 10),
        rewindInterval: const Duration(seconds: 10),
      ),
    ).timeout(const Duration(seconds: 8));
  } on TimeoutException catch (error) {
    debugPrint('AudioService init timed out: $error');
  } catch (error) {
    debugPrint('AudioService init failed: $error');
  }
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

Map<String, String> _parseHeaders(dynamic value) {
  if (value is! Map) return const {};
  final headers = <String, String>{};
  value.forEach((key, dynamic entryValue) {
    if (key == null || entryValue == null) return;
    headers[key.toString()] = entryValue.toString();
  });
  return headers;
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
  bool _streamLoopRestartInProgress = false;
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
    if (_streamTransitionInProgress || state.isLooping) return;
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
    final hasReachedTrackEnd = !state.isLooping &&
        !state.isPlaying &&
        state.duration > 0 &&
        state.currentPosition >= state.duration;
    final processingState = state.isDownloading
        ? AudioProcessingState.buffering
        : state.videoId == null || state.videoId!.isEmpty
            ? AudioProcessingState.idle
            : hasReachedTrackEnd
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
    final activeTrack = _currentTrackSnapshot();
    final albumTitle = _currentTrackAlbumTitle(activeTrack);
    final artistName =
        _currentTrackArtist(activeTrack) ?? state.artist ?? 'Unknown Artist';
    final artUri = _currentTrackArtUri(activeTrack);
    return MediaItem(
      id: state.videoId ?? 'current_track',
      album: albumTitle,
      title: state.currentTrackName,
      artist: artistName,
      duration: Duration(seconds: state.duration),
      artUri: artUri,
      extras: {
        'displayTitle': state.currentTrackName,
        'displaySubtitle': artistName,
        'displayDescription': albumTitle,
      },
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
      extras: {
        'displayTitle': track['title']?.toString() ?? 'Unknown Track',
        'displaySubtitle': _trackArtist(track) ?? 'Unknown Artist',
        'displayDescription':
            track['album']?.toString() ?? track['album_title']?.toString() ?? '',
      },
    );
  }

  Map<String, dynamic>? _currentTrackSnapshot() {
    final activeTrackId = state.videoId?.trim();
    if (activeTrackId == null || activeTrackId.isEmpty) {
      return _restorableTrackMeta == null
          ? null
          : Map<String, dynamic>.from(_restorableTrackMeta!);
    }
    if (_managedQueueActive) {
      final currentIndex = streamPlayer.currentIndex ?? 0;
      if (currentIndex >= 0 && currentIndex < _managedQueueTracks.length) {
        final track = normalizeTrack(_managedQueueTracks[currentIndex]);
        final trackId = extractTrackId(track)?.trim() ?? '';
        if (trackId == activeTrackId) {
          return Map<String, dynamic>.from(track);
        }
      }
    }
    final rememberedTrack = _restorableTrackMeta;
    if (rememberedTrack != null) {
      final rememberedId = extractTrackId(rememberedTrack)?.trim() ?? '';
      if (rememberedId == activeTrackId) {
        return Map<String, dynamic>.from(rememberedTrack);
      }
    }
    return null;
  }

  String _currentTrackAlbumTitle(Map<String, dynamic>? track) {
    final album = (track?['album'] ?? track?['album_title'])?.toString().trim();
    if (album != null && album.isNotEmpty) {
      return album;
    }
    return 'EBB';
  }

  String? _currentTrackArtist(Map<String, dynamic>? track) {
    final artist = _trackArtist(track);
    if (artist != null && artist.trim().isNotEmpty) {
      return artist;
    }
    return null;
  }

  Uri? _currentTrackArtUri(Map<String, dynamic>? track) {
    final thumbnail = (track?['thumbnail'] ?? state.thumbnail)?.toString().trim();
    if (thumbnail == null || thumbnail.isEmpty) {
      return null;
    }
    return Uri.tryParse(thumbnail);
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
        final shouldRestartLoop = isCompleted &&
            state.isLooping &&
            !_streamTransitionInProgress &&
            !_streamLoopRestartInProgress;
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
        if (shouldRestartLoop) {
          _completedTrackIdNotified = null;
          unawaited(_restartLoopedStreamPlayback());
          return;
        }
        final shouldNotifyCompletion = isCompleted &&
            !state.isLooping &&
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

  Future<void> _restartLoopedStreamPlayback() async {
    if (!_activeStream || !state.isLooping || _streamLoopRestartInProgress) {
      return;
    }
    _streamLoopRestartInProgress = true;
    try {
      await _runStreamCommand(() async {
        if (!_activeStream || !state.isLooping) return;
        await streamPlayer.seek(Duration.zero);
        if (!streamPlayer.playing) {
          await streamPlayer.play();
        }
      });
      if (!_activeStream || !state.isLooping) return;
      state = state.copyWith(
        isPlaying: true,
        currentPosition: 0,
        currentPositionMs: 0,
      );
    } catch (error) {
      if (!_activeStream || !_desiredStreamPlaying) return;
      state = state.copyWith(
        isPlaying: false,
        isDownloading: false,
        currentTrackName: 'Stream failed: $error',
      );
      unawaited(_handleStreamFailure(error));
    } finally {
      _streamLoopRestartInProgress = false;
    }
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
    _streamLoopRestartInProgress = false;
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

  Future<void> prewarmStreams(
    Iterable<String?> videoIds, {
    int lookahead = 18,
  }) async {
    await _prepareQueueSession(videoIds, lookahead: lookahead);
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
    _streamLoopRestartInProgress = false;
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
      _streamLoopRestartInProgress = false;
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
    _streamLoopRestartInProgress = false;
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
Future<Map<String, dynamic>> _buildSemanticSearchRequestBody(
  Ref ref,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
}) {
  return buildSemanticSearchRequestBody(
    ref,
    query,
    limit: limit,
    deferSideSurfaces: deferSideSurfaces,
  );
}

Future<Map<String, dynamic>> buildSemanticSearchRequestBody(
  Ref ref,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
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
      bumpSearchSignalWeight(
        artistWeights,
        extractSearchArtistHint(track),
        artistWeight,
      );
      bumpSearchSignalWeight(
        queryWeights,
        extractSearchAlbumHint(track),
        albumWeight * 0.8,
      );
      addAlbumWeight(extractSearchAlbumHint(track), albumWeight);
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
    bumpSearchSignalWeight(queryWeights, playlist.name, 1.25);
    for (final track in playlist.tracks.take(16)) {
      bumpSearchSignalWeight(
        artistWeights,
        extractSearchArtistHint(track),
        1.28,
      );
      bumpSearchSignalWeight(
        queryWeights,
        extractSearchAlbumHint(track),
        0.84,
      );
      addAlbumWeight(extractSearchAlbumHint(track), 1.0);
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
    bumpSearchSignalWeight(
      artistWeights,
      extractSearchArtistHint(track),
      1.12,
    );
    bumpSearchSignalWeight(
      queryWeights,
      extractSearchAlbumHint(track),
      0.7,
    );
    addAlbumWeight(extractSearchAlbumHint(track), 0.88);
  }

  bumpSearchSignalWeight(queryWeights, normalizedQuery, 2.45);
  if (looksLikeSemanticTasteQuery(normalizedQuery)) {
    bumpSearchSignalWeight(queryWeights, normalizedQuery, 1.25);
  }

  for (final queryValue in blendedRecentQueries) {
    if (looksLikeSemanticTasteQuery(queryValue)) {
      bumpSearchSignalWeight(queryWeights, queryValue, 1.18);
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
    'defer_side_surfaces': deferSideSurfaces,
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

class _CachedSearchPayload {
  final Map<String, dynamic> payload;
  final DateTime storedAt;

  const _CachedSearchPayload({
    required this.payload,
    required this.storedAt,
  });
}

class SearchFetchResult {
  final Map<String, dynamic>? payload;
  final String status;
  final int? statusCode;

  const SearchFetchResult({
    required this.status,
    this.payload,
    this.statusCode,
  });

  bool get hasPayload => payload != null;
}

final Map<String, _CachedSearchPayload> _searchPayloadCache =
    <String, _CachedSearchPayload>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadForegroundInflight =
    <String, Future<SearchFetchResult>>{};
final Map<String, Future<SearchFetchResult>> _searchPayloadBackgroundInflight =
    <String, Future<SearchFetchResult>>{};
const Duration _searchCacheFreshTtl = Duration(minutes: 4);
const Duration _searchCacheMaxTtl = Duration(minutes: 12);

String _searchPayloadCacheKey(
  Ref ref,
  String query, {
  required int limit,
  bool deferSideSurfaces = false,
}) {
  final normalizedQuery = query.trim().toLowerCase();
  final storageScopeId = ref.read(authProvider).storageScopeId;
  return '$storageScopeId|$limit|$normalizedQuery|${deferSideSurfaces ? 'tracks_first' : 'full'}';
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

Future<SearchFetchResult> fetchSearchPayload(
  Ref ref,
  String query, {
  required int limit,
  required Duration timeout,
  bool preferCache = true,
  bool backgroundRefresh = false,
  bool deferSideSurfaces = false,
}) async {
  final normalizedQuery = query.trim();
  if (normalizedQuery.isEmpty) {
    return const SearchFetchResult(status: 'empty_query');
  }
  final cacheKey = _searchPayloadCacheKey(
    ref,
    normalizedQuery,
    limit: limit,
    deferSideSurfaces: deferSideSurfaces,
  );
  final freshCached = _lookupSearchPayloadCache(cacheKey);
  if (preferCache && freshCached != null && !backgroundRefresh) {
    return SearchFetchResult(
      status: 'cache_hit',
      payload: Map<String, dynamic>.from(freshCached.payload),
    );
  }
  if (!backgroundRefresh) {
    final staleCached = _lookupSearchPayloadCache(cacheKey, allowStale: true);
    if (staleCached != null) {
      unawaited(
        fetchSearchPayload(
          ref,
          normalizedQuery,
          limit: limit,
          timeout: timeout,
          preferCache: false,
          backgroundRefresh: true,
          deferSideSurfaces: deferSideSurfaces,
        ),
      );
      return SearchFetchResult(
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
        deferSideSurfaces: deferSideSurfaces,
      );
      final timedResponse = await _runSearchRequest(
        proxyControlHttpClient.post(
          buildProxyUri('/search'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        ),
        timeout,
      );
      if (timedResponse.statusCode != 200) {
        if (!backgroundRefresh) {
          final staleCached =
              _lookupSearchPayloadCache(cacheKey, allowStale: true);
          if (staleCached != null) {
            return SearchFetchResult(
              status: 'stale_cache',
              payload: Map<String, dynamic>.from(staleCached.payload),
              statusCode: timedResponse.statusCode,
            );
          }
        }
        return SearchFetchResult(
          status: 'http_error',
          statusCode: timedResponse.statusCode,
        );
      }
      final payload = jsonDecode(timedResponse.body) as Map<String, dynamic>;
      _storeSearchPayloadCache(cacheKey, payload);
      return SearchFetchResult(status: 'network_success', payload: payload);
    } on TimeoutException {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const SearchFetchResult(status: 'timeout');
    } catch (_) {
      if (!backgroundRefresh) {
        final staleCached =
            _lookupSearchPayloadCache(cacheKey, allowStale: true);
        if (staleCached != null) {
          return SearchFetchResult(
            status: 'stale_cache',
            payload: Map<String, dynamic>.from(staleCached.payload),
          );
        }
      }
      return const SearchFetchResult(status: 'exception');
    } finally {
      inflightMap.remove(cacheKey);
    }
  }();
  inflightMap[cacheKey] = requestFuture;
  return requestFuture;
}

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
