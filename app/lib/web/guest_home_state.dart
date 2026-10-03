import 'dart:convert';
import 'dart:math';

import '../logic/track_metadata.dart';

/// Bounded browser-local listening evidence, partitioned by backend and scope.
/// Guest scopes are random partition keys, not authenticated identities.
class GuestHomeState {
  GuestHomeState({required this.scopeId, Iterable<Map<String, dynamic>> snapshots = const []})
      : _snapshots = _bounded(snapshots);

  static const maxSnapshots = 32;
  final String scopeId;
  final List<Map<String, dynamic>> _snapshots;
  List<Map<String, dynamic>> get snapshots => List.unmodifiable(_snapshots);

  static String newScopeId([Random? random]) {
    final source = random ?? Random.secure();
    final bytes = List<int>.generate(16, (_) => source.nextInt(256));
    // UUID v4 layout; randomness is supplied by Random.secure in browsers.
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    return 'guest:web:${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
  }

  /// Caller must establish real playback progression before recording.
  void recordPlayedSnapshot(Map<String, dynamic> track, {DateTime? at}) {
    final id = extractTrackId(track);
    if (id == null) return;
    final normalized = normalizeTrack(track);
    final previous = _snapshots.where((item) => extractTrackId(item) == id).firstOrNull;
    final snapshot = <String, dynamic>{
      'id': id,
      'track_key': id,
      'title': normalized['title'],
      'artist': normalized['artist'],
      'channel': normalized['channel'],
      'album': normalized['album'],
      'thumbnail': normalized['thumbnail'],
      'duration': trackDurationSeconds(normalized),
      'play_count': ((previous?['play_count'] as num?)?.toInt() ?? (previous == null ? 0 : 1)) + 1,
      'last_played_at': (at ?? DateTime.now()).millisecondsSinceEpoch / 1000,
      for (final key in const ['album_id', 'canonical_recording_id', 'musicbrainz_recording_id',
        'videoId', 'source_id', 'playback', 'playable', 'source_verified'])
        if (normalized[key] != null) key: normalized[key],
    };
    _snapshots.removeWhere((item) => item['id'] == id || item['track_key'] == id);
    _snapshots.add(snapshot);
    if (_snapshots.length > maxSnapshots) {
      _snapshots.removeRange(0, _snapshots.length - maxSnapshots);
    }
  }

  Map<String, dynamic> toJson() => {
        'version': 1,
        'scope_id': scopeId,
        'snapshots': _snapshots,
      };

  String encode() => jsonEncode(toJson());

  /// Reconcile the server's existing history without double counting local plays.
  void mergeHistory(Iterable<Map<String, dynamic>> tracks) {
    final combined = <String, Map<String, dynamic>>{
      for (final item in _snapshots) if (extractTrackId(item) case final String id) id: item,
    };
    for (final raw in tracks) {
      final id = extractTrackId(raw);
      if (id == null || isTrackMetadataIncomplete(raw)) continue;
      final old = combined[id] ?? const <String, dynamic>{};
      final newer = (raw['last_played_at'] as num? ?? 0) >= (old['last_played_at'] as num? ?? 0);
      combined[id] = {
        ...(newer ? old : raw), ...(newer ? raw : old), 'id': id,
        'play_count': max((old['play_count'] as num? ?? 1).toInt(), (raw['play_count'] as num? ?? 1).toInt()),
      };
    }
    final sorted = combined.values.toList()..sort((a, b) =>
      (a['last_played_at'] as num? ?? 0).compareTo(b['last_played_at'] as num? ?? 0));
    _snapshots..clear()..addAll(_bounded(sorted));
  }

  static GuestHomeState decode(String raw, {String? expectedScope}) {
    final value = jsonDecode(raw);
    if (value is! Map || value['version'] != 1) {
      throw const FormatException('Unsupported guest Home state.');
    }
    final scope = value['scope_id']?.toString() ?? '';
    if (expectedScope != null ? scope.isEmpty || scope != expectedScope :
        !RegExp(r'^guest:web:[0-9a-f-]{36}$').hasMatch(scope)) {
      throw const FormatException('Invalid guest Home scope.');
    }
    final rawSnapshots = value['snapshots'];
    final snapshots = rawSnapshots is List
        ? rawSnapshots.whereType<Map>().map((item) => Map<String, dynamic>.from(item))
        : const <Map<String, dynamic>>[];
    return GuestHomeState(scopeId: scope, snapshots: snapshots);
  }

  static List<Map<String, dynamic>> _bounded(Iterable<Map<String, dynamic>> input) {
    final result = <Map<String, dynamic>>[];
    for (final raw in input) {
      final id = extractTrackId(raw);
      if (id == null || isTrackMetadataIncomplete(raw)) continue;
      result.removeWhere((item) => item['id'] == id || item['track_key'] == id);
      result.add(Map<String, dynamic>.from(raw));
    }
    if (result.length > maxSnapshots) {
      return result.sublist(result.length - maxSnapshots);
    }
    return result;
  }
}

/// Records one play per selected playback operation after real progression.
/// Seeking or restoring a large position cannot fabricate a listening event.
class PlaybackHistoryTracker {
  int? _operation;
  String? _id;
  double _baseline = 0;
  double _lastPosition = 0;
  bool _seeking = false, _recorded = false;

  void reset() { _operation = null; _id = null; _baseline = _lastPosition = 0; _seeking = _recorded = false; }

  bool observe(String event, {required int operation, required String? trackId,
    required String? loadedTrackId, required double position, required bool paused}) {
    if (_operation != operation || _id != trackId) {
      _operation = operation; _id = trackId; _baseline = position;
      _lastPosition = position;
      _seeking = _recorded = false;
    }
    if (trackId == null || loadedTrackId != trackId) return false;
    if (event == 'seeking') _seeking = true;
    if (event == 'seeked') { _seeking = false; _baseline = _lastPosition = position; return false; }
    if (event == 'timeupdate' && !_seeking && !paused) {
      if (_recorded && position < 1 && _lastPosition > position + 1) {
        _recorded = false; _baseline = position;
      }
      _lastPosition = position;
    }
    if (event != 'timeupdate' || paused || _seeking || trackId == null ||
        loadedTrackId != trackId || _recorded) return false;
    if (position < _baseline) _baseline = position;
    if (position < _baseline + 3) return false;
    _recorded = true;
    return true;
  }
}
