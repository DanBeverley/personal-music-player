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
  List<Map<String, dynamic>> recentTracks = const <Map<String, dynamic>>[],
}) {
  final normalizedQuery = query.trim();
  final storageScopeId = ref.read(authProvider).storageScopeId;
  final lastPlayedTracks = recentTracks
      .take(8)
      .map((track) => Map<String, dynamic>.from(track))
      .toList(growable: false);
  return {
    'query': normalizedQuery,
    'limit': limit,
    'user_scope_id': storageScopeId,
    'recent_queries': recentQueries,
    'recent_tracks': lastPlayedTracks,
    'last_played_tracks': lastPlayedTracks,
    'context_surface': 'suggest',
    'force_refresh': false,
  };
}

class SearchSuggestion {
  const SearchSuggestion({
    required this.text,
    this.type = 'query',
    this.source = '',
    this.directPlay = false,
    this.track,
  });

  final String text;
  final String type;
  final String source;
  final bool directPlay;
  final Map<String, dynamic>? track;

  bool get isDirectPlayTrack => directPlay && track != null;

  factory SearchSuggestion.query(String text) => SearchSuggestion(text: text);

  factory SearchSuggestion.fromJson(dynamic value) {
    if (value is String) {
      return SearchSuggestion.query(value.trim());
    }
    if (value is! Map) {
      return const SearchSuggestion(text: '');
    }
    final map = Map<String, dynamic>.from(value);
    final text = (map['text'] ?? map['query'] ?? '').toString().trim();
    final rawTrack = map['track'];
    final track = rawTrack is Map
        ? Map<String, dynamic>.from(rawTrack)
        : null;
    return SearchSuggestion(
      text: text,
      type: (map['suggestion_type'] ?? map['type'] ?? 'query').toString(),
      source: (map['source_name'] ?? map['source'] ?? '').toString(),
      directPlay: map['direct_play'] == true || map['directPlay'] == true,
      track: track,
    );
  }
}

class SuggestNotifier extends StateNotifier<List<SearchSuggestion>> {
  final Ref ref;
  int _requestVersion = 0;
  bool _requestInFlight = false;
  String? _pendingQuery;
  List<Map<String, dynamic>> _pendingRecentTracks = const <Map<String, dynamic>>[];

  SuggestNotifier(this.ref) : super(const <SearchSuggestion>[]);

  List<SearchSuggestion> _localSuggestions(
    String query,
    List<String> recentQueries,
  ) {
    final normalized = query.trim();
    if (normalized.isEmpty) return const <SearchSuggestion>[];
    final needle = normalized.toLowerCase();
    final suggestions = <String>[];
    for (final recent in recentQueries) {
      final value = recent.trim();
      if (value.isEmpty) continue;
      final lower = value.toLowerCase();
      if ((lower.startsWith(needle) || lower.contains(needle)) &&
          !suggestions.any((entry) => entry.toLowerCase() == lower)) {
        suggestions.add(value);
      }
      if (suggestions.length >= 5) break;
    }
    if (!suggestions.any((entry) => entry.toLowerCase() == needle)) {
      suggestions.insert(0, normalized);
    }
    return suggestions
        .take(5)
        .map(SearchSuggestion.query)
        .toList(growable: false);
  }

  Future<void> fetchSuggestions(
    String query, {
    List<Map<String, dynamic>> recentTracks = const <Map<String, dynamic>>[],
  }) async {
    if (query.isEmpty) {
      state = const <SearchSuggestion>[];
      return;
    }
    final requestVersion = ++_requestVersion;
    if (_requestInFlight) {
      _pendingQuery = query;
      _pendingRecentTracks = recentTracks;
      return;
    }
    _requestInFlight = true;
    final recentQueries = peekRecentCloudSearchQueries(limit: 8);
    final localSuggestions = _localSuggestions(query, recentQueries);
    if (localSuggestions.isNotEmpty) {
      state = localSuggestions;
    }
    try {
      if (requestVersion != _requestVersion) return;
      final body = buildSuggestRequestBody(
        ref,
        query,
        limit: 5,
        recentQueries: recentQueries,
        recentTracks: recentTracks,
      );
      final res = await proxyControlHttpClient
          .post(
            buildProxyUri('/suggest'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body),
          )
          .timeout(const Duration(seconds: 2));
      if (requestVersion != _requestVersion) return;
      if (res.statusCode == 200) {
        final decoded = jsonDecode(res.body);
        final rawSuggestions = decoded is Map
            ? (decoded['suggestions'] ?? decoded['results'])
            : null;
        final remote = rawSuggestions is List
            ? rawSuggestions
                .map(SearchSuggestion.fromJson)
                .where((suggestion) => suggestion.text.isNotEmpty)
                .toList(growable: false)
            : const <SearchSuggestion>[];
        state = remote.isEmpty ? localSuggestions : remote;
      }
    } catch (_) {
      // Suggest remains best-effort and silent on failure.
    } finally {
      _requestInFlight = false;
      final pending = _pendingQuery;
      final pendingTracks = _pendingRecentTracks;
      _pendingQuery = null;
      _pendingRecentTracks = const <Map<String, dynamic>>[];
      if (pending != null) {
        unawaited(fetchSuggestions(pending, recentTracks: pendingTracks));
      }
    }
  }

  void clear() {
    _requestVersion++;
    _pendingQuery = null;
    _pendingRecentTracks = const <Map<String, dynamic>>[];
    state = const <SearchSuggestion>[];
  }
}

final suggestProvider =
    StateNotifierProvider<SuggestNotifier, List<SearchSuggestion>>((ref) {
  return SuggestNotifier(ref);
});
