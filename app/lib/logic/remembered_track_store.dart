import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'auth_provider.dart';
import 'track_metadata.dart';

int _parseRememberedTrackInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

Map<String, dynamic>? buildRememberedTrackSnapshot(
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
    'duration': _parseRememberedTrackInt(normalized['duration']),
    'album': normalized['album'] ?? normalized['album_title'],
    'album_title': normalized['album_title'] ?? normalized['album'],
    'album_id': normalized['album_id'],
    if (localPath != null && localPath.trim().isNotEmpty)
      'local_path': localPath,
  };
}

class RememberedTrackStore {
  const RememberedTrackStore();

  Future<File> _snapshotFile() => getScopedDataFile('last_track_snapshot.json');

  bool hasValidDownloadedAudio(String path) {
    try {
      final file = File(path);
      return file.existsSync() && file.lengthSync() >= 10000;
    } catch (_) {
      return false;
    }
  }

  Future<void> persistSnapshot(Map<String, dynamic>? snapshot) async {
    if (snapshot == null) return;
    try {
      final file = await _snapshotFile();
      await file.writeAsString(jsonEncode(snapshot));
    } catch (error) {
      debugPrint('Last track snapshot save failed: $error');
    }
  }

  Future<Map<String, dynamic>?> restoreSnapshot() async {
    try {
      final file = await _snapshotFile();
      if (!file.existsSync()) return null;
      final raw = (await file.readAsString()).trim();
      if (raw.isEmpty) return null;
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return null;
      final snapshot = Map<String, dynamic>.from(decoded);
      final trackId = extractTrackId(snapshot);
      if (trackId == null || trackId.isEmpty) return null;
      final localPath = snapshot['local_path']?.toString();
      if (localPath != null &&
          localPath.isNotEmpty &&
          !hasValidDownloadedAudio(localPath)) {
        snapshot.remove('local_path');
      }
      return snapshot;
    } catch (error) {
      debugPrint('Last track snapshot restore failed: $error');
      return null;
    }
  }
}
