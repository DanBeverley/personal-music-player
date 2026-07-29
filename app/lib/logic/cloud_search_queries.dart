import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'auth_provider.dart';
import 'proxy_runtime.dart';
import 'track_metadata.dart';

const Duration _recentCloudSearchQueryTimeout = Duration(seconds: 2);
const Duration _recentCloudSearchQueryCacheTtl = Duration(seconds: 45);

List<String> _recentCloudSearchQueryCache = const [];
final Map<String, DateTime> _recentCloudSearchQueryTimes = {};
DateTime? _recentCloudSearchQueryCacheAt;
String _recentCloudSearchQueryCacheUserId = '';
List<RecentSearchPick> _recentSearchPickCache = const [];
DateTime? _recentSearchPickCacheAt;
String _recentSearchPickCacheScopeId = '';

class RecentSearchPick {
  const RecentSearchPick({
    required this.query,
    required this.track,
    required this.selectedAt,
  });

  final String query;
  final Map<String, dynamic> track;
  final DateTime selectedAt;

  String get title => track['title']?.toString().trim() ?? query;

  String get artist =>
      (track['artist'] ?? track['channel'] ?? track['author'])
          ?.toString()
          .trim() ??
      '';

  String? get trackId => extractTrackId(track);

  String get key {
    final id = trackId;
    if (id != null && id.isNotEmpty) return '${query.toLowerCase()}|$id';
    return '${query.toLowerCase()}|${title.toLowerCase()}|${artist.toLowerCase()}';
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'query': query,
        'track': track,
        'selected_at': selectedAt.toIso8601String(),
      };

  static RecentSearchPick? fromJson(dynamic value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    final query = map['query']?.toString().trim() ?? '';
    final rawTrack = map['track'];
    if (query.isEmpty || rawTrack is! Map) return null;
    final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
    if (track.isEmpty) return null;
    final selectedAtRaw = map['selected_at'];
    DateTime selectedAt;
    if (selectedAtRaw is num) {
      selectedAt = DateTime.fromMillisecondsSinceEpoch(
        (selectedAtRaw * 1000).round(),
        isUtc: true,
      ).toLocal();
    } else {
      selectedAt =
          DateTime.tryParse(selectedAtRaw?.toString() ?? '') ??
              DateTime.fromMillisecondsSinceEpoch(0);
    }
    return RecentSearchPick(
      query: query,
      track: track,
      selectedAt: selectedAt,
    );
  }
}

List<String> peekRecentCloudSearchQueries({int limit = 8}) {
  return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
}

DateTime recentCloudSearchQueryTimestamp(String query) =>
    _recentCloudSearchQueryTimes[query.trim().toLowerCase()] ??
    DateTime.fromMillisecondsSinceEpoch(0);

List<RecentSearchPick> peekRecentSearchPicks({int limit = 8}) {
  return _recentSearchPickCache.take(limit).toList(growable: false);
}

void invalidateRecentCloudSearchQueryCache() {
  _recentCloudSearchQueryCacheAt = null;
}

Future<File> _recentSearchPicksFile() {
  return getScopedDataFile('recent_search_picks.json');
}

Future<List<RecentSearchPick>> _loadRecentSearchPicksFromDisk() async {
  try {
    final file = await _recentSearchPicksFile();
    if (!file.existsSync()) return const [];
    final decoded = jsonDecode(await file.readAsString());
    if (decoded is! List) return const [];
    return decoded
        .map(RecentSearchPick.fromJson)
        .whereType<RecentSearchPick>()
        .toList(growable: false);
  } catch (error) {
    debugPrint('Recent selected search lookup failed: $error');
    return const [];
  }
}

Future<List<RecentSearchPick>> _loadRecentSearchPicksFromProxy({
  required int limit,
}) async {
  final scopeId = activeStorageScopeId;
  if (scopeId.trim().isEmpty || scopeId == 'guest') return const [];
  try {
    final uri = buildProxyUri('/recent_search_picks').replace(
      queryParameters: <String, String>{
        'user_scope_id': scopeId,
        'limit': limit.toString(),
      },
    );
    final response = await proxyControlHttpClient.get(uri).timeout(
          _recentCloudSearchQueryTimeout,
        );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      return const [];
    }
    final decoded = jsonDecode(response.body);
    final rawPicks = decoded is Map ? decoded['picks'] : null;
    if (rawPicks is! List) return const [];
    return rawPicks
        .map(RecentSearchPick.fromJson)
        .whereType<RecentSearchPick>()
        .toList(growable: false);
  } catch (error) {
    debugPrint('Proxy recent selected search lookup failed: $error');
    return const [];
  }
}

Future<void> _saveRecentSearchPicksToDisk(List<RecentSearchPick> picks) async {
  final file = await _recentSearchPicksFile();
  await file.writeAsString(
    jsonEncode(picks.map((pick) => pick.toJson()).toList(growable: false)),
  );
}

