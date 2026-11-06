import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;

import 'audio_provider.dart';
import 'auth_provider.dart';

enum DownloadPhase { active, complete, failed }

class DownloadTask {
  final String id;
  final String title;
  final String? subtitle;
  final String? thumbnail;
  final double progress;
  final DownloadPhase phase;
  final String? error;
  final DateTime updatedAt;

  const DownloadTask({
    required this.id,
    required this.title,
    this.subtitle,
    this.thumbnail,
    this.progress = 0,
    this.phase = DownloadPhase.active,
    this.error,
    required this.updatedAt,
  });

  DownloadTask copyWith({
    String? title,
    String? subtitle,
    String? thumbnail,
    double? progress,
    DownloadPhase? phase,
    String? error,
    DateTime? updatedAt,
  }) {
    return DownloadTask(
      id: id,
      title: title ?? this.title,
      subtitle: subtitle ?? this.subtitle,
      thumbnail: thumbnail ?? this.thumbnail,
      progress: progress ?? this.progress,
      phase: phase ?? this.phase,
      error: error,
      updatedAt: updatedAt ?? this.updatedAt,
    );
  }

  bool get isActive => phase == DownloadPhase.active;
}

class DownloadCenterState {
  final List<DownloadTask> tasks;

  const DownloadCenterState({this.tasks = const []});

  DownloadCenterState copyWith({List<DownloadTask>? tasks}) {
    return DownloadCenterState(tasks: tasks ?? this.tasks);
  }

  List<DownloadTask> get activeTasks =>
      tasks.where((task) => task.phase == DownloadPhase.active).toList()
        ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));

  DownloadTask? taskFor(String id) {
    for (final task in tasks) {
      if (task.id == id) return task;
    }
    return null;
  }
}

int _parseDownloadInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

String _cleanFilename(String title) {
  return title
      .replaceAll(RegExp(r'[<>:"/\\|?*]'), '_')
      .trim()
      .replaceAll(RegExp(r'\s+'), ' ');
}

class DownloadCenterNotifier extends StateNotifier<DownloadCenterState> {
  final Ref ref;

  DownloadCenterNotifier(this.ref) : super(const DownloadCenterState());

  void resetForScopeChange() {
    state = const DownloadCenterState();
  }

  void _upsert(DownloadTask task) {
    final next = [
      task,
      ...state.tasks.where((existing) => existing.id != task.id),
    ];
    state = state.copyWith(tasks: next.take(6).toList());
  }

  void _remove(String id) {
    state = state.copyWith(
      tasks: state.tasks.where((task) => task.id != id).toList(),
    );
  }

  Future<void> clearFinished(String id) async {
    _remove(id);
  }

  Future<bool> downloadTrack(Map<String, dynamic> track) async {
    if (extractPlaybackProvider(track) != 'youtube') return false;
    final videoId = extractPlaybackSourceId(track);
    if (videoId == null || videoId.isEmpty) return false;

    final existing = state.taskFor(videoId);
    if (existing?.isActive == true) {
      return false;
    }

    final title = (track['title'] ?? 'Unknown Track').toString();
    final subtitle = (track['author'] ?? track['artist'] ?? track['channel'])?.toString();
    final thumbnail = track['thumbnail']?.toString();
    final task = DownloadTask(
      id: videoId,
      title: title,
      subtitle: subtitle,
      thumbnail: thumbnail,
      updatedAt: DateTime.now(),
    );
    _upsert(task);

    final dir = await getScopedDownloadsDirectory();
    final outPath = '${dir.path}/${_cleanFilename(title)}.mp3';

    try {
      final metaResponse = await http.post(
        buildProxyUri('/download'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({"video_id": videoId, "title": title}),
      );

      if (metaResponse.statusCode != 200) {
        throw Exception('Proxy refused download: ${metaResponse.body}');
      }

      final meta = jsonDecode(metaResponse.body) as Map<String, dynamic>;
      if (meta['thumbnail'] == null && thumbnail != null) {
        meta['thumbnail'] = thumbnail;
      }
      meta['owner_id'] = currentAuthenticatedUserId ?? 'guest';

      final client = http.Client();
      try {
        final streamResponse = await client.send(
          http.Request('GET', buildProxyUri('/downloaded/$videoId')),
        );

        if (streamResponse.statusCode != 200) {
          throw Exception('Stream error: ${streamResponse.statusCode}');
        }

        final totalBytes = streamResponse.contentLength ??
            _parseDownloadInt(meta['filesize']);
        final outputFile = File(outPath);
        final sink = outputFile.openWrite();
        var receivedBytes = 0;

        await for (final chunk in streamResponse.stream) {
          sink.add(chunk);
          receivedBytes += chunk.length;
          if (totalBytes > 0) {
            _upsert(task.copyWith(
              progress: (receivedBytes / totalBytes).clamp(0, 1).toDouble(),
              updatedAt: DateTime.now(),
            ));
          }
        }
        await sink.close();
      } finally {
        client.close();
      }

      final savedFile = File(outPath);
      if (!savedFile.existsSync() || savedFile.lengthSync() < 10000) {
        throw Exception('Downloaded file is empty or corrupted');
      }

      final jsonPath = outPath.replaceAll('.mp3', '.json');
      await File(jsonPath).writeAsString(jsonEncode(meta));
      await upsertCloudLibraryTrack(
        {
          ...meta,
          'id': videoId,
          'videoId': videoId,
          if (meta['title'] == null) 'title': title,
          if (meta['author'] == null && subtitle != null) 'author': subtitle,
          if (meta['thumbnail'] == null && thumbnail != null)
            'thumbnail': thumbnail,
        },
        persistLocalLike: false,
      );
      ref.invalidate(libraryProvider);

      _upsert(task.copyWith(
        progress: 1,
        phase: DownloadPhase.complete,
        updatedAt: DateTime.now(),
      ));

      unawaited(Future<void>.delayed(const Duration(seconds: 4), () {
        if (mounted && state.taskFor(videoId)?.phase == DownloadPhase.complete) {
          _remove(videoId);
        }
      }));

      return true;
    } catch (e) {
      debugPrint('Download failed for $videoId: $e');
      try {
        final outputFile = File(outPath);
        if (outputFile.existsSync()) {
          outputFile.deleteSync();
        }
        final jsonFile = File(outPath.replaceAll('.mp3', '.json'));
        if (jsonFile.existsSync()) {
          jsonFile.deleteSync();
        }
      } catch (_) {}

      _upsert(task.copyWith(
        phase: DownloadPhase.failed,
        error: e.toString(),
        updatedAt: DateTime.now(),
      ));
      return false;
    }
  }

  Future<void> downloadTracks(Iterable<Map<String, dynamic>> tracks) async {
    for (final track in tracks) {
      await downloadTrack(track);
    }
  }
}

final downloadCenterProvider =
    StateNotifierProvider<DownloadCenterNotifier, DownloadCenterState>((ref) {
  final notifier = DownloadCenterNotifier(ref);
  ref.listen<String>(
    authProvider.select((state) => state.storageScopeId),
    (_, __) => notifier.resetForScopeChange(),
  );
  return notifier;
});

final downloadTaskProvider =
    Provider.family<DownloadTask?, String>((ref, videoId) {
  return ref.watch(downloadCenterProvider.select((state) => state.taskFor(videoId)));
});
