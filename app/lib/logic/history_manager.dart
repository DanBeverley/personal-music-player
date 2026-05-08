import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'auth_provider.dart';
import 'interaction_events.dart';
import 'track_metadata.dart';

int _historyParseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
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
    'duration': _historyParseInt(normalized['duration']),
    'album': normalized['album'] ?? normalized['album_title'],
    'album_title': normalized['album_title'] ?? normalized['album'],
    'album_id': normalized['album_id'],
    if (localPath != null && localPath.trim().isNotEmpty)
      'local_path': localPath,
  };
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
              currentVersion = _historyParseInt(decoded['version']);
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
    if (_historyParseInt(merged['duration']) <= 0 &&
        _historyParseInt(existing['duration']) > 0) {
      merged['duration'] = _historyParseInt(existing['duration']);
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
      debugPrint('Cloud history write error: $e');
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
      debugPrint('History Write Error: $e');
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
      debugPrint('History Read Error: $e');
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
