List<String> collectHomePrewarmTrackIds({
  required Iterable<dynamic> tracks,
  required Set<String> alreadyPrewarmed,
  int limit = 12,
}) {
  final idsToWarm = <String>[];
  for (final track in tracks.take(limit)) {
    if (track is! Map) continue;
    final id = (track['id'] ?? track['videoId'])?.toString();
    if (id == null || id.isEmpty) continue;
    if (alreadyPrewarmed.add(id)) {
      idsToWarm.add(id);
    }
  }
  return idsToWarm;
}

String homePrewarmSignature(Iterable<String> trackIds) {
  return trackIds.join('|');
}
