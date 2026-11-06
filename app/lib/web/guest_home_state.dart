import 'dart:convert';
import 'dart:math';

import '../logic/track_metadata.dart';

/// Bounded browser-local listening evidence, partitioned by backend and scope.
/// Guest scopes are random partition keys, not authenticated identities.
class GuestHomeState {
  GuestHomeState(
      {required this.scopeId,
      Iterable<Map<String, dynamic>> snapshots = const []})
      : _snapshots = _bounded(snapshots);

  static const maxSnapshots = 32;
  final String scopeId;
  final List<Map<String, dynamic>> _snapshots;
  final List<Map<String, dynamic>> _serverFrequent = [];
  final List<Map<String, dynamic>> _pendingPlays = [];
  bool _accountHydrated = false;
  bool get hasHistorySnapshot => _accountHydrated || _snapshots.isNotEmpty;
  static const maxPendingPlays = 512;
  List<Map<String, dynamic>> get pendingPlays => List.unmodifiable(
      _pendingPlays.map((item) => Map<String, dynamic>.from(item)));
  List<String> get pendingPlayKeys =>
      _pendingPlays.map((item) => '${item['key']}').toList();
  List<Map<String, dynamic>> get snapshots => !_accountHydrated
      ? List.unmodifiable(_snapshots)
      : _accountItems().take(maxSnapshots).toList().reversed.toList();
  List<Map<String, dynamic>> get frequentSnapshots {
    final items = (_accountHydrated ? _accountItems() : [..._snapshots])
        .where((item) => (item['play_count'] as num? ?? 1) >= 2)
        .toList()
      ..sort((a, b) {
        final count =
            (b['play_count'] as num).compareTo(a['play_count'] as num);
        return count != 0
            ? count
            : (b['last_played_at'] as num? ?? 0)
                .compareTo(a['last_played_at'] as num? ?? 0);
      });
    return items.take(maxSnapshots).toList();
  }

  static String _identity(Map<String, dynamic> track) {
    for (final key in const [
      'musicbrainz_recording_id',
      'canonical_recording_id',
      'recording_mbid'
    ]) {
      var value = '${track[key] ?? ''}'.trim().toLowerCase();
      if (value.isEmpty) continue;
      value = value.replaceFirst(RegExp(r'^(musicbrainz:)?recording:'), '');
      return 'recording:$value';
    }
    final isrc =
        '${track['isrc'] ?? ''}'.replaceAll(RegExp(r'[\s-]'), '').toUpperCase();
    if (isrc.isNotEmpty) return 'isrc:$isrc';
    for (final key in const [
      'canonical_entity_id',
      'canonical_track_identity'
    ]) {
      final value = '${track[key] ?? ''}'.trim();
      if (value.isEmpty) continue;
      final recording =
          RegExp(r'^(musicbrainz:)?recording:', caseSensitive: false);
      if (recording.hasMatch(value)) {
        return 'recording:${value.replaceFirst(recording, '').toLowerCase()}';
      }
      return 'canonical:$value';
    }
    // Equal display names cannot prove two sources are the same recording.
    return 'source:${extractTrackId(track)}';
  }

  List<Map<String, dynamic>> _accountItems() {
    final items = <String, Map<String, dynamic>>{};
    final identityBySource = <String, String>{};
    for (final item in [..._serverFrequent, ..._snapshots]) {
      final key = _identity(item), old = items[_identity(item)];
      final source = extractTrackId(item);
      if (source != null) identityBySource[source] = key;
      items[key] = {
        ...?old,
        ...item,
        'play_count': max((old?['play_count'] as num? ?? 0).toInt(),
            (item['play_count'] as num? ?? 0).toInt())
      };
    }
    for (final event in _pendingPlays) {
      final track = Map<String, dynamic>.from(event['track'] as Map);
      // Server hydration can add identity after this play was queued. Exact
      // source ownership joins its pending overlay without guessing by title.
      final key = identityBySource[extractTrackId(track)] ?? _identity(track);
      final old = items[key];
      final newer = (event['occurred_at'] as num) >=
          (old?['last_played_at'] as num? ?? 0);
      items[key] = {
        ...?old,
        if (newer) ...track,
        if (old == null) ...track,
        'play_count': (old?['play_count'] as num? ?? 0).toInt() + 1,
        'last_played_at': max((event['occurred_at'] as num).toDouble(),
            (old?['last_played_at'] as num? ?? 0).toDouble())
      };
    }
    return items.values.toList()
      ..sort((a, b) => (b['last_played_at'] as num? ?? 0)
          .compareTo(a['last_played_at'] as num? ?? 0));
  }

