import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';
import 'cloud_search_queries.dart';
import 'proxy_runtime.dart';

Map<String, dynamic> buildSuggestRequestBody(
  Ref ref,
  String query, {
  required int limit,
  List<String> recentQueries = const <String>[],
}) {
  final normalizedQuery = query.trim();
  final storageScopeId = ref.read(authProvider).storageScopeId;
  return {
    'query': normalizedQuery,
    'limit': limit,
    'user_scope_id': storageScopeId,
    'recent_queries': recentQueries,
    'context_surface': 'suggest',
    'force_refresh': false,
  };
}

class SuggestNotifier extends StateNotifier<List<String>> {
  final Ref ref;
  int _requestVersion = 0;
  static const Duration _suggestDebounce = Duration(milliseconds: 160);

  SuggestNotifier(this.ref) : super(const <String>[]);

  Future<void> fetchSuggestions(String query) async {
    if (query.isEmpty) {
      state = const <String>[];
      return;
    }
    final requestVersion = ++_requestVersion;
    try {
      await Future<void>.delayed(_suggestDebounce);
      if (requestVersion != _requestVersion) return;
      final recentQueries = await getRecentCloudSearchQueries(limit: 4);
      if (requestVersion != _requestVersion) return;
      final body = buildSuggestRequestBody(
        ref,
        query,
        limit: 5,
        recentQueries: recentQueries,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/suggest'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 4));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        state = List<String>.from(jsonDecode(res.body)['results']);
      }
    } catch (_) {
      // Suggest remains best-effort and silent on failure.
    }
  }

  void clear() {
    _requestVersion++;
    state = const <String>[];
  }
}

final suggestProvider =
    StateNotifierProvider<SuggestNotifier, List<String>>((ref) {
  return SuggestNotifier(ref);
});
