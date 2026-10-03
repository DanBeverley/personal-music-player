import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import '../logic/track_metadata.dart';

/// Account-scoped cloud library state for the browser client.
///
/// This class deliberately works only on individual records. In particular,
/// playlist changes never use the native client's replace-all sync strategy.
class WebLibrary extends ChangeNotifier {
  WebLibrary(
    this._client,
    this._userId, {
    bool playlistWritesEnabled = false,
  }) : _playlistWritesEnabled = playlistWritesEnabled;

  final SupabaseClient _client;
  final String _userId;
  final bool _playlistWritesEnabled;
  final Random _random = Random.secure();

  List<Map<String, dynamic>> _likes = const [];
  List<Map<String, dynamic>> _playlists = const [];
  bool _loading = false;
  String? _error;
  bool _disposed = false;
  int _refreshGeneration = 0;
  Future<void> _writeQueue = Future<void>.value();

  List<Map<String, dynamic>> get likes => List.unmodifiable(_likes);
  List<Map<String, dynamic>> get playlists => List.unmodifiable(_playlists);
  bool get playlistWritesEnabled => _playlistWritesEnabled;
  bool get loading => _loading;
  String? get error => _error;

  @override
  void dispose() {
    _disposed = true;
    _refreshGeneration++;
    super.dispose();
  }

