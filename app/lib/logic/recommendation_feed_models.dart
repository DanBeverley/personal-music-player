import 'track_metadata.dart';

const Set<String> activeHomeFeedRowKinds = <String>{
  'todays_pick',
  'featured_new_albums',
  'last_played',
  'frequently_listened',
  'made_for_you',
  'because_you_played',
  'trending_by_genre',
  'recommended_albums',
  'recommended_artists',
  'quiet_picks',
  'hidden_gems',
};

bool isActiveHomeFeedRowKind(String rowKind) =>
    activeHomeFeedRowKinds.contains(rowKind.trim());

String homeFeedRowKindFromJson(Map<String, dynamic> row) =>
    (row['kind'] ?? row['id'] ?? '').toString().trim();

List<Map<String, dynamic>> filterActiveHomeFeedRows(
  Iterable<dynamic> rows,
) {
  return rows.whereType<Map>().where((row) {
    final typedRow = Map<String, dynamic>.from(row);
    return isActiveHomeFeedRowKind(homeFeedRowKindFromJson(typedRow));
  }).map((row) => Map<String, dynamic>.from(row)).toList(growable: false);
}

Map<String, dynamic> filterActiveHomeFeedDiagnostics(
  Map<String, dynamic> diagnostics,
) {
  Map<String, dynamic>? filterKeyedMap(dynamic value) {
    if (value is! Map) return null;
    final filtered = <String, dynamic>{};
    for (final entry in value.entries) {
      final key = entry.key?.toString() ?? '';
      if (isActiveHomeFeedRowKind(key)) {
        filtered[key] = entry.value;
      }
    }
    return filtered;
  }

  List<dynamic>? filterRowKindList(dynamic value) {
    if (value is! List) return null;
    return value
        .where((entry) => isActiveHomeFeedRowKind(entry.toString()))
        .toList(growable: false);
  }

  final filtered = Map<String, dynamic>.from(diagnostics);
  final rowStatus = filterKeyedMap(filtered['row_status']);
  if (rowStatus != null) filtered['row_status'] = rowStatus;
  final rowItemCounts = filterKeyedMap(filtered['row_item_counts']);
  if (rowItemCounts != null) filtered['row_item_counts'] = rowItemCounts;
  final rowQualityReasons = filterKeyedMap(filtered['row_quality_reasons']);
  if (rowQualityReasons != null) {
    filtered['row_quality_reasons'] = rowQualityReasons;
  }
  final rowOrder = filterRowKindList(filtered['row_order']);
  if (rowOrder != null) filtered['row_order'] = rowOrder;
  final deferredRowKinds = filterRowKindList(filtered['deferred_row_kinds']);
  if (deferredRowKinds != null) {
    filtered['deferred_row_kinds'] = deferredRowKinds;
  }
  return filtered;
}

Map<String, dynamic> filterActiveHomeFeedPayload(
  Map<String, dynamic> payload,
) {
  final filtered = Map<String, dynamic>.from(payload);
  final rows = filtered['rows'];
  filtered['rows'] = filterActiveHomeFeedRows(
    rows is List ? rows : const <dynamic>[],
  );
  final diagnostics = filtered['diagnostics'];
  if (diagnostics is Map) {
    filtered['diagnostics'] = filterActiveHomeFeedDiagnostics(
      Map<String, dynamic>.from(diagnostics),
    );
  }
  return filtered;
}

RecommendationFeedState filterActiveHomeFeedState(
  RecommendationFeedState state,
) {
  final activeRows = state.rows
      .where((row) =>
          isActiveHomeFeedRowKind(row.kind) || isActiveHomeFeedRowKind(row.id))
      .toList(growable: false);
  return state.copyWith(
    rows: activeRows,
    diagnostics: filterActiveHomeFeedDiagnostics(state.diagnostics),
  );
}

RecommendationFeedRowState? activeHomeFeedRowFromJson(
  Map<dynamic, dynamic> json,
) {
  final typedJson = Map<String, dynamic>.from(json);
  final rowKind = homeFeedRowKindFromJson(typedJson);
  if (!isActiveHomeFeedRowKind(rowKind)) {
    return null;
  }
  return RecommendationFeedRowState.fromJson(typedJson);
}

class RecommendationFeedRowState {
  final String id;
  final String title;
  final String kind;
  final String itemType;
  final String rowStyle;
  final Map<String, dynamic> meta;
  final List<Map<String, dynamic>> items;
  final int nextOffset;
  final bool hasMore;

