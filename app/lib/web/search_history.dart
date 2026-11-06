import 'dart:convert';
import '../logic/track_metadata.dart';
import 'playback_port.dart';

/// Browser storage adapter for the existing recent-query/selected-pick contracts.
class SearchHistory {
  SearchHistory(this.scope, {this.store}) {
    try {
      final raw = store?.read();
      if (raw == null || raw.length > 65536) return;
      final saved = jsonDecode(raw) as Map;
      if (saved['version'] != 1 || saved['scope'] != scope) return;
      clearedAt = (saved['cleared_at'] as num?)?.toDouble() ?? 0;
      hidden.addAll(
          (saved['hidden'] as List? ?? []).whereType<String>().take(40));
      merge(
          (saved['entries'] as List? ?? [])
              .whereType<Map>()
              .map((entry) => Map<String, dynamic>.from(entry)),
          persist: false);
    } catch (_) {/* A damaged/blocked browser store must not block Search. */}
  }
  final String scope;
  final SessionStore? store;
  final List<Map<String, dynamic>> entries = [];
  final Set<String> hidden = {};
  double clearedAt = 0;
  static String key(String query) => query.trim().toLowerCase();
  List<String> get queries =>
      entries.map((entry) => '${entry['query']}').toSet().take(8).toList();
  void record(String query, {Map<String, dynamic>? track, DateTime? at}) {
    query = query.trim();
    if (query.isEmpty || query.length > 200) return;
    final previous = entries
        .where((entry) => key('${entry['query']}') == key(query))
        .firstOrNull;
    track ??= previous?['track'] as Map<String, dynamic>?;
    hidden.remove(key(query));
    entries.removeWhere((entry) => key('${entry['query']}') == key(query));
    merge([
      {
        'query': query,
        'selected_at': (at ?? DateTime.now()).millisecondsSinceEpoch / 1000,
        if (track != null) 'track': track
      }
    ]);
  }

  void merge(Iterable<Map<String, dynamic>> incoming, {bool persist = true}) {
    final combined = <String, Map<String, dynamic>>{};
    for (final entry in [...entries, ...incoming]) {
      final query = '${entry['query'] ?? ''}'.trim();
      final rawTime = entry['selected_at'];
      var time = rawTime is num
          ? rawTime.toDouble()
          : (DateTime.tryParse('$rawTime')?.millisecondsSinceEpoch ?? 0) / 1000;
      if (query.isEmpty ||
          query.length > 200 ||
          time <= clearedAt ||
          hidden.contains(key(query))) {
        continue;
      }
      final prior = combined[key(query)];
      if (prior != null && (prior['selected_at'] as num) >= time) {
        if (prior['track'] != null || entry['track'] is! Map) continue;
        // A newer query event must retain its older selected song.
        time = (prior['selected_at'] as num).toDouble();
      }
      final rawTrack = entry['track'] ?? prior?['track'];
      final track = rawTrack is Map
          ? normalizeTrack(Map<String, dynamic>.from(rawTrack))
          : null;
      combined[key(query)] = {
        'query': query,
        'selected_at': time,
        if (track != null && extractTrackId(track) != null)
          'track': {
            for (final field in const [
              'id',
              'track_key',
              'title',
              'artist',
              'channel',
              'thumbnail',
              'duration',
              'album',
              'album_id',
              'videoId',
              'source_id',
              'playable',
              'canonical_recording_id',
              'canonical_artist_id'
            ])
              if (track[field] != null) field: track[field],
            if (track['playback'] is Map)
              'playback': {
                for (final field in const ['provider', 'source_id'])
                  if ((track['playback'] as Map)[field] != null)
                    field: (track['playback'] as Map)[field],
              },
          }
      };
    }
    final ordered = combined.values.toList()
      ..sort((a, b) =>
          (b['selected_at'] as num).compareTo(a['selected_at'] as num));
    entries
      ..clear()
      ..addAll(ordered.take(20));
    if (persist) _save();
  }

  void remove(String query) {
    hidden.add(key(query));
    if (hidden.length > 40) hidden.remove(hidden.first);
    entries.removeWhere((entry) => key('${entry['query']}') == key(query));
    _save();
  }

  void clear({DateTime? at}) {
    clearedAt = (at ?? DateTime.now()).millisecondsSinceEpoch / 1000;
    entries.clear();
    hidden.clear();
    _save();
  }

  void _save() {
    try {
      final value = jsonEncode({
        'version': 1,
        'scope': scope,
        'entries': entries,
        'hidden': hidden.toList(),
        'cleared_at': clearedAt
      });
      if (value.length <= 65536) store?.write(value);
    } catch (_) {/* Storage failure must not interrupt a play gesture. */}
  }
}
