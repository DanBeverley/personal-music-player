import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'library_catalog_provider.dart';
import 'proxy_runtime.dart';
import 'track_metadata.dart';

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

Future<List<Map<String, dynamic>>> hydrateTracksFromIds(
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
      if (isTrackMetadataIncomplete(localTrack)) {
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
