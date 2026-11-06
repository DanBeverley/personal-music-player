import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'proxy_runtime.dart';

class NewReleaseEntry {
  const NewReleaseEntry({
    required this.id,
    required this.title,
    required this.artist,
    required this.thumbnail,
    required this.releaseDate,
    required this.year,
    required this.providers,
    required this.raw,
  });

  final String id;
  final String title;
  final String artist;
  final String thumbnail;
  final String releaseDate;
  final String year;
  final List<String> providers;
  final Map<String, dynamic> raw;

  factory NewReleaseEntry.fromJson(Map<String, dynamic> json) {
    String text(Object? value) => value?.toString().trim() ?? '';
    return NewReleaseEntry(
      id: text(json['id'] ?? json['browseId']),
      title: text(json['title'] ?? json['name']),
      artist: text(json['artist']),
      thumbnail: text(json['thumbnail']),
      releaseDate: text(json['release_date']),
      year: text(json['year']),
      providers: (json['release_providers'] as List? ?? const <dynamic>[])
          .map(text)
          .where((value) => value.isNotEmpty)
          .toList(growable: false),
      raw: Map<String, dynamic>.from(json),
    );
  }
}

class NewReleasesSnapshot {
  const NewReleasesSnapshot({
    required this.status,
    required this.generatedAt,
    required this.entries,
    this.refreshStatus = '',
  });

  final String status;
  final DateTime? generatedAt;
  final List<NewReleaseEntry> entries;
  final String refreshStatus;

  factory NewReleasesSnapshot.fromJson(Map<String, dynamic> json) {
    final seconds = (json['generated_at'] as num?)?.toDouble() ?? 0;
    return NewReleasesSnapshot(
      status: json['status']?.toString() ?? 'unavailable',
      refreshStatus: json['refresh'] is Map
          ? (json['refresh']['refresh_status']?.toString() ?? '') : '',
      generatedAt: seconds <= 0
          ? null
          : DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()),
      entries: (json['entries'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((value) => NewReleaseEntry.fromJson(
                Map<String, dynamic>.from(value),
              ))
          .where((value) =>
              value.id.isNotEmpty &&
              value.title.isNotEmpty &&
              value.artist.isNotEmpty)
          .toList(growable: false),
    );
  }
}

final newReleasesProvider = FutureProvider.autoDispose
    .family<NewReleasesSnapshot, bool>((ref, refresh) async {
  final requestLifetime = ref.keepAlive();
  try {
    var disposed = false;
    ref.onDispose(() => disposed = true);
    for (var attempt = 0; attempt < 40; attempt++) {
      final uri = buildProxyUri('/new_releases').replace(
        queryParameters: <String, String>{
          'limit': '120',
          if (refresh && attempt == 0) 'refresh': 'true',
        },
      );
      final response =
          await appHttpClient.get(uri).timeout(const Duration(seconds: 15));
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw StateError(
            'New releases service returned ${response.statusCode}.');
      }
      final payload = jsonDecode(response.body);
      if (payload is! Map) {
        throw const FormatException('New releases response is not an object.');
      }
      if (disposed) throw StateError('Release screen closed.');
      if (payload['status'] == 'preparing' || payload['refreshing'] == true) {
        await Future<void>.delayed(const Duration(seconds: 3));
        if (disposed) throw StateError('Release screen closed.');
        continue;
      }
      return NewReleasesSnapshot.fromJson(Map<String, dynamic>.from(payload));
    }
    throw StateError(
        'Release preparation is taking longer than expected. Retry shortly.');
  } finally {
    requestLifetime.close();
  }
});
