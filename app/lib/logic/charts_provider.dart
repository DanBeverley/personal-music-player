import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'proxy_runtime.dart';
import 'track_metadata.dart';

class ChartQuery {
  const ChartQuery({
    this.country = 'GLOBAL',
    this.range = 'week',
    this.source = 'all',
  });

  final String country;
  final String range;
  final String source;

  @override
  bool operator ==(Object other) =>
      other is ChartQuery &&
      country == other.country &&
      range == other.range &&
      source == other.source;

  @override
  int get hashCode => Object.hash(country, range, source);
}

class ChartHistoryPoint {
  const ChartHistoryPoint({required this.at, required this.rank});

  final DateTime at;
  final int rank;

  factory ChartHistoryPoint.fromJson(Map<String, dynamic> json) {
    final seconds = (json['at'] as num?)?.toDouble() ?? 0;
    return ChartHistoryPoint(
      at: DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()),
      rank: (json['rank'] as num?)?.toInt() ?? 0,
    );
  }
}

class ChartEntry {
  const ChartEntry({
    required this.rank,
    required this.previousRank,
    required this.rankChange,
    required this.isNewEntry,
    required this.title,
    required this.artist,
    required this.thumbnail,
    required this.playableSourceId,
    required this.listenCount,
    required this.listenerCount,
    required this.viewCount,
    required this.providers,
    required this.history,
    required this.raw,
  });

  final int rank;
  final int? previousRank;
  final int? rankChange;
  final bool isNewEntry;
  final String title;
  final String artist;
  final String thumbnail;
  final String playableSourceId;
  final int listenCount;
  final int listenerCount;
  final int viewCount;
  final List<String> providers;
  final List<ChartHistoryPoint> history;
  final Map<String, dynamic> raw;

  bool get isPlayable => playableSourceId.isNotEmpty;

  Map<String, dynamic> get track => normalizeTrack(<String, dynamic>{
        ...raw,
        'id': playableSourceId,
        'videoId': playableSourceId,
        'playable_source_id': playableSourceId,
        'title': title,
        'artist': artist,
        'channel': artist,
        'thumbnail': thumbnail,
        'playable': true,
      });

  factory ChartEntry.fromJson(Map<String, dynamic> json) {
    String text(Object? value) => value?.toString().trim() ?? '';
    int count(Object? value) => int.tryParse('${value ?? 0}') ?? 0;
    final sourceId = text(
      json['playable_source_id'] ?? json['videoId'] ?? json['id'],
    );
    final history = (json['history'] as List? ?? const <dynamic>[])
        .whereType<Map>()
        .map((point) => ChartHistoryPoint.fromJson(
              Map<String, dynamic>.from(point),
            ))
        .where((point) => point.rank > 0)
        .toList(growable: false);
    return ChartEntry(
      rank: count(json['rank']),
      previousRank:
          json['previous_rank'] == null ? null : count(json['previous_rank']),
      rankChange:
          json['rank_change'] == null ? null : count(json['rank_change']),
      isNewEntry: json['is_new_entry'] == true,
      title: text(json['title'] ?? json['track_name']),
      artist: text(json['artist'] ?? json['artist_name'] ?? json['channel']),
      thumbnail: text(json['thumbnail']),
      playableSourceId: sourceId,
      listenCount: count(json['listen_count']),
      listenerCount: count(json['listener_count']),
      viewCount: count(json['view_count']),
      providers: (json['chart_providers'] as List? ?? const <dynamic>[])
          .map(text)
          .where((value) => value.isNotEmpty)
          .toList(growable: false),
      history: history,
      raw: Map<String, dynamic>.from(json),
    );
  }
}

class ChartsSnapshot {
  const ChartsSnapshot({
    required this.status,
    required this.country,
    required this.range,
    required this.source,
    required this.generatedAt,
    required this.providerStatus,
    required this.refreshing,
    required this.totalCount,
    required this.entries,
  });

  final String status;
  final String country;
  final String range;
  final String source;
  final DateTime? generatedAt;
  final Map<String, dynamic> providerStatus;
  final bool refreshing;
  final int totalCount;
  final List<ChartEntry> entries;

  bool get isWarming => status == 'warming' && entries.isEmpty;
  bool get isUnavailable => status == 'unavailable' && entries.isEmpty;
  bool get isRefreshing => refreshing || status == 'stale';

  factory ChartsSnapshot.fromJson(Map<String, dynamic> json) {
    final generatedSeconds = (json['generated_at'] as num?)?.toDouble() ?? 0;
    return ChartsSnapshot(
      status: json['status']?.toString() ?? 'warming',
      country: json['country']?.toString() ?? 'GLOBAL',
      range: json['range']?.toString() ?? 'week',
      source: json['source']?.toString() ?? 'all',
      generatedAt: generatedSeconds <= 0
          ? null
          : DateTime.fromMillisecondsSinceEpoch(
              (generatedSeconds * 1000).round(),
            ),
      providerStatus: json['provider_status'] is Map
          ? Map<String, dynamic>.from(json['provider_status'] as Map)
          : const <String, dynamic>{},
      refreshing: json['refreshing'] == true,
      totalCount: (json['total_count'] as num?)?.toInt() ?? 0,
      entries: (json['entries'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((entry) => ChartEntry.fromJson(
                Map<String, dynamic>.from(entry),
              ))
          .where((entry) =>
              entry.rank > 0 &&
              entry.title.isNotEmpty &&
              entry.artist.isNotEmpty &&
              entry.isPlayable)
          .toList(growable: false),
    );
  }
}

final chartsProvider = FutureProvider.autoDispose
    .family<ChartsSnapshot, ChartQuery>((ref, query) async {
  final uri =
      buildProxyUri('/charts').replace(queryParameters: <String, String>{
    'country': query.country,
    'range': query.range,
    'source': query.source,
    'limit': '60',
  });
  final response = await appHttpClient.get(uri);
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw StateError('Charts service returned ${response.statusCode}.');
  }
  final payload = jsonDecode(response.body);
  if (payload is! Map) {
    throw const FormatException('Charts response is not an object.');
  }
  return ChartsSnapshot.fromJson(Map<String, dynamic>.from(payload));
});
