int _parseTrackInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

String _cleanTrackString(dynamic value) {
  return value?.toString().trim() ?? '';
}

String _extractPrimaryArtist(dynamic rawTrack) {
  if (rawTrack is! Map) return '';
  final artists = rawTrack['artists'];
  if (artists is! List) return '';
  for (final entry in artists) {
    if (entry is Map) {
      final name = _cleanTrackString(
        entry['name'] ?? entry['artist'] ?? entry['title'],
      );
      if (name.isNotEmpty) return name;
    } else {
      final name = _cleanTrackString(entry);
      if (name.isNotEmpty) return name;
    }
  }
  return '';
}

String? extractTrackId(dynamic track) {
  if (track is! Map) return null;
  final value = track['id'] ?? track['videoId'] ?? track['video_id'];
  final id = value?.toString();
  if (id == null || id.isEmpty) return null;
  return id;
}

List<String> extractTrackArtists(dynamic rawTrack) {
  if (rawTrack is! Map) return const <String>[];
  final results = <String>[];
  final seen = <String>{};

  void addArtist(String? rawValue) {
    final value = _cleanTrackString(rawValue);
    final normalized = value.toLowerCase();
    if (value.isEmpty ||
        normalized == 'unknown artist' ||
        !seen.add(normalized)) {
      return;
    }
    results.add(value);
  }

  final artists = rawTrack['artists'];
  if (artists is List) {
    for (final entry in artists) {
      if (entry is Map) {
        addArtist(
          entry['name']?.toString() ??
              entry['artist']?.toString() ??
              entry['title']?.toString(),
        );
      } else {
        addArtist(entry?.toString());
      }
    }
  }

  addArtist(
    rawTrack['channel']?.toString() ??
        rawTrack['author']?.toString() ??
        rawTrack['artist']?.toString(),
  );

  return results;
}

Map<String, dynamic> normalizeTrack(dynamic rawTrack) {
  final track = Map<String, dynamic>.from(rawTrack as Map);
  final id = extractTrackId(track);
  final title = _cleanTrackString(
    track['title'] ??
        track['name'] ??
        track['track'] ??
        track['song'] ??
        track['video_title'],
  );
  final primaryArtist = _extractPrimaryArtist(track);
  final channel = _cleanTrackString(
    track['channel'] ??
        track['author'] ??
        track['artist'] ??
        primaryArtist ??
        track['uploader'],
  );
  final album = _cleanTrackString(track['album'] ?? track['album_title']);
  return {
    ...track,
    if (id != null) 'id': id,
    if (id != null && track['videoId'] == null) 'videoId': id,
    'title': title.isNotEmpty ? title : 'Unknown Track',
    'channel': channel,
    'author': channel,
    'artist': _cleanTrackString(track['artist']).isNotEmpty
        ? _cleanTrackString(track['artist'])
        : channel,
    if (album.isNotEmpty) 'album': album,
    if (album.isNotEmpty) 'album_title': album,
    'duration': _parseTrackInt(track['duration']),
  };
}

bool isTrackMetadataIncomplete(dynamic rawTrack) {
  if (rawTrack is! Map) return true;
  final title = _cleanTrackString(
    rawTrack['title'] ??
        rawTrack['name'] ??
        rawTrack['track'] ??
        rawTrack['song'],
  ).toLowerCase();
  final artist = _cleanTrackString(
    rawTrack['channel'] ?? rawTrack['author'] ?? rawTrack['artist'],
  ).toLowerCase();
  return title.isEmpty ||
      title == 'unknown track' ||
      artist.isEmpty ||
      artist == 'unknown artist';
}

bool isTrackHidden(dynamic track) {
  if (track is! Map) return false;
  return track['isHidden'] == true;
}

Map<String, dynamic> copyTrackWithHidden(
  Map<String, dynamic> track,
  bool isHidden,
) {
  return {
    ...track,
    'isHidden': isHidden,
  };
}
