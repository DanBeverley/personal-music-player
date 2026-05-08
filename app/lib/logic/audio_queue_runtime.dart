// ignore_for_file: experimental_member_use

import 'dart:io';

import 'package:just_audio/just_audio.dart';

import 'track_metadata.dart';

class ManagedQueueResolution {
  final List<Map<String, dynamic>> tracks;
  final List<AudioSource> sources;
  final int initialIndex;

  const ManagedQueueResolution({
    required this.tracks,
    required this.sources,
    required this.initialIndex,
  });
}

Future<ManagedQueueResolution> resolveManagedQueueSources({
  required List<Map<String, dynamic>> tracks,
  required int preferredInitialIndex,
  required bool Function(Map<String, dynamic> track) isKnownUnavailableRemoteTrack,
  required Future<AudioSource> Function(Map<String, dynamic> track)
      buildManagedAudioSource,
  required void Function(String? videoId, [String? reason]) registerUnavailableTrack,
}) async {
  final playableTracks = <Map<String, dynamic>>[];
  final sources = <AudioSource>[];
  var resolvedInitialIndex = preferredInitialIndex;

  for (var i = 0; i < tracks.length; i++) {
    final track = tracks[i];
    final videoId = extractTrackId(track);
    if (isKnownUnavailableRemoteTrack(track)) {
      if (i < resolvedInitialIndex) {
        resolvedInitialIndex -= 1;
      }
      continue;
    }
    try {
      final source = await buildManagedAudioSource(track);
      playableTracks.add(track);
      sources.add(source);
    } catch (error) {
      registerUnavailableTrack(videoId, error.toString());
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

  return ManagedQueueResolution(
    tracks: playableTracks,
    sources: sources,
    initialIndex: resolvedInitialIndex,
  );
}

Future<void> prefetchManagedQueueAhead({
  required bool managedQueueActive,
  required List<Map<String, dynamic>> managedQueueTracks,
  required int? currentManagedQueueIndex,
  required int lookahead,
  required List<AudioPlayer> prefetchPlayers,
  required List<String?> prefetchedTrackIds,
  required Future<LockCachingAudioSource> Function(
    String videoId, {
    Map<String, String>? headers,
    String? urlOverride,
    dynamic tag,
  }) buildCachingSourceForVideoId,
}) async {
  if (!managedQueueActive || managedQueueTracks.isEmpty) return;
  final currentIndex = currentManagedQueueIndex ?? 0;
  final targets = <Map<String, dynamic>>[];
  for (var i = currentIndex + 1; i < managedQueueTracks.length; i++) {
    final track = managedQueueTracks[i];
    if (isTrackHidden(track)) continue;
    targets.add(track);
    if (targets.length >= lookahead) break;
  }

  for (var slot = 0; slot < prefetchPlayers.length; slot++) {
    final track = slot < targets.length ? targets[slot] : null;
    final trackId = track == null ? null : extractTrackId(track);
    if (prefetchedTrackIds[slot] == trackId) {
      continue;
    }

    if (track == null || trackId == null || trackId.isEmpty) {
      prefetchedTrackIds[slot] = null;
      try {
        await prefetchPlayers[slot].stop();
      } catch (_) {}
      continue;
    }

    final localPath = track['local_path']?.toString();
    if (localPath != null &&
        localPath.isNotEmpty &&
        File(localPath).existsSync()) {
      prefetchedTrackIds[slot] = trackId;
      continue;
    }

    try {
      final source = await buildCachingSourceForVideoId(trackId, tag: track);
      prefetchedTrackIds[slot] = trackId;
      await prefetchPlayers[slot].stop();
      await prefetchPlayers[slot].setAudioSource(source, preload: true);
    } catch (_) {
      prefetchedTrackIds[slot] = null;
    }
  }
}