  const RecommendationFeedRowState({
    required this.id,
    required this.title,
    required this.kind,
    required this.itemType,
    required this.rowStyle,
    required this.meta,
    required this.items,
    required this.nextOffset,
    required this.hasMore,
  });

  factory RecommendationFeedRowState.fromJson(Map<String, dynamic> json) {
    final kind = (json['kind'] ?? json['id'] ?? 'tracks').toString();
    final inferredItemType = kind == 'recommended_albums' ||
            kind == 'featured_new_albums'
        ? 'album'
        : kind == 'recommended_artists'
            ? 'artist'
            : 'track';
    final itemType = (json['item_type'] ?? inferredItemType).toString();
    final rowStyle = (json['row_style'] ?? '').toString();
    final rawItems = (json['items'] as List<dynamic>? ?? const []);
    final items = rawItems.whereType<Map>().map((entry) {
      final map = Map<String, dynamic>.from(entry);
      if (itemType != 'track') {
        return map;
      }
      return normalizeTrack(map);
    }).where((item) {
      if (itemType == 'album') {
        final albumId = item['id']?.toString().trim() ?? '';
        final albumTitle = item['title']?.toString().trim() ?? '';
        return albumId.isNotEmpty || albumTitle.isNotEmpty;
      }
      if (itemType == 'artist') {
        final artistId = item['id']?.toString().trim() ?? '';
        final artistName = item['name']?.toString().trim() ?? '';
        return artistId.isNotEmpty || artistName.isNotEmpty;
      }
      if (itemType == 'mix') {
        final mixId = item['id']?.toString().trim() ?? '';
        final mixTitle = item['title']?.toString().trim() ?? '';
        return mixId.isNotEmpty || mixTitle.isNotEmpty;
      }
      return extractTrackId(item)?.isNotEmpty ?? false;
    }).toList(growable: false);
    return RecommendationFeedRowState(
      id: (json['id'] ?? kind).toString(),
      title: (json['title'] ?? 'Recommended').toString(),
      kind: kind,
      itemType: itemType,
      rowStyle: rowStyle,
      meta: json['meta'] is Map
          ? Map<String, dynamic>.from(json['meta'] as Map)
          : const <String, dynamic>{},
      items: items,
      nextOffset: (json['next_offset'] as num?)?.toInt() ?? items.length,
      hasMore: json['has_more'] == true,
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'kind': kind,
        'item_type': itemType,
        'row_style': rowStyle,
        if (meta.isNotEmpty) 'meta': meta,
        'items': items,
        'next_offset': nextOffset,
        'has_more': hasMore,
      };

  bool get isPending => (meta['loading_state']?.toString() ?? '') == 'pending';


  bool get isPartialReady => meta['partial_ready'] == true;

  String get rowState => meta['row_state']?.toString().trim() ?? '';

  int get rowVersion => (meta['row_version'] as num?)?.toInt() ?? 0;

  bool get isRefinementActive =>
      meta['refinement_active'] == true || rowState == 'partial_inflight';

  bool get isUnavailable => rowState == 'unavailable';

  int get refineAfterMs => (meta['refine_after_ms'] as num?)?.toInt() ?? 0;

  String get loadingLabel => meta['loading_label']?.toString().trim() ?? '';

  String get loadingMessage => meta['loading_message']?.toString().trim() ?? '';

  RecommendationFeedRowState copyWith({
    String? id,
    String? title,
    String? kind,
    String? itemType,
    String? rowStyle,
    Map<String, dynamic>? meta,
    List<Map<String, dynamic>>? items,
    int? nextOffset,
    bool? hasMore,
  }) {
    return RecommendationFeedRowState(
      id: id ?? this.id,
      title: title ?? this.title,
      kind: kind ?? this.kind,
      itemType: itemType ?? this.itemType,
      rowStyle: rowStyle ?? this.rowStyle,
      meta: meta ?? this.meta,
      items: items ?? this.items,
      nextOffset: nextOffset ?? this.nextOffset,
      hasMore: hasMore ?? this.hasMore,
    );
  }
}

class RecommendationFeedState {
  final String requestState;
  final String sessionId;
  final List<RecommendationFeedRowState> rows;
  final double? generatedAt;
  final double? expiresAt;
  final String? errorMessage;
  final Map<String, dynamic> diagnostics;

