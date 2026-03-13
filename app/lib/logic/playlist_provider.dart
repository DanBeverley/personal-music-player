import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';
import 'dart:convert';

class Playlist {
  final String id;
  final String name;
  final List<dynamic> tracks;

  Playlist({required this.id, required this.name, required this.tracks});

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'tracks': tracks,
      };

  factory Playlist.fromJson(Map<String, dynamic> json) {
    return Playlist(
      id: json['id'],
      name: json['name'],
      tracks: json['tracks'] ?? [],
    );
  }
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

  void createPlaylist(String name) {
    final newPlaylist = Playlist(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      name: name,
      tracks: [],
    );
    state = [...state, newPlaylist];
    _savePlaylists();
  }

  void renamePlaylist(String id, String newName) {
    state = state.map((p) {
      if (p.id == id) {
        return Playlist(id: p.id, name: newName, tracks: p.tracks);
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
    state = state.map((p) {
      if (p.id == playlistId) {
        // Prevent duplicates
        final exists = p.tracks.any((t) => t['id'] == (track['id'] ?? track['videoId']));
        if (!exists) {
          return Playlist(id: p.id, name: p.name, tracks: [...p.tracks, track]);
        }
      }
      return p;
    }).toList();
    _savePlaylists();
  }

  void removeTrackFromPlaylist(String playlistId, String trackId) {
    state = state.map((p) {
      if (p.id == playlistId) {
        final newTracks = p.tracks.where((t) => (t['id'] ?? t['videoId']) != trackId).toList();
        return Playlist(id: p.id, name: p.name, tracks: newTracks);
      }
      return p;
    }).toList();
    _savePlaylists();
  }
}

final playlistProvider = StateNotifierProvider<PlaylistNotifier, List<Playlist>>((ref) {
  return PlaylistNotifier();
});
