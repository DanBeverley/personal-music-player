// ignore_for_file: experimental_member_use

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
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
      url: _proxyUri(
        '/playback/stream/${Uri.encodeComponent(videoId)}',
      ).toString(),
      headers: const {},
      fetchedAt: fetchedAt ?? DateTime.now(),
      sourceKind: 'proxy',
    );
  }

  String _preparedPlaybackUrl(String videoId, Map<dynamic, dynamic> payload) {
    final raw = (payload['playback_url'] ?? payload['playback_path'])?.toString();
    if (raw == null || raw.trim().isEmpty) {
      return _proxyUri(
        '/playback/stream/${Uri.encodeComponent(videoId)}',
      ).toString();
    }
    final trimmed = raw.trim();
    if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
      return trimmed;
    }
    return _proxyUri(trimmed.startsWith('/') ? trimmed : '/$trimmed').toString();
  }

  DateTime? _preparedExpiresAt(Map<dynamic, dynamic> payload) {
    final raw = payload['expires_at'];
    if (raw is num && raw > 0) {
      return DateTime.fromMillisecondsSinceEpoch(
        (raw * 1000).round(),
        isUtc: true,
      );
    }
    if (raw is String && raw.trim().isNotEmpty) {
      final parsedNumber = num.tryParse(raw);
      if (parsedNumber != null && parsedNumber > 0) {
        return DateTime.fromMillisecondsSinceEpoch(
          (parsedNumber * 1000).round(),
          isUtc: true,
        );
      }
      return DateTime.tryParse(raw);
    }
    return null;
  }

  ResolvedStreamSource buildPreparedStreamSource(
    String videoId,
    dynamic payload, {
    DateTime? fetchedAt,
  }) {
    if (payload is! Map) {
      return buildProxyStreamSource(videoId, fetchedAt: fetchedAt);
    }
    final headers = _parseHeaders(payload['headers']);
    final source = ResolvedStreamSource(
      url: _preparedPlaybackUrl(videoId, payload),
      headers: headers,
      fetchedAt: fetchedAt ?? DateTime.now(),
      sourceKind: payload['source_kind']?.toString() ?? 'prepared',
      expiresAt: _preparedExpiresAt(payload),
    );
    _streamCache[videoId] = source;
    return source;
  }

  ResolvedStreamSource markPreparedStream(
    String videoId, {
    dynamic payload,
    DateTime? fetchedAt,
  }) {
    final source = buildPreparedStreamSource(
      videoId,
      payload,
      fetchedAt: fetchedAt,
    );
    _streamCache[videoId] = source;
    _unavailableStreamTrackIds.remove(videoId);
    _unavailableStreamReasons.remove(videoId);
    return source;
  }

  void invalidateStreamSource(String? videoId) {
    final normalizedId = videoId?.trim();
    if (normalizedId == null || normalizedId.isEmpty) return;
    _streamCache.remove(normalizedId);
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
    if (localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync()) {
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

  Future<AudioSource> buildPlaybackSourceForVideoId(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
    bool requirePrepared = false,
  }) async {
    final cacheFile = await cacheFileForVideoId(videoId);
    final markerFile = await cacheMarkerForVideoId(videoId);
    if (cacheFile.existsSync() &&
        markerFile.existsSync() &&
        cacheFile.lengthSync() > 100000) {
      return AudioSource.file(cacheFile.path, tag: tag);
    }
    final preparedSource =
        urlOverride == null ? freshStreamSource(videoId) : null;
    if (requirePrepared && urlOverride == null && preparedSource == null) {
      throw StateError('managed_source_not_prepared');
    }
    final resolvedUrl = urlOverride ??
        preparedSource?.url ??
        _proxyUri(
          '/playback/stream/${Uri.encodeComponent(videoId)}',
        ).toString();
    final resolvedHeaders =
        headers ?? preparedSource?.headers ?? const <String, String>{};
    return AudioSource.uri(
      Uri.parse(resolvedUrl),
      headers: resolvedHeaders.isEmpty ? null : resolvedHeaders,
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
    required bool background,
    required int lookahead,
  }) {
    return [
      activeQueue ? '1' : '0',
      background ? '1' : '0',
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
    bool background = true,
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
      background: background,
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
              'track_keys': idsNeedingWarm,
              'current_track_key': currentVideoId,
              'active_queue': activeQueue,
              'lookahead': lookahead,
              'background': background,
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
          markPreparedStream(key.toString(), payload: value);
          if (value is Map) {
            debugPrint(
              '[EBB] prepared ${key.toString()} '
              'source=${value['source_kind']} '
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
            'track_keys': [videoId],
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
        'source=${metrics['source_kind']} '
        'resolveMs=${metrics['resolve_ms']} '
        'chunkMs=${metrics['chunk_ms']} '
        'chunkBytes=${metrics['cached_prefix_bytes']} '
        'serverMs=${data['server_ms']}',
      );
    }
    debugPrint(
      '[EBB] prepare_session[$videoId] totalMs=${stopwatch.elapsedMilliseconds}',
    );
    return markPreparedStream(videoId, payload: metrics);
  }

  Future<ResolvedStreamSource> fetchDirectStreamSource(String videoId) async {
    final res = await appHttpClient
        .post(
          _proxyUri('/playback/resolve'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'track_key': videoId}),
        )
        .timeout(const Duration(seconds: 15));

    if (res.statusCode != 200) {
      throw Exception('Direct stream lookup failed: ${res.statusCode}');
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    final directUrl = data['url']?.toString();
    if (directUrl == null || directUrl.isEmpty) {
      throw Exception('Proxy returned an empty direct stream URL');
    }

    final playbackUrl = directUrl.startsWith('http://') ||
            directUrl.startsWith('https://')
        ? directUrl
        : _proxyUri(directUrl.startsWith('/') ? directUrl : '/$directUrl')
            .toString();
    final source = ResolvedStreamSource(
      url: playbackUrl,
      headers: _parseHeaders(data['headers']),
      fetchedAt: DateTime.now(),
      sourceKind: 'direct_url',
      expiresAt: _preparedExpiresAt(data),
    );
    _streamCache[videoId] = source;
    return source;
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
          background: false,
          lookahead: 1,
        );
        return;
      }
      await prepareQueueSession(
        [videoId],
        currentVideoId: videoId,
        activeQueue: false,
        background: true,
        lookahead: 1,
      );
    } catch (_) {
      // Prewarm should stay silent. Playback will surface real errors later.
    }
  }

  Future<void> prewarmStreams(
    Iterable<String?> videoIds, {
    int lookahead = 3,
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
      background: !immediatePlayback,
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
