// ignore_for_file: experimental_member_use

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:just_audio/just_audio.dart';

import 'auth_provider.dart';
import 'playback_models.dart';
import 'proxy_runtime.dart';
import 'track_metadata.dart';

class AudioStreamRuntime {
  final Map<String, ResolvedStreamSource> _streamCache = {};
  final Map<String, Future<ResolvedStreamSource>> _pendingStreamLookups = {};
  final Set<String> _unavailableStreamTrackIds = <String>{};
  final Map<String, String> _unavailableStreamReasons = <String, String>{};
  final Map<String, DateTime> _recentPrepareBatches = <String, DateTime>{};
  final Map<String, Future<void>> _pendingPrepareBatches =
      <String, Future<void>>{};
  final Map<String, Future<void>> _fullPrefetchTasks = {};
  int _latencySummaryProbeCounter = 0;

  Uri _proxyUri(String path) => buildProxyUri(path);

  ResolvedStreamSource? freshStreamSource(String videoId) {
    final cached = _streamCache[videoId];
    if (cached != null && cached.isFresh) {
      return cached;
    }
    return null;
  }

  ResolvedStreamSource buildProxyStreamSource(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    return ResolvedStreamSource(
      url: _proxyUri('/proxy_stream/$videoId').toString(),
      headers: const {},
      fetchedAt: fetchedAt ?? DateTime.now(),
    );
  }

  ResolvedStreamSource markPreparedStream(
    String videoId, {
    DateTime? fetchedAt,
  }) {
    final source = buildProxyStreamSource(videoId, fetchedAt: fetchedAt);
    _streamCache[videoId] = source;
    return source;
  }

  bool isUnavailableTrackId(String? videoId) {
    final normalized = videoId?.trim();
    if (normalized == null || normalized.isEmpty) return false;
    return _unavailableStreamTrackIds.contains(normalized);
  }

  String? unavailableReason(String? videoId) {
    final normalized = videoId?.trim();
    if (normalized == null || normalized.isEmpty) return null;
    return _unavailableStreamReasons[normalized];
  }

  void registerUnavailableTrack(String? videoId, [String? reason]) {
    final normalizedId = videoId?.trim();
    if (normalizedId == null || normalizedId.isEmpty) return;
    _unavailableStreamTrackIds.add(normalizedId);
    if (reason != null && reason.trim().isNotEmpty) {
      _unavailableStreamReasons[normalizedId] = reason.trim();
    }
    _streamCache.remove(normalizedId);
  }

  bool isKnownUnavailableRemoteTrack(Map<String, dynamic> track) {
    final localPath = track['local_path']?.toString();
    if (localPath != null && localPath.isNotEmpty && File(localPath).existsSync()) {
      return false;
    }
    final videoId = extractTrackId(track);
    return videoId != null && _unavailableStreamTrackIds.contains(videoId);
  }

  String _sanitizeCacheKey(String value) {
    return value.replaceAll(RegExp(r'[^A-Za-z0-9_\\-]'), '_');
  }

  Future<Directory> streamCacheDirectory() async {
    final userDir = await getScopedUserDirectory();
    final cacheDir = Directory('${userDir.path}/stream_cache');
    if (!cacheDir.existsSync()) {
      cacheDir.createSync(recursive: true);
    }
    return cacheDir;
  }

  Future<File> cacheFileForVideoId(String videoId) async {
    final cacheDir = await streamCacheDirectory();
    return File('${cacheDir.path}/${_sanitizeCacheKey(videoId)}.audio');
  }

  Future<File> cacheMarkerForVideoId(String videoId) async {
    final cacheFile = await cacheFileForVideoId(videoId);
    return File('${cacheFile.path}.done');
  }

