import 'dart:convert';
import 'dart:io';

import 'package:path_provider/path_provider.dart';

import 'proxy_runtime.dart';
import 'recommendation_feed_models.dart';

const int homeFeedCacheVersion = 10;
const Duration homeFeedCacheTtl = Duration(hours: 18);

String _safeCacheSegment(String value) =>
    value.replaceAll(RegExp(r'[^A-Za-z0-9_\-]'), '_');

Future<File> _homeFeedCacheFile(String scopeId) async {
  final dir = await getApplicationDocumentsDirectory();
  final scope = _safeCacheSegment(scopeId.trim().isNotEmpty ? scopeId : 'guest');
  return File('${dir.path}/users/$scope/home_feed_cache.json');
}

bool shouldCacheHomeFeed(RecommendationFeedState feedState) {
  const activeActions = <String>{
    'served_active',
    'promoted_prepared',
    'built_and_promoted',
  };
  final failedContracts = feedState.diagnostics['artifact_failed_contracts'];
  return feedState.hasRows &&
      feedState.feedVersion > 0 &&
      activeActions.contains(feedState.feedAction) &&
      feedState.diagnostics['feed_state_version'] == 'feed-state-v2' &&
      feedState.diagnostics['artifact_status'] == 'servable' &&
      (failedContracts is! List || failedContracts.isEmpty);
}

Future<RecommendationFeedState?> loadCachedHomeFeed(String scopeId) async {
  try {
    final file = await _homeFeedCacheFile(scopeId);
    if (!file.existsSync()) return null;
    final raw = (await file.readAsString()).trim();
    if (raw.isEmpty) return null;
    final decoded = jsonDecode(raw);
    if (decoded is! Map) return null;
    final envelope = Map<String, dynamic>.from(decoded);
    if ((envelope['version'] as num?)?.toInt() != homeFeedCacheVersion) {
      return null;
    }
    if ((envelope['scope_id'] ?? '').toString() != scopeId) return null;
    final cachedAtMs = (envelope['cached_at_ms'] as num?)?.toInt() ?? 0;
    if (cachedAtMs <= 0) return null;
    final age = DateTime.now().millisecondsSinceEpoch - cachedAtMs;
    if (age > homeFeedCacheTtl.inMilliseconds) return null;
    final payload = envelope['feed'];
    if (payload is! Map) return null;
    final cachedState = RecommendationFeedState.fromHomeJson(
      Map<String, dynamic>.from(payload),
    );
    return shouldCacheHomeFeed(cachedState) ? cachedState : null;
  } catch (error) {
    debugProxyLog('recommend', 'home feed cache read failed=$error');
    return null;
  }
}

Future<void> storeCachedHomeFeed({
  required String scopeId,
  required RecommendationFeedState feedState,
}) async {
  final activeFeedState = filterActiveHomeFeedState(feedState);
  if (!shouldCacheHomeFeed(activeFeedState)) return;
  try {
    final file = await _homeFeedCacheFile(scopeId);
    if (!file.parent.existsSync()) {
      file.parent.createSync(recursive: true);
    }
    final cacheableState = activeFeedState.copyWith(
      requestState: 'complete',
      clearError: true,
      diagnostics: {
        ...activeFeedState.diagnostics,
        'client_cached_at_ms': DateTime.now().millisecondsSinceEpoch,
      },
    );
    final envelope = <String, dynamic>{
      'version': homeFeedCacheVersion,
      'scope_id': scopeId,
      'cached_at_ms': DateTime.now().millisecondsSinceEpoch,
      'feed': cacheableState.toJson(),
    };
    await file.writeAsString(jsonEncode(envelope), flush: true);
  } catch (error) {
    debugProxyLog('recommend', 'home feed cache write failed=$error');
  }
}
