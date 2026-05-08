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
