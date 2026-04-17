import 'package:flutter/foundation.dart';

import 'auth_provider.dart';
import 'proxy_runtime.dart';

const Duration _recentCloudSearchQueryTimeout = Duration(seconds: 2);
const Duration _recentCloudSearchQueryCacheTtl = Duration(seconds: 45);

List<String> _recentCloudSearchQueryCache = const [];
DateTime? _recentCloudSearchQueryCacheAt;
String _recentCloudSearchQueryCacheUserId = '';

List<String> peekRecentCloudSearchQueries({int limit = 8}) {
  return _recentCloudSearchQueryCache.take(limit).toList(growable: false);
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
