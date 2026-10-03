import '../logic/track_metadata.dart';

/// Builds bounded Home evidence from the active account's library and the
/// current track only after playback has advanced beyond its start.
Map<String, dynamic> buildWebHomeSignals({
  required Iterable<Map<String, dynamic>> likedTracks,
  required Iterable<Map<String, dynamic>> playlists,
  required bool libraryKnown,
  bool allowColdStart = false,
  Map<String, dynamic>? currentTrack,
  double currentPosition = 0,
  Iterable<Map<String, dynamic>> playedSnapshots = const [],
}) {
  final boundedSnapshots = playedSnapshots.toList().reversed.take(32).toList(growable: false);
  final frequent = boundedSnapshots.where((item) => (item['play_count'] as num? ?? 1) >= 2).toList()
    ..sort((a, b) => (b['play_count'] as num).compareTo(a['play_count'] as num));
  final libraryTrackIds = <String>{};
  final artistHints = <String>{};
  final playlistNames = <String>[];

  void addLibraryTrack(Map<String, dynamic> track) {
    final id = extractTrackId(track);
    if (id != null) libraryTrackIds.add(id);
    artistHints.addAll(extractTrackArtists(track));
  }

  for (final track in likedTracks) {
    addLibraryTrack(track);
  }
  for (final playlist in playlists) {
    final name = playlist['name']?.toString().trim() ?? '';
    if (name.isNotEmpty && playlistNames.length < 10) playlistNames.add(name);
    final rawTracks = playlist['tracks'];
    if (rawTracks is List) {
      for (final rawTrack in rawTracks.whereType<Map>()) {
        addLibraryTrack(Map<String, dynamic>.from(rawTrack));
      }
    }
  }

  Map<String, dynamic>? playedSnapshot;
  String? playedId;
  if (currentPosition > 0 && currentTrack != null) {
    playedId = extractTrackId(currentTrack);
    if (playedId != null) {
      final normalized = normalizeTrack(currentTrack);
      playedSnapshot = {
        'id': playedId,
        'track_key': playedId,
        'title': normalized['title'],
        'artist': normalized['artist'],
        'channel': normalized['channel'],
        'album': normalized['album'],
        'thumbnail': normalized['thumbnail'],
        'duration': trackDurationSeconds(normalized),
      };
    }
  }

  final hasSignals = libraryTrackIds.isNotEmpty ||
      artistHints.isNotEmpty ||
      playlistNames.isNotEmpty ||
      playedId != null || boundedSnapshots.isNotEmpty;
  return {
    if (hasSignals)
      'client_signal_tier': 'personalized'
    else if (libraryKnown && allowColdStart) ...{
      'client_signal_tier': 'cold_start',
      'fresh_account_empty_home': true,
    },
    if (libraryTrackIds.isNotEmpty)
      'library_track_ids': libraryTrackIds.take(28).toList(growable: false),
    if (artistHints.isNotEmpty)
      'artist_hints': artistHints.take(6).toList(growable: false),
    if (playlistNames.isNotEmpty) 'playlist_names': playlistNames,
    if (frequent.isNotEmpty) ...{
      'top_track_ids': frequent.map(extractTrackId).whereType<String>().take(16).toList(),
      'top_track_snapshots': frequent.take(16).toList(),
    },
    if (playedId != null || boundedSnapshots.isNotEmpty)
      'recent_track_ids': <String>[
        if (playedId != null) playedId,
        ...boundedSnapshots.map(extractTrackId).whereType<String>(),
      ].toSet().take(32).toList(growable: false),
    if (playedSnapshot != null || boundedSnapshots.isNotEmpty) ...{
      'recent_track_snapshots': <Map<String, dynamic>>[
        if (playedSnapshot != null) playedSnapshot,
        ...boundedSnapshots,
      ].take(32).toList(growable: false),
      'last_played_tracks': <Map<String, dynamic>>[
        if (playedSnapshot != null) playedSnapshot,
        ...boundedSnapshots,
      ].take(32).toList(growable: false),
    },
  };
}