  Future<LockCachingAudioSource> buildCachingSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) async {
    final cacheFile = await cacheFileForVideoId(videoId);
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

  Future<void> prepareQueueSession(
    Iterable<String?> videoIds, {
    String? currentVideoId,
    bool activeQueue = false,
    int lookahead = 8,
    Future<void> Function(String videoId)? onFallbackPrewarm,
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
        .where((videoId) => freshStreamSource(videoId) == null)
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
        activeQueue ? const Duration(seconds: 4) : const Duration(seconds: 8);
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
          registerUnavailableTrack(key.toString(), failureReason);
        });
      }
      if (prepared is Map) {
        prepared.forEach((key, dynamic value) {
          markPreparedStream(key.toString());
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
        if (onFallbackPrewarm != null) {
          unawaited(onFallbackPrewarm(id));
        } else {
          unawaited(prewarmStream(id));
        }
      }
    } finally {
      _pendingPrepareBatches.remove(batchKey);
    }
  }

  Future<void> cleanupFullPrefetchCache(Set<String> keepIds) async {
    final cacheDir = await streamCacheDirectory();
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

  Future<void> prefetchTrackToTempStorage(String videoId) async {
    final existing = _fullPrefetchTasks[videoId];
    if (existing != null) {
      return existing;
    }

    final task = () async {
      final cacheFile = await cacheFileForVideoId(videoId);
      final markerFile = await cacheMarkerForVideoId(videoId);
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
        final request = http.Request('GET', _proxyUri('/proxy_stream/$videoId'));
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
          '[EBB] full-prefetched $videoId bytes=${cacheFile.lengthSync()}',
        );
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

    await cleanupFullPrefetchCache(keepIds);
    for (final id in targetIds) {
      unawaited(prefetchTrackToTempStorage(id));
    }
  }

  Future<ResolvedStreamSource> fetchStreamSource(String videoId) async {
    final cached = freshStreamSource(videoId);
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
      registerUnavailableTrack(videoId, failureReason);
      throw Exception(failureReason ?? 'Track unavailable for streaming');
    }
    if (prepared is! Map || !prepared.containsKey(videoId)) {
      registerUnavailableTrack(videoId, 'Track unavailable for streaming');
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
    return markPreparedStream(videoId);
  }

  Future<ResolvedStreamSource> fetchDirectStreamSource(String videoId) async {
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

  Future<ResolvedStreamSource> resolveStreamSource(String videoId) {
    final cached = freshStreamSource(videoId);
    if (cached != null) {
      return Future<ResolvedStreamSource>.value(cached);
    }

    final pending = _pendingStreamLookups[videoId];
    if (pending != null) {
      return pending;
    }

    final lookup = fetchStreamSource(videoId);
    _pendingStreamLookups[videoId] = lookup;
    lookup.whenComplete(() {
      if (identical(_pendingStreamLookups[videoId], lookup)) {
        _pendingStreamLookups.remove(videoId);
      }
    });
    return lookup;
  }

  Future<void> prewarmStream(
    String? videoId, {
    bool immediatePlayback = false,
  }) async {
    if (videoId == null || videoId.isEmpty) return;
    try {
      if (immediatePlayback) {
        await prepareQueueSession(
          [videoId],
          currentVideoId: videoId,
          activeQueue: true,
          lookahead: 1,
        );
        return;
      }
      await resolveStreamSource(videoId);
    } catch (_) {
      // Prewarm should stay silent. Playback will surface real errors later.
    }
  }

  Future<void> prewarmStreams(
    Iterable<String?> videoIds, {
    int lookahead = 18,
    bool immediatePlayback = false,
    String? currentVideoId,
  }) async {
    String? prioritizedId = currentVideoId;
    if (prioritizedId == null || prioritizedId.isEmpty) {
      for (final rawId in videoIds) {
        final candidate = rawId?.toString();
        if (candidate != null && candidate.isNotEmpty) {
          prioritizedId = candidate;
          break;
        }
      }
    }
    await prepareQueueSession(
      videoIds,
      lookahead: lookahead,
      activeQueue: immediatePlayback,
      currentVideoId: prioritizedId,
    );
  }
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
