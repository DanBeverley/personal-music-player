import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';
import 'dart:convert';

class Playlist {
  final String id;
  final String name;
  final List<dynamic> tracks;
  final int? coverColorValue;
  final String? coverImagePath;

  Playlist({
    required this.id,
    required this.name,
    required this.tracks,
    this.coverColorValue,
    this.coverImagePath,
  });

  Playlist copyWith({
    String? name,
    List<dynamic>? tracks,
    int? coverColorValue,
    String? coverImagePath,
    bool clearCoverImage = false,
  }) {
    return Playlist(
      id: id,
      name: name ?? this.name,
      tracks: tracks ?? this.tracks,
      coverColorValue: coverColorValue ?? this.coverColorValue,
      coverImagePath: clearCoverImage ? null : coverImagePath ?? this.coverImagePath,
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'tracks': tracks,
        'cover_color_value': coverColorValue,
        'cover_image_path': coverImagePath,
      };

  factory Playlist.fromJson(Map<String, dynamic> json) {
    return Playlist(
      id: json['id'],
      name: json['name'],
      tracks: json['tracks'] ?? [],
      coverColorValue: json['cover_color_value'],
      coverImagePath: json['cover_image_path'],
    );
  }
}

const List<int> _playlistCoverDefaults = <int>[
  0xFF4C5363,
  0xFF3F4A5A,
  0xFF5B4F4B,
  0xFF37424A,
  0xFF514959,
  0xFF5A5A5A,
];

int _defaultPlaylistCoverForName(String seed) {
  final index = seed.hashCode.abs() % _playlistCoverDefaults.length;
  return _playlistCoverDefaults[index];
}

Map<String, dynamic> _normalizePlaylistTrack(dynamic rawTrack) {
  final track = Map<String, dynamic>.from(rawTrack as Map);
  final id = (track['id'] ?? track['videoId'])?.toString();
  return {
    ...track,
    if (id != null) 'id': id,
    if (id != null && track['videoId'] == null) 'videoId': id,
    if (track['channel'] == null && track['author'] != null)
      'channel': track['author'],
    if (track['author'] == null && track['channel'] != null)
      'author': track['channel'],
    'isHidden': track['isHidden'] == true,
  };
}

class PlaylistNotifier extends StateNotifier<List<Playlist>> {
  PlaylistNotifier() : super([]) {
    _loadPlaylists();
  }

  Future<File> get _file async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/playlists.json');
  }

  Future<void> _loadPlaylists() async {
    try {
      final f = await _file;
      if (f.existsSync()) {
        final data = jsonDecode(await f.readAsString()) as List;
        state = data.map((e) => Playlist.fromJson(e)).toList();
      }
    } catch (e) {
      // Ignored
    }
  }

  Future<void> _savePlaylists() async {
    try {
      final f = await _file;
      await f.writeAsString(jsonEncode(state.map((e) => e.toJson()).toList()));
    } catch (e) {
      // Ignored
    }
  }

  Playlist createPlaylist(String name) {
    final newPlaylist = Playlist(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      name: name,
      tracks: [],
      coverColorValue: _defaultPlaylistCoverForName(name),
    );
    state = [...state, newPlaylist];
    _savePlaylists();
    return newPlaylist;
  }

  void renamePlaylist(String id, String newName) {
    state = state.map((p) {
      if (p.id == id) {
        return p.copyWith(
          name: newName,
          coverColorValue:
              p.coverColorValue ?? _defaultPlaylistCoverForName(newName),
        );
      }
      return p;
    }).toList();
    _savePlaylists();
  }

  void deletePlaylist(String id) {
    state = state.where((p) => p.id != id).toList();
    _savePlaylists();
  }

  void addTrackToPlaylist(String playlistId, dynamic track) {
    final normalizedTrack = _normalizePlaylistTrack(track);
    state = state.map((p) {
      if (p.id == playlistId) {
        // Prevent duplicates
        final exists = p.tracks.any(
          (t) => t['id'] == (normalizedTrack['id'] ?? normalizedTrack['videoId']),
        );
        if (!exists) {
          return p.copyWith(tracks: [...p.tracks, normalizedTrack]);
        }
      }
      return p;
    }).toList();
    _savePlaylists();
  }

  void removeTrackFromPlaylist(String playlistId, String trackId) {
    state = state.map((p) {
      if (p.id == playlistId) {
        final newTracks = p.tracks
            .where((t) => (t['id'] ?? t['videoId']) != trackId)
            .toList();
        return p.copyWith(tracks: newTracks);
      }
      return p;
    }).toList();
    _savePlaylists();
  }

  void setTrackHidden(String playlistId, String trackId, bool isHidden) {
    state = state.map((p) {
      if (p.id != playlistId) return p;
      final nextTracks = p.tracks.map((rawTrack) {
        final track = Map<String, dynamic>.from(rawTrack as Map);
        final id = (track['id'] ?? track['videoId'])?.toString();
        if (id != trackId) return track;
        return {
          ...track,
          'isHidden': isHidden,
        };
      }).toList(growable: false);
      return p.copyWith(tracks: nextTracks);
    }).toList();
    _savePlaylists();
  }

  void replaceTracks(String playlistId, List<dynamic> tracks) {
    final normalizedTracks =
        tracks.map(_normalizePlaylistTrack).toList(growable: false);
    state = state.map((p) {
      if (p.id != playlistId) return p;
      return p.copyWith(tracks: normalizedTracks);
    }).toList();
    _savePlaylists();
  }

  void updatePlaylistArtwork(
    String playlistId, {
    int? coverColorValue,
    String? coverImagePath,
    bool clearCoverImage = false,
  }) {
    state = state.map((p) {
      if (p.id != playlistId) return p;
      return p.copyWith(
        coverColorValue: coverColorValue,
        coverImagePath: coverImagePath,
        clearCoverImage: clearCoverImage,
      );
    }).toList();
    _savePlaylists();
  }
}

final playlistProvider = StateNotifierProvider<PlaylistNotifier, List<Playlist>>((ref) {
  return PlaylistNotifier();
});
