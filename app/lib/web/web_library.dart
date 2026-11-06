import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import '../logic/track_metadata.dart';

class _LikeWrite {
  _LikeWrite(this.confirmed);
  Map<String, dynamic>? confirmed;
  int version = 0;
  int pending = 0;
}

/// Account-scoped cloud library state for the browser client.
///
/// This class deliberately works only on individual records. In particular,
/// playlist changes never use the native client's replace-all sync strategy.
class WebLibrary extends ChangeNotifier {
  WebLibrary(
    this._client,
    this._userId, {
    bool playlistWritesEnabled = false,
    String? Function()? accessToken,
    bool Function()? scopeIsCurrent,
  })  : _playlistWritesEnabled = playlistWritesEnabled,
        _accessToken = accessToken,
        _scopeIsCurrent = scopeIsCurrent;

  final SupabaseClient _client;
  final String _userId;
  final bool _playlistWritesEnabled;
  final String? Function()? _accessToken;
  final bool Function()? _scopeIsCurrent;
  final Random _random = Random.secure();

  List<Map<String, dynamic>> _likes = const [];
  List<Map<String, dynamic>> _playlists = const [];
  bool _loading = false;
  String? _error;
  bool _disposed = false;
  int _refreshGeneration = 0;
  Future<void> _writeQueue = Future<void>.value();
  final Map<String, _LikeWrite> _likeWrites = {};
  final Map<String, int> _likeRevisions = {};
  int _likeRevision = 0;

  List<Map<String, dynamic>> get likes => List.unmodifiable(_likes);
  List<Map<String, dynamic>> get playlists => List.unmodifiable(_playlists);
  bool get playlistWritesEnabled => _playlistWritesEnabled;
  bool get active => !_disposed && _scopeIsCurrent?.call() != false;
  bool get loading => _loading;
  String? get error => _error;

  Future<List<Map<String, dynamic>>> recentSearchQueries() async {
    final rows = await _client
        .from('search_events')
        .select('query,result_count,created_at')
        .eq('user_id', _userId)
        .order('created_at', ascending: false)
        .limit(32)
        .timeout(const Duration(seconds: 2));
    if (_disposed) return [];
    return [
      for (final row in rows)
        if ((row['result_count'] as num? ?? 0) > 0 &&
            '${row['query'] ?? ''}'.trim().isNotEmpty)
          {'query': row['query'], 'selected_at': row['created_at']}
    ];
  }

  @override
  void dispose() {
    _disposed = true;
    _refreshGeneration++;
    super.dispose();
  }

  Future<void> refresh() async {
    final generation = ++_refreshGeneration;
    // Older refreshes cannot publish after this generation begins.
    _likeRevisions.clear();
    final likeRevision = _likeRevision;
    if (!_isCurrent(generation)) return;
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      final reads = await Future.wait<dynamic>([
        _client
            .from('library_tracks')
            .select('track_id,track_data,added_at')
            .eq('user_id', _userId)
            .order('added_at', ascending: false),
        if (_playlistWritesEnabled)
          _client.rpc('neatie_playlist_library')
        else ...[
          _client
              .from('playlists')
              .select('id,name,created_at')
              .eq('user_id', _userId)
              .order('created_at'),
          _client
              .from('playlist_tracks')
              .select('playlist_id,position,track_id,track_data')
              .eq('user_id', _userId)
              .order('position'),
        ],
      ]);
      final likedRows = reads[0];
      final playlistRows = reads[1];
      final playlistTrackRows = _playlistWritesEnabled
          ? [
              for (final playlist in (playlistRows as List).whereType<Map>())
                for (final track in (playlist['tracks'] as List? ?? const [])
                    .whereType<Map>())
                  {...track, 'playlist_id': playlist['id']}
            ]
          : reads[2];

      final nextLikes = _decodeLikes(likedRows);
      final nextPlaylists = _decodePlaylists(playlistRows, playlistTrackRows);
      if (!_isCurrent(generation)) return;
      // A read can finish after a local intent or acknowledgement. Preserve
      // those tracks while still accepting unrelated changes from the server.
      final protectedIds = <String>{
        ..._likeWrites.keys,
        for (final entry in _likeRevisions.entries)
          if (entry.value > likeRevision) entry.key,
      };
      _likes = [
        ..._likes
            .where((track) => protectedIds.contains(extractTrackId(track))),
        ...nextLikes
            .where((track) => !protectedIds.contains(extractTrackId(track))),
      ];
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
    final normalized = normalizeTrack(track);
    final trackId = extractTrackId(normalized)?.trim();
    if (trackId == null || trackId.isEmpty) {
      return Future.error(
          ArgumentError.value(track, 'track', 'A track id is required.'));
    }
    if (_disposed || _scopeIsCurrent?.call() == false) {
      return Future.error(StateError('The account changed. Please retry.'));
    }
    final token = _accessToken?.call();
    if (_accessToken != null && token == null) {
      return Future.error(StateError('The account changed. Please retry.'));
    }
    final state = _likeWrites.putIfAbsent(
        trackId,
        () => _LikeWrite(_likes
            .where((item) => extractTrackId(item) == trackId)
            .firstOrNull));
    final version = ++state.version;
    state.pending++;
    _setLocalLike(trackId, liked ? normalized : null);
    _error = null;
    notifyListeners();
    return _enqueueWrite(() async {
      try {
        if (_disposed || _scopeIsCurrent?.call() == false) {
          throw StateError('The account changed. Please retry.');
        }
        if (liked) {
          final request = _client.from('library_tracks').upsert(
            {
              'user_id': _userId,
              'track_id': trackId,
              'track_data': normalized,
              'updated_at': DateTime.now().toUtc().toIso8601String(),
            },
            onConflict: 'user_id,track_id',
          );
          await (token == null
              ? request
              : request.setHeader('Authorization', 'Bearer $token'));
        } else {
          final request = _client
              .from('library_tracks')
              .delete()
              .eq('user_id', _userId)
              .eq('track_id', trackId);
          await (token == null
              ? request
              : request.setHeader('Authorization', 'Bearer $token'));
        }
        state.confirmed = liked ? normalized : null;
        _likeRevisions[trackId] = ++_likeRevision;
        if (!_disposed &&
            _scopeIsCurrent?.call() != false &&
            state.version == version &&
            _error != null) {
          _error = null;
          notifyListeners();
        }
      } catch (_) {
        // An obsolete intent must not report an error over a newer tap.
        if (state.version != version) return;
        if (!_disposed &&
            _scopeIsCurrent?.call() != false &&
            state.version == version) {
          _setLocalLike(trackId, state.confirmed);
          notifyListeners();
        }
        rethrow;
      } finally {
        state.pending--;
        if (state.pending == 0) _likeWrites.remove(trackId);
      }
    }, checkScope: false);
  }

