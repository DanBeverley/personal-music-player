import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';
import 'interaction_events.dart';
import 'track_metadata.dart';

int _parseLibraryInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
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
    'duration': _parseLibraryInt(track['duration']),
    if (track['album'] != null) 'album': track['album'],
    if (track['album_id'] != null) 'album_id': track['album_id'],
  };
}

Future<File> _localLikedTracksFile(String scopeId) {
  return getScopedDataFile('liked_tracks.json', scopeId);
}

Future<List<Map<String, dynamic>>> _loadLocalLikedTracks(String scopeId) async {
  try {
    final file = await _localLikedTracksFile(scopeId);
    if (!await file.exists()) return const [];
    final raw = jsonDecode(await file.readAsString());
    if (raw is! List) return const [];
    return raw
        .whereType<Map>()
        .map((entry) => normalizeTrack(Map<String, dynamic>.from(entry)))
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .map((track) => {
              ...track,
              'is_liked_locally': true,
              'is_cloud_saved': true,
            })
        .toList(growable: false);
  } catch (error) {
    debugPrint('Local liked tracks load failed: $error');
    return const [];
  }
}

Future<void> _writeLocalLikedTracks(
  String scopeId,
  List<Map<String, dynamic>> tracks,
) async {
  try {
    final file = await _localLikedTracksFile(scopeId);
    await file.parent.create(recursive: true);
    await file.writeAsString(jsonEncode(tracks));
  } catch (error) {
    debugPrint('Local liked tracks save failed: $error');
  }
}

Future<void> _upsertLocalLikedTrack(Map<String, dynamic> payload) async {
  final trackId = extractTrackId(payload);
  if (trackId == null || trackId.isEmpty) return;
  final tracks = await _loadLocalLikedTracks(activeStorageScopeId);
  final merged = <Map<String, dynamic>>[];
  var inserted = false;
  for (final track in tracks) {
    if (extractTrackId(track) == trackId) {
      merged.add({
        ...track,
        ...payload,
        'is_liked_locally': true,
        'is_cloud_saved': true,
      });
      inserted = true;
    } else {
      merged.add(track);
    }
  }
  if (!inserted) {
    merged.insert(0, {
      ...payload,
      'is_liked_locally': true,
      'is_cloud_saved': true,
      'liked_at': DateTime.now().toUtc().toIso8601String(),
    });
  }
  await _writeLocalLikedTracks(
    activeStorageScopeId,
    merged.take(500).toList(),
  );
}

Future<void> _removeLocalLikedTrack(String trackId) async {
  if (trackId.isEmpty) return;
  final tracks = await _loadLocalLikedTracks(activeStorageScopeId);
  await _writeLocalLikedTracks(
    activeStorageScopeId,
    tracks.where((track) => extractTrackId(track) != trackId).toList(),
  );
}

Future<void> upsertCloudLibraryTrack(
  dynamic rawTrack, {
  bool persistLocalLike = true,
}) async {
  final payload = _cloudTrackPayload(rawTrack);
  final trackId = extractTrackId(payload);
  if (persistLocalLike) {
    await _upsertLocalLikedTrack(payload);
  }
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
  await _removeLocalLikedTrack(trackId ?? '');
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

List<Map<String, dynamic>> _mergeLibraryTracks({
  required List<Map<String, dynamic>> likedTracks,
  required List<Map<String, dynamic>> cloudTracks,
  required List<Map<String, dynamic>> downloadedTracks,
}) {
  final merged = <Map<String, dynamic>>[];
  final indexes = <String, int>{};

  void add(Map<String, dynamic> track) {
    final trackId = extractTrackId(track);
    if (trackId == null || trackId.isEmpty) {
      merged.add(track);
      return;
    }
    final existingIndex = indexes[trackId];
    if (existingIndex == null) {
      indexes[trackId] = merged.length;
      merged.add(track);
      return;
    }
    merged[existingIndex] = {
      ...merged[existingIndex],
      ...track,
      'is_cloud_saved':
          merged[existingIndex]['is_cloud_saved'] == true ||
              track['is_cloud_saved'] == true,
      'is_liked_locally':
          merged[existingIndex]['is_liked_locally'] == true ||
              track['is_liked_locally'] == true,
      'is_downloaded_locally':
          merged[existingIndex]['is_downloaded_locally'] == true ||
              track['is_downloaded_locally'] == true,
    };
  }

  for (final track in likedTracks) {
    add(track);
  }
  for (final track in cloudTracks) {
    add({...track, 'is_cloud_saved': true});
  }
  for (final track in downloadedTracks) {
    add(track);
  }
  return merged;
}

Future<List<Map<String, dynamic>>> _loadLocalLibraryTracks(
  String scopeId,
) async {
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
  final likedTracks = await _loadLocalLikedTracks(scopeId);
  if (!authState.isAuthenticated || !isSupabaseConfigured) {
    return _mergeLibraryTracks(
      likedTracks: likedTracks,
      cloudTracks: const [],
      downloadedTracks: localTracks,
    );
  }

  final cloudTracks = await _loadCloudLibraryTracks();
  final localById = <String, Map<String, dynamic>>{};
  for (final track in localTracks) {
    final trackId = extractTrackId(track);
    if (trackId != null && trackId.isNotEmpty) {
      localById[trackId] = track;
    }
  }

  for (final localTrack in localById.values) {
    unawaited(upsertCloudLibraryTrack(localTrack, persistLocalLike: false));
  }

  return _mergeLibraryTracks(
    likedTracks: likedTracks,
    cloudTracks: cloudTracks,
    downloadedTracks: localTracks,
  );
});