  const RecommendationFeedState({
    this.requestState = 'idle',
    this.sessionId = '',
    this.rows = const [],
    this.generatedAt,
    this.expiresAt,
    this.errorMessage,
    this.diagnostics = const <String, dynamic>{},
  });

  bool get isEmpty => rows.every((row) => row.items.isEmpty);
  bool get hasRows => rows.any((row) => row.items.isNotEmpty);

  List<Map<String, dynamic>> get visibleTracks => rows
      .where((row) => row.itemType == 'track')
      .expand((row) => row.items)
      .toList(growable: false);

  factory RecommendationFeedState.fromJson(Map<String, dynamic> json) {
    final rawRows = (json['rows'] as List<dynamic>? ?? const []);
    return RecommendationFeedState(
      requestState: (json['request_state'] ?? '').toString().trim().isNotEmpty
          ? json['request_state'].toString()
          : ((json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? 'failed'
              : 'complete'),
      sessionId: (json['session_id'] ?? '').toString(),
      rows: rawRows
          .whereType<Map>()
          .map((row) => RecommendationFeedRowState.fromJson(
              Map<String, dynamic>.from(row)))
          .toList(growable: false),
      generatedAt: (json['generated_at'] as num?)?.toDouble(),
      expiresAt: (json['expires_at'] as num?)?.toDouble(),
      errorMessage:
          (json['error_message']?.toString().trim().isNotEmpty ?? false)
              ? json['error_message'].toString().trim()
              : null,
      diagnostics: json['diagnostics'] is Map
          ? Map<String, dynamic>.from(json['diagnostics'] as Map)
          : const <String, dynamic>{},
    );
  }

  factory RecommendationFeedState.fromHomeJson(Map<String, dynamic> json) {
    return RecommendationFeedState.fromJson(filterActiveHomeFeedPayload(json));
  }

  Map<String, dynamic> toJson() => {
        'request_state': requestState,
        'session_id': sessionId,
        'generated_at': generatedAt,
        'expires_at': expiresAt,
        'rows': rows.map((row) => row.toJson()).toList(growable: false),
        if (errorMessage != null) 'error_message': errorMessage,
        if (diagnostics.isNotEmpty) 'diagnostics': diagnostics,
      };

  RecommendationFeedState copyWith({
    String? requestState,
    String? sessionId,
    List<RecommendationFeedRowState>? rows,
    double? generatedAt,
    double? expiresAt,
    String? errorMessage,
    Map<String, dynamic>? diagnostics,
    bool clearError = false,
  }) {
    return RecommendationFeedState(
      requestState: requestState ?? this.requestState,
      sessionId: sessionId ?? this.sessionId,
      rows: rows ?? this.rows,
      generatedAt: generatedAt ?? this.generatedAt,
      expiresAt: expiresAt ?? this.expiresAt,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
      diagnostics: diagnostics ?? this.diagnostics,
    );
  }
}

String recommendationRowItemKey(
  String itemType,
  Map<String, dynamic> item,
) {
  if (itemType == 'album') {
    final albumId = item['id']?.toString().trim() ?? '';
    if (albumId.isNotEmpty) return 'album:$albumId';
    final title = item['title']?.toString().trim().toLowerCase() ?? '';
    final artist = item['artist']?.toString().trim().toLowerCase() ?? '';
    return 'album:$title|$artist';
  }
  if (itemType == 'artist') {
    final artistId = item['id']?.toString().trim() ?? '';
    if (artistId.isNotEmpty) return 'artist:$artistId';
    final name = item['name']?.toString().trim().toLowerCase() ?? '';
    return 'artist:$name';
  }
  if (itemType == 'mix') {
    final mixId = item['id']?.toString().trim() ?? '';
    if (mixId.isNotEmpty) return 'mix:$mixId';
    final title = item['title']?.toString().trim().toLowerCase() ?? '';
    return 'mix:$title';
  }
  final trackId = extractTrackId(item)?.trim() ?? '';
  if (trackId.isNotEmpty) return 'track:$trackId';
  final title = item['title']?.toString().trim().toLowerCase() ?? '';
  final artist = (item['channel'] ?? item['author'] ?? item['artist'])
          ?.toString()
          .trim()
          .toLowerCase() ??
      '';
  return 'track:$title|$artist';
}