  Future<void> refresh() async {
    final generation = ++_refreshGeneration;
    if (!_isCurrent(generation)) return;
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      final likedRows = await _client
          .from('library_tracks')
          .select('track_id,track_data,added_at')
          .eq('user_id', _userId)
          .order('added_at', ascending: false);
      final playlistRows = await _client
          .from('playlists')
          .select('id,name,created_at')
          .eq('user_id', _userId)
          .order('created_at');
      final playlistTrackRows = await _client
          .from('playlist_tracks')
          .select('playlist_id,position,track_id,track_data')
          .eq('user_id', _userId)
          .order('position');

      final nextLikes = _decodeLikes(likedRows);
      final nextPlaylists = _decodePlaylists(playlistRows, playlistTrackRows);
      if (!_isCurrent(generation)) return;
      _likes = nextLikes;
      _playlists = nextPlaylists;
      _loading = false;
      notifyListeners();
    } catch (error) {
      if (!_isCurrent(generation)) return;
      _loading = false;
      _error = _messageFor(error);
      notifyListeners();
    }
  }

  Future<void> setLiked(Map<String, dynamic> track, bool liked) {
    return _enqueueWrite(() async {
      final normalized = normalizeTrack(track);
      final trackId = extractTrackId(normalized)?.trim();
      if (trackId == null || trackId.isEmpty) {
        throw ArgumentError.value(track, 'track', 'A track id is required.');
      }
      if (liked) {
        await _client.from('library_tracks').upsert(
          {
            'user_id': _userId,
            'track_id': trackId,
            'track_data': normalized,
            'updated_at': DateTime.now().toUtc().toIso8601String(),
          },
          onConflict: 'user_id,track_id',
        );
      } else {
        await _client
            .from('library_tracks')
            .delete()
            .eq('user_id', _userId)
            .eq('track_id', trackId);
      }
      await refresh();
    });
  }

  Future<void> createPlaylist(String name) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      final normalizedName = _validateName(name);
      await _client.from('playlists').insert({
        'id': _newPlaylistId(),
        'user_id': _userId,
        'name': normalizedName,
      });
      await refresh();
    });
  }

  Future<void> renamePlaylist(String id, String name) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      final playlistId = _requireId(id, 'playlist id');
      await _client
          .from('playlists')
          .update({'name': _validateName(name)})
          .eq('user_id', _userId)
          .eq('id', playlistId);
      await refresh();
    });
  }

  Future<void> deletePlaylist(String id) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      final playlistId = _requireId(id, 'playlist id');
      // The schema's FK cascade removes only this playlist's rows.
      await _client
          .from('playlists')
          .delete()
          .eq('user_id', _userId)
          .eq('id', playlistId);
      await refresh();
    });
  }

  Future<void> addTrack(String id, Map<String, dynamic> track) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      final playlistId = _requireId(id, 'playlist id');
      final normalized = normalizeTrack(track);
      final trackId = extractTrackId(normalized)?.trim();
      if (trackId == null || trackId.isEmpty) {
        throw ArgumentError.value(track, 'track', 'A track id is required.');
      }

      Object? lastCollision;
      for (var attempt = 0; attempt < 3; attempt++) {
        final position = await _nextPosition(playlistId);
        try {
          await _client.from('playlist_tracks').insert({
            'user_id': _userId,
            'playlist_id': playlistId,
            'position': position,
            'track_id': trackId,
            'track_data': normalized,
          });
          await refresh();
          return;
        } catch (error) {
          if (!_isPositionCollision(error) || attempt == 2) rethrow;
          lastCollision = error;
        }
      }
      throw StateError('Could not allocate a playlist position: $lastCollision');
    });
  }

  Future<void> removeTrack(String id, String trackId) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      final playlistId = _requireId(id, 'playlist id');
      final normalizedTrackId = _requireId(trackId, 'track id');
      await _client
          .from('playlist_tracks')
          .delete()
          .eq('user_id', _userId)
          .eq('playlist_id', playlistId)
          .eq('track_id', normalizedTrackId);
      await refresh();
    });
  }

  Future<int> _nextPosition(String playlistId) async {
    final rows = await _client
        .from('playlist_tracks')
        .select('position')
        .eq('user_id', _userId)
        .eq('playlist_id', playlistId)
        .order('position', ascending: false)
        .limit(1);
    if (rows is! List || rows.isEmpty) return 0;
    final raw = rows.first;
    if (raw is! Map) return 0;
    final position = raw['position'];
    if (position is num) return position.toInt() + 1;
    return (int.tryParse(position?.toString() ?? '') ?? -1) + 1;
  }

  Future<void> _enqueueWrite(Future<void> Function() write) {
    final scheduled = _writeQueue.then((_) {
      if (_disposed) throw StateError('The account changed. Please retry.');
      return write();
    });
    _writeQueue = scheduled.catchError((_) {});
    return scheduled.catchError((error, stackTrace) {
      _recordError(error);
      Error.throwWithStackTrace(error, stackTrace);
    });
  }

  void _requirePlaylistWrites() {
    if (_playlistWritesEnabled) return;
    throw StateError('Playlist edits are not enabled for web yet.');
  }

  String _validateName(String name) {
    final normalized = name.trim();
    if (normalized.isEmpty || normalized.length > 120) {
      throw ArgumentError.value(name, 'name', 'Use 1 to 120 characters.');
    }
    return normalized;
  }

  String _requireId(String value, String label) {
    final normalized = value.trim();
    if (normalized.isEmpty) {
      throw ArgumentError.value(value, label, 'A non-empty value is required.');
    }
    return normalized;
  }

  String _newPlaylistId() =>
      'web-${DateTime.now().toUtc().microsecondsSinceEpoch}-${_random.nextInt(1 << 32)}';

  bool _isCurrent(int generation) => !_disposed && generation == _refreshGeneration;

  void _recordError(Object error) {
    if (_disposed) return;
    _error = _messageFor(error);
    notifyListeners();
  }

  String _messageFor(Object error) => error is StateError ? error.message.toString()
      : error is ArgumentError ? 'Check the name or track and retry.'
      : 'Could not update your library. Check the connection and retry.';

  bool _isPositionCollision(Object error) =>
      error.toString().contains('23505') ||
      error.toString().toLowerCase().contains('duplicate key');

  List<Map<String, dynamic>> _decodeLikes(dynamic rawRows) {
    if (rawRows is! List) return const [];
    return rawRows.whereType<Map>().map((rawRow) {
      final row = Map<String, dynamic>.from(rawRow);
      final rawTrack = row['track_data'];
      if (rawTrack is! Map) return <String, dynamic>{};
      final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
      final trackId = row['track_id']?.toString().trim();
      return {
        ...track,
        if (trackId != null && trackId.isNotEmpty) 'id': trackId,
        if (trackId != null && trackId.isNotEmpty) 'track_key': trackId,
      };
    }).where((track) => extractTrackId(track)?.isNotEmpty ?? false).toList(growable: false);
  }

  List<Map<String, dynamic>> _decodePlaylists(
    dynamic rawPlaylists,
    dynamic rawTracks,
  ) {
    final tracksByPlaylist = <String, List<Map<String, dynamic>>>{};
    if (rawTracks is List) {
      for (final rawRow in rawTracks.whereType<Map>()) {
        final row = Map<String, dynamic>.from(rawRow);
        final playlistId = row['playlist_id']?.toString().trim();
        final rawTrack = row['track_data'];
        if (playlistId == null || playlistId.isEmpty || rawTrack is! Map) continue;
        final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
        final trackId = row['track_id']?.toString().trim();
        tracksByPlaylist.putIfAbsent(playlistId, () => []).add({
          ...track,
          if (trackId != null && trackId.isNotEmpty) 'id': trackId,
          if (trackId != null && trackId.isNotEmpty) 'track_key': trackId,
        });
      }
    }
    if (rawPlaylists is! List) return const [];
    return rawPlaylists.whereType<Map>().map((rawRow) {
      final row = Map<String, dynamic>.from(rawRow);
      final id = row['id']?.toString().trim() ?? '';
      return <String, dynamic>{
        'id': id,
        'name': row['name']?.toString() ?? 'Untitled Playlist',
        'tracks': List<Map<String, dynamic>>.unmodifiable(tracksByPlaylist[id] ?? const []),
      };
    }).where((playlist) => (playlist['id'] as String).isNotEmpty).toList(growable: false);
  }
}
