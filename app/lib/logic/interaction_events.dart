import 'dart:async';
import 'dart:convert';

import 'proxy_runtime.dart';
import 'track_metadata.dart';
import 'auth_provider.dart';

Map<String, dynamic>? _interactionEventTrackPayload(dynamic rawTrack) {
  if (rawTrack == null) return null;
  if (rawTrack is Map<String, dynamic>) {
    return normalizeTrack(Map<String, dynamic>.from(rawTrack));
  }
  if (rawTrack is Map) {
    return normalizeTrack(Map<String, dynamic>.from(rawTrack));
  }
  return null;
}

Future<void> recordCloudSearchEvent(
  String query, {
  required int resultCount,
}) async {
  final client = supabaseClientOrNull;
  final userId = currentAuthenticatedUserId;
  final trimmedQuery = query.trim();
  if (client == null ||
      userId == null ||
      userId.isEmpty ||
      trimmedQuery.isEmpty) {
    return;
  }
  try {
    await client.from('search_events').insert({
      'user_id': userId,
      'query': trimmedQuery,
      'result_count': resultCount,
    });
  } catch (_) {
    // Search event logging is best-effort.
  }
}

Future<void> recordProxyInteractionEvent(
  String eventType, {
  String? trackId,
  dynamic rawTrack,
  Map<String, dynamic>? metadata,
  DateTime? occurredAt,
}) async {
  final normalizedTrack = _interactionEventTrackPayload(rawTrack);
  final resolvedTrackId =
      (trackId ?? extractTrackId(normalizedTrack))?.trim() ?? '';
  if (resolvedTrackId.isEmpty) return;

  final payload = <String, dynamic>{
    if (normalizedTrack != null) ...normalizedTrack,
    if (metadata != null) ...metadata,
  };

  try {
    await proxyControlHttpClient
        .post(
          buildProxyUri('/interaction_event'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'user_scope_id': activeStorageScopeId,
            'track_id': resolvedTrackId,
            'event_type': eventType.trim().toLowerCase(),
            'artist_name': payload['channel'] ?? payload['artist'],
            'source': 'app',
            'occurred_at':
                (occurredAt ?? DateTime.now().toUtc()).millisecondsSinceEpoch /
                    1000.0,
            'metadata': payload,
          }),
        )
        .timeout(const Duration(seconds: 3));
  } catch (_) {
    // Keep playback/search interactions non-blocking if the proxy is unavailable.
  }
}

Future<void> recordProxySearchEvent(
  String query, {
  required int resultCount,
  String searchScope = 'track',
  Map<String, dynamic>? metadata,
  DateTime? occurredAt,
}) async {
  final trimmedQuery = query.trim();
  if (trimmedQuery.isEmpty) return;

  final payload = <String, dynamic>{
    'search_scope': searchScope.trim().isEmpty ? 'track' : searchScope.trim(),
    if (metadata != null) ...metadata,
  };

  try {
    await proxyControlHttpClient
        .post(
          buildProxyUri('/search_interaction'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'user_scope_id': activeStorageScopeId,
            'query': trimmedQuery,
            'result_count': resultCount,
            'source': 'app_${payload['search_scope']}_search',
            'occurred_at':
                (occurredAt ?? DateTime.now().toUtc()).millisecondsSinceEpoch /
                    1000.0,
            'metadata': payload,
          }),
        )
        .timeout(const Duration(seconds: 3));
  } catch (_) {
    // Search logging should never block the UI.
  }
}

final StreamController<String> _recommendationSignalController =
    StreamController<String>.broadcast();

Stream<String> get recommendationSignalStream =>
    _recommendationSignalController.stream;

void notifyRecommendationSignal([String reason = '']) {
  if (_recommendationSignalController.isClosed) return;
  _recommendationSignalController.add(reason);
}
