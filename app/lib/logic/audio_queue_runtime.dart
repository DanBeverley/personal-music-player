// ignore_for_file: experimental_member_use

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
      final reason = error.toString();
      if (!reason.contains('managed_source_not_prepared')) {
        registerUnavailableTrack(videoId, reason);
      }
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
