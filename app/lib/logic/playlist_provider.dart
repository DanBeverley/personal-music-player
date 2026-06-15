import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';
import 'interaction_events.dart';

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
      coverImagePath:
          clearCoverImage ? null : coverImagePath ?? this.coverImagePath,
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
      tracks: json['tracks'] ?? const [],
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
  final Ref ref;
  String _scopeId = 'guest';
  Future<void> _persistQueue = Future<void>.value();

  PlaylistNotifier(this.ref) : super(const []) {
    unawaited(reloadForCurrentScope());
  }

  Future<List<Playlist>> _loadLocalPlaylists(String scopeId) async {
    try {
      final file = await getScopedDataFile('playlists.json', scopeId);
      if (!file.existsSync()) return const [];
      final raw = jsonDecode(await file.readAsString()) as List<dynamic>;
      return raw
          .map((entry) => Playlist.fromJson(
              Map<String, dynamic>.from(entry as Map<dynamic, dynamic>)))
          .toList(growable: false);
    } catch (error) {
      debugPrint('Playlist local load failed: $error');
      return const [];
    }
  }

  Future<void> _saveLocalPlaylists(
    List<Playlist> playlists, [
    String? scopeId,
  ]) async {
    try {
      final file = await getScopedDataFile('playlists.json', scopeId ?? _scopeId);
      await file.writeAsString(
        jsonEncode(playlists.map((playlist) => playlist.toJson()).toList()),
      );
    } catch (error) {
      debugPrint('Playlist local save failed: $error');
    }
  }

  Future<List<Playlist>> _loadCloudPlaylists(String userId) async {
    final client = supabaseClientOrNull;
    if (client == null) return const [];

    final playlistRows = await client
        .from('playlists')
        .select('id,name,cover_color_value,cover_image_path')
        .eq('user_id', userId)
        .order('created_at');

    final trackRows = await client
        .from('playlist_tracks')
        .select('playlist_id,position,track_data')
        .eq('user_id', userId)
        .order('position');

    final tracksByPlaylist = <String, List<dynamic>>{};
    for (final rawTrack in trackRows as List<dynamic>) {
      final row = Map<String, dynamic>.from(rawTrack as Map);
      final playlistId = row['playlist_id']?.toString();
      if (playlistId == null || playlistId.isEmpty) continue;
      tracksByPlaylist.putIfAbsent(playlistId, () => <dynamic>[]);
      tracksByPlaylist[playlistId]!.add(
        Map<String, dynamic>.from(row['track_data'] as Map),
      );
    }

    return (playlistRows as List<dynamic>).map((rawPlaylist) {
      final row = Map<String, dynamic>.from(rawPlaylist as Map);
      final playlistId = row['id'].toString();
      return Playlist(
        id: playlistId,
        name: row['name']?.toString() ?? 'Untitled Playlist',
        tracks: tracksByPlaylist[playlistId] ?? const [],
        coverColorValue: row['cover_color_value'],
        coverImagePath: row['cover_image_path']?.toString(),
      );
    }).toList(growable: false);
  }

  Future<void> _syncAllPlaylistsToCloud(
    String userId,
    List<Playlist> playlists,
  ) async {
    final client = supabaseClientOrNull;
    if (client == null) return;

    final remoteRows = await client
        .from('playlists')
        .select('id')
        .eq('user_id', userId);
    final remoteIds = (remoteRows as List<dynamic>)
        .map((row) => row['id'].toString())
        .toSet();
    final localIds = playlists.map((playlist) => playlist.id).toSet();

    for (final staleId in remoteIds.difference(localIds)) {
      await client.from('playlist_tracks').delete().eq('playlist_id', staleId);
      await client.from('playlists').delete().eq('id', staleId);
    }

    if (playlists.isNotEmpty) {
      await client.from('playlists').upsert(
            playlists
                .map((playlist) => {
                      'id': playlist.id,
                      'user_id': userId,
                      'name': playlist.name,
                      'cover_color_value': playlist.coverColorValue,
                      'cover_image_path': _cloudSafeCoverPath(
                        playlist.coverImagePath,
                      ),
                    })
                .toList(growable: false),
          );
    }

    await client.from('playlist_tracks').delete().eq('user_id', userId);

    final nextTrackRows = <Map<String, dynamic>>[];
    for (final playlist in playlists) {
      for (var i = 0; i < playlist.tracks.length; i++) {
        nextTrackRows.add({
          'user_id': userId,
          'playlist_id': playlist.id,
          'position': i,
          'track_id': (playlist.tracks[i]['id'] ?? playlist.tracks[i]['videoId'])
              ?.toString(),
          'track_data': _normalizePlaylistTrack(playlist.tracks[i]),
        });
      }
    }

    if (nextTrackRows.isNotEmpty) {
      await client.from('playlist_tracks').insert(nextTrackRows);
    }
  }

  String? _cloudSafeCoverPath(String? coverImagePath) {
    if (coverImagePath == null || coverImagePath.isEmpty) return null;
    if (coverImagePath.startsWith('http://') ||
        coverImagePath.startsWith('https://')) {
      return coverImagePath;
    }
    return null;
  }

  Future<void> reloadForCurrentScope() async {
    final authState = ref.read(authProvider);
    final nextScopeId = authState.storageScopeId;
    _scopeId = nextScopeId;

    final localPlaylists = await _loadLocalPlaylists(nextScopeId);
    if (!authState.isAuthenticated || !isSupabaseConfigured) {
      state = localPlaylists;
      return;
    }

    try {
      final userId = authState.user!.id;
      final remotePlaylists = await _loadCloudPlaylists(userId);
      if (remotePlaylists.isEmpty && localPlaylists.isNotEmpty) {
        await _syncAllPlaylistsToCloud(userId, localPlaylists);
        state = localPlaylists;
        return;
      }
      state = remotePlaylists;
      await _saveLocalPlaylists(remotePlaylists, nextScopeId);
    } catch (error) {
      debugPrint('Playlist cloud reload failed: $error');
      state = localPlaylists;
    }
  }

  void _persistState() {
    final snapshot = [...state];
    final scopeId = _scopeId;
    final authState = ref.read(authProvider);
    _persistQueue = _persistQueue.catchError((_) {}).then((_) async {
      await _saveLocalPlaylists(snapshot, scopeId);
      if (authState.isAuthenticated) {
        try {
          await _syncAllPlaylistsToCloud(authState.user!.id, snapshot);
        } catch (error) {
          debugPrint('Playlist cloud sync failed: $error');
        }
      }
    });
    unawaited(_persistQueue);
  }

  Playlist createPlaylist(String name) {
    final newPlaylist = Playlist(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      name: name,
      tracks: const [],
      coverColorValue: _defaultPlaylistCoverForName(name),
    );
    state = [...state, newPlaylist];
    _persistState();
    return newPlaylist;
  }

  void renamePlaylist(String id, String newName) {
    state = state.map((playlist) {
      if (playlist.id != id) return playlist;
      return playlist.copyWith(
        name: newName,
        coverColorValue:
            playlist.coverColorValue ?? _defaultPlaylistCoverForName(newName),
      );
    }).toList(growable: false);
    _persistState();
  }

  void deletePlaylist(String id) {
    state = state.where((playlist) => playlist.id != id).toList(growable: false);
    _persistState();
  }

  void addTrackToPlaylist(String playlistId, dynamic track) {
    final normalizedTrack = _normalizePlaylistTrack(track);
    final targetPlaylist = state.where((playlist) => playlist.id == playlistId);
    final playlistName =
        targetPlaylist.isEmpty ? '' : targetPlaylist.first.name.trim();
    final trackId =
        (normalizedTrack['id'] ?? normalizedTrack['videoId'])?.toString();
    final isNewAddition = targetPlaylist.isNotEmpty &&
        !targetPlaylist.first.tracks.any(
          (entry) =>
              (entry['id'] ?? entry['videoId']) ==
              (normalizedTrack['id'] ?? normalizedTrack['videoId']),
        );
    state = state.map((playlist) {
      if (playlist.id != playlistId) return playlist;
      final exists = playlist.tracks.any(
        (entry) => entry['id'] == (normalizedTrack['id'] ?? normalizedTrack['videoId']),
      );
      if (exists) return playlist;
      return playlist.copyWith(tracks: [...playlist.tracks, normalizedTrack]);
    }).toList(growable: false);
    _persistState();
    if (isNewAddition && trackId != null && trackId.isNotEmpty) {
      unawaited(
        recordProxyInteractionEvent(
          'playlist_add',
          trackId: trackId,
          rawTrack: normalizedTrack,
          metadata: {
            'playlist_id': playlistId,
            if (playlistName.isNotEmpty) 'playlist_name': playlistName,
          },
        ),
      );
    }
  }

  void removeTrackFromPlaylist(String playlistId, String trackId) {
    state = state.map((playlist) {
      if (playlist.id != playlistId) return playlist;
      return playlist.copyWith(
        tracks: playlist.tracks
            .where((track) => (track['id'] ?? track['videoId']) != trackId)
            .toList(growable: false),
      );
    }).toList(growable: false);
    _persistState();
  }

  void setTrackHidden(String playlistId, String trackId, bool isHidden) {
    state = state.map((playlist) {
      if (playlist.id != playlistId) return playlist;
      final nextTracks = playlist.tracks.map((rawTrack) {
        final track = Map<String, dynamic>.from(rawTrack as Map);
        final id = (track['id'] ?? track['videoId'])?.toString();
        if (id != trackId) return track;
        return {
          ...track,
          'isHidden': isHidden,
        };
      }).toList(growable: false);
      return playlist.copyWith(tracks: nextTracks);
    }).toList(growable: false);
    _persistState();
  }

  void replaceTracks(String playlistId, List<dynamic> tracks) {
    final normalizedTracks =
        tracks.map(_normalizePlaylistTrack).toList(growable: false);
    state = state.map((playlist) {
      if (playlist.id != playlistId) return playlist;
      return playlist.copyWith(tracks: normalizedTracks);
    }).toList(growable: false);
    _persistState();
  }

  void updatePlaylistArtwork(
    String playlistId, {
    int? coverColorValue,
    String? coverImagePath,
    bool clearCoverImage = false,
  }) {
    state = state.map((playlist) {
      if (playlist.id != playlistId) return playlist;
      return playlist.copyWith(
        coverColorValue: coverColorValue,
        coverImagePath: coverImagePath,
        clearCoverImage: clearCoverImage,
      );
    }).toList(growable: false);
    _persistState();
  }
}

final playlistProvider =
    StateNotifierProvider<PlaylistNotifier, List<Playlist>>((ref) {
  final notifier = PlaylistNotifier(ref);
  ref.listen<String>(
    authProvider.select((state) => state.storageScopeId),
    (_, __) {
      unawaited(notifier.reloadForCurrentScope());
    },
  );
  ref.listen<int>(
    storageRefreshTickProvider,
    (_, __) {
      unawaited(notifier.reloadForCurrentScope());
    },
  );
  return notifier;
});