  /// Keep the original timestamp and key across retries. The backend already
  /// deduplicates play writes; only a matching committed history read retires it.
  void queueAccountPlay(Map<String, dynamic> track, {required DateTime at}) {
    if (extractTrackId(track) == null || isTrackMetadataIncomplete(track))
      return;
    if (_pendingPlays.length >= maxPendingPlays) {
      throw StateError(
          'Listening history needs to sync before more plays can be queued.');
    }
    if (!_accountHydrated) recordPlayedSnapshot(track, at: at);
    final value = GuestHomeState(scopeId: scopeId)
      ..recordPlayedSnapshot(track, at: at);
    _pendingPlays.add({
      'key': '${at.microsecondsSinceEpoch}:${newScopeId()}',
      'occurred_at': at.millisecondsSinceEpoch / 1000,
      'track': value._snapshots.single
    });
  }

  void reconcileAccount(
      {required Iterable<Map<String, dynamic>> recent,
      required Iterable<Map<String, dynamic>> frequent,
      required Iterable<String> acknowledgedPlayKeys}) {
    final confirmed = acknowledgedPlayKeys.toSet();
    _pendingPlays.removeWhere((event) => confirmed.contains(event['key']));
    _snapshots
      ..clear()
      ..addAll(_bounded(recent.toList().reversed));
    _serverFrequent
      ..clear()
      ..addAll(_bounded(frequent));
    _accountHydrated = true;
  }

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
    final previous =
        _snapshots.where((item) => extractTrackId(item) == id).firstOrNull;
    final snapshot = <String, dynamic>{
      'id': id,
      'track_key': id,
      'title': normalized['title'],
      'artist': normalized['artist'],
      'channel': normalized['channel'],
      'album': normalized['album'],
      'thumbnail': normalized['thumbnail'],
      'duration': trackDurationSeconds(normalized),
      'play_count': ((previous?['play_count'] as num?)?.toInt() ??
              (previous == null ? 0 : 1)) +
          1,
      'last_played_at': (at ?? DateTime.now()).millisecondsSinceEpoch / 1000,
      for (final key in const [
        'album_id',
        'canonical_recording_id',
        'musicbrainz_recording_id',
        'recording_mbid',
        'canonical_entity_id',
        'canonical_track_identity',
        'isrc',
        'videoId',
        'source_id',
        'playable',
        'source_verified'
      ])
        if (normalized[key] != null) key: normalized[key],
      if (normalized['playback'] is Map)
        'playback': {
          for (final key in const ['provider', 'source_id'])
            if ((normalized['playback'] as Map)[key] != null)
              key: (normalized['playback'] as Map)[key],
        },
    };
    _snapshots
        .removeWhere((item) => item['id'] == id || item['track_key'] == id);
    _snapshots.add(snapshot);
    if (_snapshots.length > maxSnapshots) {
      _snapshots.removeRange(0, _snapshots.length - maxSnapshots);
    }
  }

  Map<String, dynamic> toJson() => {
        'version': 1,
        'scope_id': scopeId,
        'snapshots': _snapshots,
        if (_accountHydrated) 'account_hydrated': true,
        if (_serverFrequent.isNotEmpty) 'frequent': _serverFrequent,
        if (_pendingPlays.isNotEmpty) 'pending_plays': _pendingPlays,
      };

  String encode() => jsonEncode(toJson());

  /// Reconcile the server's existing history without double counting local plays.
  void mergeHistory(Iterable<Map<String, dynamic>> tracks) {
    final combined = <String, Map<String, dynamic>>{
      for (final item in _snapshots)
        if (extractTrackId(item) case final String id) id: item,
    };
    for (final raw in tracks) {
      final id = extractTrackId(raw);
      if (id == null || isTrackMetadataIncomplete(raw)) continue;
      final old = combined[id] ?? const <String, dynamic>{};
      final newer = (raw['last_played_at'] as num? ?? 0) >=
          (old['last_played_at'] as num? ?? 0);
      combined[id] = {
        ...(newer ? old : raw),
        ...(newer ? raw : old),
        'id': id,
        'play_count': max((old['play_count'] as num? ?? 1).toInt(),
            (raw['play_count'] as num? ?? 1).toInt()),
      };
    }
    final sorted = combined.values.toList()
      ..sort((a, b) => (a['last_played_at'] as num? ?? 0)
          .compareTo(b['last_played_at'] as num? ?? 0));
    _snapshots
      ..clear()
      ..addAll(_bounded(sorted));
  }

  static GuestHomeState decode(String raw, {String? expectedScope}) {
    final value = jsonDecode(raw);
    if (value is! Map || value['version'] != 1) {
      throw const FormatException('Unsupported guest Home state.');
    }
    final scope = value['scope_id']?.toString() ?? '';
    if (expectedScope != null
        ? scope.isEmpty || scope != expectedScope
        : !RegExp(r'^guest:web:[0-9a-f-]{36}$').hasMatch(scope)) {
      throw const FormatException('Invalid guest Home scope.');
    }
    final rawSnapshots = value['snapshots'];
    final snapshots = rawSnapshots is List
        ? rawSnapshots
            .whereType<Map>()
            .map((item) => Map<String, dynamic>.from(item))
        : const <Map<String, dynamic>>[];
    final state = GuestHomeState(scopeId: scope, snapshots: snapshots);
    if (expectedScope != null && !scope.startsWith('guest:')) {
      state._accountHydrated = value['account_hydrated'] == true;
      state._serverFrequent.addAll(_bounded((value['frequent'] as List? ?? [])
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))));
      for (final raw in (value['pending_plays'] as List? ?? [])
          .whereType<Map>()
          .take(maxPendingPlays)) {
        final track = raw['track'];
        if (raw['key'] is! String ||
            raw['occurred_at'] is! num ||
            !(raw['occurred_at'] as num).isFinite ||
            track is! Map ||
            extractTrackId(Map<String, dynamic>.from(track)) == null) continue;
        state._pendingPlays.add(Map<String, dynamic>.from(raw));
      }
    }
    return state;
  }

  static List<Map<String, dynamic>> _bounded(
      Iterable<Map<String, dynamic>> input) {
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

  void reset() {
    _operation = null;
    _id = null;
    _baseline = _lastPosition = 0;
    _seeking = _recorded = false;
  }

  bool observe(String event,
      {required int operation,
      required String? trackId,
      required String? loadedTrackId,
      required double position,
      required bool paused}) {
    if (_operation != operation || _id != trackId) {
      _operation = operation;
      _id = trackId;
      _baseline = position;
      _lastPosition = position;
      _seeking = _recorded = false;
    }
    if (trackId == null || loadedTrackId != trackId) return false;
    if (event == 'seeking') _seeking = true;
    if (event == 'seeked') {
      _seeking = false;
      _baseline = _lastPosition = position;
      return false;
    }
    if (event == 'timeupdate' && !_seeking && !paused) {
      if (_recorded && position < 1 && _lastPosition > position + 1) {
        _recorded = false;
        _baseline = position;
      }
      _lastPosition = position;
    }
    if (event != 'timeupdate' ||
        paused ||
        _seeking ||
        trackId == null ||
        loadedTrackId != trackId ||
        _recorded) return false;
    if (position < _baseline) _baseline = position;
    if (position < _baseline + 3) return false;
    _recorded = true;
    return true;
  }
}
