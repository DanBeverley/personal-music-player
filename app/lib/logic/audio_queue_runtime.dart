// ignore_for_file: experimental_member_use

import 'package:just_audio/just_audio.dart';

import 'track_metadata.dart';

enum PlaybackStallRecoveryAction { wait, softSeek, refreshSource }

PlaybackStallRecoveryAction playbackStallRecoveryAction({
  required Duration stalledFor,
  required bool waitingForNetwork,
  required int priorRecoveryAttempts,
}) {
  final threshold = waitingForNetwork
      ? const Duration(seconds: 18)
      : const Duration(seconds: 10);
  if (stalledFor < threshold) return PlaybackStallRecoveryAction.wait;
  if (priorRecoveryAttempts > 0) {
    return PlaybackStallRecoveryAction.wait;
  }
  if (!waitingForNetwork) {
    return PlaybackStallRecoveryAction.softSeek;
  }
  return PlaybackStallRecoveryAction.refreshSource;
}

int trackIndexByIdentity(
  List<Map<String, dynamic>> tracks,
  String? trackId,
) {
  final normalizedId = trackId?.trim();
  if (normalizedId == null || normalizedId.isEmpty) return -1;
  return tracks.indexWhere((track) => extractTrackId(track) == normalizedId);
}

String? trackIdentityAt(
  List<Map<String, dynamic>> tracks,
  int? index,
) {
  if (index == null || index < 0 || index >= tracks.length) return null;
  return extractTrackId(tracks[index]);
}

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
  required Future<AudioSource> Function(Map<String, dynamic> track)
      buildManagedAudioSource,
  required void Function(String? videoId, [String? reason])
      registerUnavailableTrack,
}) async {
  final playableTracks = <Map<String, dynamic>>[];
  final sources = <AudioSource>[];
  var resolvedInitialIndex = preferredInitialIndex;

  for (var i = 0; i < tracks.length; i++) {
    final track = tracks[i];
    final videoId = extractTrackId(track);
    try {
      final source = await buildManagedAudioSource(track);
      playableTracks.add(track);
      sources.add(source);
    } catch (error) {
      final reason = error.toString();
      registerUnavailableTrack(videoId, reason);
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