  void _setLocalLike(String trackId, Map<String, dynamic>? track) {
    _likeRevisions[trackId] = ++_likeRevision;
    _likes = [
      if (track != null) {...track, 'id': trackId, 'track_key': trackId},
      ..._likes.where((item) => extractTrackId(item) != trackId),
    ];
  }

  Future<String> createPlaylist(String name,
      {Map<String, dynamic>? initialTrack}) async {
    final id = _newPlaylistId();
    await _enqueueWrite(() async {
      _requirePlaylistWrites();
      await _editPlaylist(id, 'create', {
        'name': _validateName(name),
        if (initialTrack != null) ..._trackPayload(initialTrack)
      });
    });
    return id;
  }

  Future<void> renamePlaylist(String id, String name, {int? expectedRevision}) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      await _editPlaylist(id, 'rename', {'name': _validateName(name)},
          expectedRevision: expectedRevision);
    });
  }

  Future<void> deletePlaylist(String id, {int? expectedRevision}) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      await _editPlaylist(id, 'delete', {}, expectedRevision: expectedRevision);
    });
  }

  Future<void> addTrack(String id, Map<String, dynamic> track,
      {int? expectedRevision}) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      await _editPlaylist(id, 'append', _trackPayload(track),
          expectedRevision: expectedRevision);
    });
  }

  Map<String, dynamic> _trackPayload(Map<String, dynamic> track) {
    final normalized = normalizeTrack(track);
    final id = extractTrackId(normalized)?.trim();
    if (id == null || id.isEmpty)
      throw ArgumentError.value(track, 'track', 'A track id is required.');
    normalized.remove('playlist_entry_id');
    normalized.remove('playlist_position');
    return {'track_id': id, 'track_data': normalized};
  }

  Future<void> removeTrack(String id, String entryId, {int? expectedRevision}) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      await _editPlaylist(
          id, 'remove', {'entry_id': _requireId(entryId, 'entry id')},
          expectedRevision: expectedRevision);
    });
  }

  Future<void> moveTrack(String id, String entryId, int position,
      {int? expectedRevision}) {
    return _enqueueWrite(() async {
      _requirePlaylistWrites();
      if (position < 0) throw ArgumentError.value(position, 'position');
      await _editPlaylist(id, 'move',
          {'entry_id': _requireId(entryId, 'entry id'), 'position': position},
          expectedRevision: expectedRevision);
    });
  }

  Future<void> _editPlaylist(
      String id, String action, Map<String, dynamic> payload,
      {int? expectedRevision}) async {
    final playlistId = _requireId(id, 'playlist id');
    final existing =
        _playlists.where((row) => row['id'] == playlistId).firstOrNull;
    final revision = expectedRevision ?? (existing?['revision'] as int?);
    if (action != 'create' && revision == null) {
      throw StateError('Refresh this playlist before editing.');
    }
    // Invalidate reads started before this edit; only a committed snapshot publishes.
    _refreshGeneration++;
    _loading = false;
    try {
      final token = _accessToken?.call();
      if (_accessToken != null && token == null) {
        throw StateError('The account changed. Please retry.');
      }
      final request = _client.rpc('neatie_edit_playlist', params: {
        'p_playlist_id': playlistId,
        'p_action': action,
        'p_expected_revision': action == 'create' ? null : revision,
        'p_payload': payload,
      });
      final result = await (token == null
              ? request
              : request.setHeader('Authorization', 'Bearer $token'))
          .timeout(const Duration(seconds: 15));
      if (_disposed || _scopeIsCurrent?.call() == false) return;
      if (result is! Map ||
          result['id'] != playlistId ||
          (result['deleted'] != true &&
              (result['revision'] is! num || result['tracks'] is! List)) ||
          (action == 'create' &&
              payload['track_id'] != null &&
              (result['tracks'] is! List ||
                  !(result['tracks'] as List)
                      .whereType<Map>()
                      .any((row) => row['track_id'] == payload['track_id'])))) {
        throw StateError(
            'Could not confirm the saved playlist. Refresh before retrying.');
      }
      _refreshGeneration++;
      _loading = false;
      final decoded = result['deleted'] == true
          ? null
          : _decodePlaylists([
              result
            ], [
              for (final row in (result['tracks'] as List).whereType<Map>())
                {...row, 'playlist_id': playlistId}
            ]).single;
      final at = _playlists.indexWhere((row) => row['id'] == playlistId);
      _playlists = List.of(_playlists);
      if (decoded == null) {
        _playlists.removeWhere((row) => row['id'] == playlistId);
      } else if (at < 0) {
        _playlists.add(decoded);
      } else {
        _playlists[at] = decoded;
      }
      _error = null;
      notifyListeners();
    } on PostgrestException catch (error) {
      if (const {'40001', 'P0002', '23505'}.contains(error.code)) {
        await refresh();
        throw StateError(
            'This playlist changed on another device. Review the latest version and retry your edit.');
      }
      if (error.code == 'PGRST202' || error.code == '42883') {
        throw StateError(
            'The cloud playlist upgrade is not installed. Editing is unavailable.');
      }
      rethrow;
    }
  }

  Future<void> _enqueueWrite(Future<void> Function() write,
      {bool checkScope = true}) {
    final scheduled = _writeQueue.then((_) {
      if (checkScope && (_disposed || _scopeIsCurrent?.call() == false)) {
        throw StateError('The account changed. Please retry.');
      }
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

  // Web bit shifts are 32-bit: 1 << 32 becomes zero in the compiled client.
  String _newPlaylistId() =>
      'web-${DateTime.now().toUtc().microsecondsSinceEpoch}-${_random.nextInt(0x100000000)}';

  bool _isCurrent(int generation) =>
      !_disposed &&
      generation == _refreshGeneration &&
      _scopeIsCurrent?.call() != false;

  void _recordError(Object error) {
    if (_disposed || _scopeIsCurrent?.call() == false) return;
    _error = _messageFor(error);
    notifyListeners();
  }

  String _messageFor(Object error) => error is StateError
      ? error.message.toString()
      : error is ArgumentError
          ? 'Check the name or track and retry.'
          : 'Could not update your library. Check the connection and retry.';

  List<Map<String, dynamic>> _decodeLikes(dynamic rawRows) {
    if (rawRows is! List) return const [];
    return rawRows
        .whereType<Map>()
        .map((rawRow) {
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
        })
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .toList(growable: false);
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
        if (playlistId == null || playlistId.isEmpty || rawTrack is! Map)
          continue;
        final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
        final trackId = row['track_id']?.toString().trim();
        tracksByPlaylist.putIfAbsent(playlistId, () => []).add({
          ...track,
          if (row['id'] != null) 'playlist_entry_id': '${row['id']}',
          'playlist_position': row['position'],
          if (trackId != null && trackId.isNotEmpty) 'id': trackId,
          if (trackId != null && trackId.isNotEmpty) 'track_key': trackId,
        });
      }
    }
    if (rawPlaylists is! List) return const [];
    return rawPlaylists
        .whereType<Map>()
        .map((rawRow) {
          final row = Map<String, dynamic>.from(rawRow);
          final id = row['id']?.toString().trim() ?? '';
          return <String, dynamic>{
            'id': id,
            'name': row['name']?.toString() ?? 'Untitled Playlist',
            'created_at': row['created_at'],
            'revision': (row['revision'] as num?)?.toInt(),
            'tracks': List<Map<String, dynamic>>.unmodifiable(
                tracksByPlaylist[id] ?? const []),
          };
        })
        .where((playlist) => (playlist['id'] as String).isNotEmpty)
        .toList(growable: false);
  }
}