List<RecentSearchPick> _mergeRecentSearchPicks(
  Iterable<RecentSearchPick> primary,
  Iterable<RecentSearchPick> secondary, {
  required int limit,
}) {
  final output = <RecentSearchPick>[];
  final seen = <String>{};
  final all = <RecentSearchPick>[...primary, ...secondary]
    ..sort((a, b) => b.selectedAt.compareTo(a.selectedAt));
  for (final pick in all) {
    if (!seen.add(pick.key)) continue;
    output.add(pick);
    if (output.length >= limit) break;
  }
  return output;
}

Future<List<RecentSearchPick>> getRecentSearchPicks({int limit = 8}) async {
  final scopeId = activeStorageScopeId;
  final now = DateTime.now();
  final cacheAt = _recentSearchPickCacheAt;
  if (_recentSearchPickCacheScopeId == scopeId &&
      cacheAt != null &&
      now.difference(cacheAt) < _recentCloudSearchQueryCacheTtl &&
      _recentSearchPickCache.isNotEmpty) {
    return _recentSearchPickCache.take(limit).toList(growable: false);
  }
  final diskPicks = await _loadRecentSearchPicksFromDisk();
  final proxyPicks = await _loadRecentSearchPicksFromProxy(limit: limit);
  final picks = _mergeRecentSearchPicks(
    proxyPicks,
    diskPicks,
    limit: 24,
  );
  if (proxyPicks.isNotEmpty) {
    await _saveRecentSearchPicksToDisk(picks);
  }
  _recentSearchPickCache = picks;
  _recentSearchPickCacheAt = now;
  _recentSearchPickCacheScopeId = scopeId;
  return picks.take(limit).toList(growable: false);
}

Future<void> recordRecentSearchPick(
  String query,
  Map<String, dynamic> rawTrack,
) async {
  final trimmedQuery = query.trim();
  if (trimmedQuery.isEmpty || rawTrack.isEmpty) return;
  final pick = RecentSearchPick(
    query: trimmedQuery,
    track: normalizeTrack(Map<String, dynamic>.from(rawTrack)),
    selectedAt: DateTime.now(),
  );
  if (pick.track.isEmpty) return;
  final scopeId = activeStorageScopeId;
  final cachedMatchesScope = _recentSearchPickCacheScopeId == scopeId;
  final existing = cachedMatchesScope && _recentSearchPickCache.isNotEmpty
      ? _recentSearchPickCache
      : await _loadRecentSearchPicksFromDisk();
  final next = <RecentSearchPick>[pick];
  final seen = <String>{pick.key};
  for (final entry in existing) {
    if (!seen.add(entry.key)) continue;
    next.add(entry);
    if (next.length >= 24) break;
  }
  await _saveRecentSearchPicksToDisk(next);
  _recentSearchPickCache = next;
  _recentSearchPickCacheAt = DateTime.now();
  _recentSearchPickCacheScopeId = scopeId;
}

Future<void> removeRecentSearchPick(String key) async {
  final existing = await _loadRecentSearchPicksFromDisk();
  final next = existing.where((pick) => pick.key != key).toList(growable: false);
  await _saveRecentSearchPicksToDisk(next);
  _recentSearchPickCache = next;
  _recentSearchPickCacheAt = DateTime.now();
  _recentSearchPickCacheScopeId = activeStorageScopeId;
}

Future<List<String>> getRecentCloudSearchQueries({int limit = 8}) async {
  final userId = currentAuthenticatedUserId;
  final now = DateTime.now();
  final cachedAt = _recentCloudSearchQueryCacheAt;
  final cacheMatchesUser = userId != null &&
      userId.isNotEmpty &&
      _recentCloudSearchQueryCacheUserId == userId;
  if (cachedAt != null &&
      cacheMatchesUser &&
      now.difference(cachedAt) < _recentCloudSearchQueryCacheTtl &&
      _recentCloudSearchQueryCache.isNotEmpty) {
    return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
  }
  final client = supabaseClientOrNull;
  if (client == null || userId == null || userId.isEmpty) {
    return const [];
  }

  try {
    final queryFuture = client
        .from('search_events')
        .select('query,result_count,created_at')
        .eq('user_id', userId)
        .order('created_at', ascending: false)
        .limit(limit * 4);
    final rows = disableRecommendationTimeouts
        ? await queryFuture
        : await queryFuture.timeout(_recentCloudSearchQueryTimeout);
    if (rows.isEmpty) return const [];

    final seen = <String>{};
    final results = <String>[];
    for (final row in rows) {
      final query = row['query']?.toString().trim();
      if (query == null || query.isEmpty) continue;
      final normalized = query.toLowerCase();
      final createdAt = DateTime.tryParse(
        row['created_at']?.toString() ?? '',
      );
      if (createdAt != null) {
        _recentCloudSearchQueryTimes[normalized] = createdAt.toLocal();
      }
      if (!seen.add(normalized)) continue;
      results.add(normalized);
      if (results.length >= limit) {
        break;
      }
    }
    _recentCloudSearchQueryCache = results;
    _recentCloudSearchQueryCacheAt = now;
    _recentCloudSearchQueryCacheUserId = userId;
    return results;
  } catch (error) {
    debugPrint('Cloud search query lookup failed: $error');
    if (cacheMatchesUser && _recentCloudSearchQueryCache.isNotEmpty) {
      return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
    }
    return const [];
  }
}
