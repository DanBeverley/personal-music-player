import 'track_metadata.dart';

class AssistantSessionSummary {
  final String id;
  final String title;
  final String lastMessagePreview;
  final String lastMode;
  final DateTime? updatedAt;
  final DateTime? archivedAt;
  final DateTime? pinnedAt;

  const AssistantSessionSummary({
    required this.id,
    required this.title,
    this.lastMessagePreview = '',
    this.lastMode = '',
    this.updatedAt,
    this.archivedAt,
    this.pinnedAt,
  });

  factory AssistantSessionSummary.fromJson(Map<String, dynamic> json) {
    return AssistantSessionSummary(
      id: json['id']?.toString() ?? '',
      title: json['title']?.toString() ?? 'New chat',
      lastMessagePreview: json['last_message_preview']?.toString() ?? '',
      lastMode: json['last_mode']?.toString() ?? '',
      updatedAt: _assistantParseDateTime(json['updated_at']),
      archivedAt: _assistantParseDateTime(json['archived_at']),
      pinnedAt: _assistantParseDateTime(json['pinned_at']),
    );
  }

  AssistantSessionSummary copyWith({
    String? id,
    String? title,
    String? lastMessagePreview,
    String? lastMode,
    DateTime? updatedAt,
    DateTime? archivedAt,
    DateTime? pinnedAt,
  }) {
    return AssistantSessionSummary(
      id: id ?? this.id,
      title: title ?? this.title,
      lastMessagePreview: lastMessagePreview ?? this.lastMessagePreview,
      lastMode: lastMode ?? this.lastMode,
      updatedAt: updatedAt ?? this.updatedAt,
      archivedAt: archivedAt ?? this.archivedAt,
      pinnedAt: pinnedAt ?? this.pinnedAt,
    );
  }

  bool get isArchived => archivedAt != null;
  bool get isPinned => pinnedAt != null;
}

class AssistantPlaylistSummary {
  final String id;
  final String name;
  final int trackCount;

  const AssistantPlaylistSummary({
    required this.id,
    required this.name,
    required this.trackCount,
  });

  factory AssistantPlaylistSummary.fromJson(Map<String, dynamic> json) {
    return AssistantPlaylistSummary(
      id: json['id']?.toString() ?? '',
      name: json['name']?.toString() ?? 'Playlist',
      trackCount: _assistantParseInt(json['track_count']),
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'track_count': trackCount,
      };
}

class AssistantTrackCard {
  final Map<String, dynamic> track;
  final String? reason;

  const AssistantTrackCard({
    required this.track,
    this.reason,
  });

  factory AssistantTrackCard.fromJson(Map<String, dynamic> json) {
    return AssistantTrackCard(
      track: normalizeTrack(json),
      reason: json['reason']?.toString(),
    );
  }

  Map<String, dynamic> toContextJson() => {
        'id': extractTrackId(track),
        'title': track['title']?.toString() ?? '',
        'artist': (track['channel'] ?? track['artist'] ?? track['author'] ?? '')
            .toString(),
        'album': track['album']?.toString(),
        'thumbnail': track['thumbnail']?.toString(),
        'duration': _assistantParseInt(track['duration']),
        if (reason != null && reason!.trim().isNotEmpty) 'reason': reason,
      };
}

class AssistantPlaylistDraft {
  final String name;
  final String summary;
  final List<AssistantTrackCard> tracks;

  const AssistantPlaylistDraft({
    required this.name,
    required this.summary,
    required this.tracks,
  });

  factory AssistantPlaylistDraft.fromJson(Map<String, dynamic> json) {
    final tracks = (json['tracks'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantTrackCard.fromJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);
    return AssistantPlaylistDraft(
      name: json['name']?.toString() ?? 'Neatie Mix',
      summary: json['summary']?.toString() ?? '',
      tracks: tracks,
    );
  }
}

class AssistantSourceLink {
  final String label;
  final String url;

  const AssistantSourceLink({
    required this.label,
    required this.url,
  });

  factory AssistantSourceLink.fromJson(Map<String, dynamic> json) {
    return AssistantSourceLink(
      label: json['label']?.toString() ?? 'Source',
      url: json['url']?.toString() ?? '',
    );
  }
}

class AssistantFactCard {
  final String id;
  final String kind;
  final String title;
  final String value;
  final String subtitle;
  final List<String> metadata;
  final String? sourceLabel;
  final String? sourceUrl;

  const AssistantFactCard({
    required this.id,
    required this.kind,
    required this.title,
    required this.value,
    required this.subtitle,
    this.metadata = const [],
    this.sourceLabel,
    this.sourceUrl,
  });

  factory AssistantFactCard.fromJson(Map<String, dynamic> json) {
    return AssistantFactCard(
      id: json['id']?.toString() ?? '',
      kind: json['kind']?.toString() ?? 'fact',
      title: json['title']?.toString() ?? 'Fact',
      value: json['value']?.toString() ?? '',
      subtitle: json['subtitle']?.toString() ?? '',
      metadata: (json['metadata'] as List<dynamic>? ?? const [])
          .map((entry) => entry.toString())
          .where((entry) => entry.trim().isNotEmpty)
          .toList(growable: false),
      sourceLabel: json['source_label']?.toString(),
      sourceUrl: json['source_url']?.toString(),
    );
  }
}

class AssistantClarificationOption {
  final String id;
  final String kind;
  final String label;
  final String value;
  final String description;

  const AssistantClarificationOption({
    required this.id,
    required this.kind,
    required this.label,
    required this.value,
    this.description = '',
  });

  factory AssistantClarificationOption.fromJson(Map<String, dynamic> json) {
    return AssistantClarificationOption(
      id: json['id']?.toString() ?? '',
      kind: json['kind']?.toString() ?? 'option',
      label: json['label']?.toString() ?? 'Option',
      value: json['value']?.toString() ?? '',
      description: json['description']?.toString() ?? '',
    );
  }
}

class AssistantMessage {
  final String id;
  final String role;
  final String text;
  final List<AssistantTrackCard> tracks;
  final AssistantPlaylistDraft? playlistDraft;
  final AssistantPlaylistSummary? targetPlaylist;
  final List<AssistantPlaylistSummary> playlistOptions;
  final List<AssistantFactCard> factCards;
  final List<AssistantSourceLink> sourceLinks;
  final List<AssistantClarificationOption> clarificationOptions;
  final String? followUpQuestion;
  final String? actionType;
  final String? mode;

  const AssistantMessage({
    required this.id,
    required this.role,
    required this.text,
    this.tracks = const [],
    this.playlistDraft,
    this.targetPlaylist,
    this.playlistOptions = const [],
    this.factCards = const [],
    this.sourceLinks = const [],
    this.clarificationOptions = const [],
    this.followUpQuestion,
    this.actionType,
    this.mode,
  });

  factory AssistantMessage.fromStoredJson(Map<String, dynamic> json) {
    final role = json['role']?.toString() ?? 'assistant';
    final payload = json['payload'] is Map
        ? Map<String, dynamic>.from(json['payload'] as Map)
        : <String, dynamic>{};
    if (role != 'assistant') {
      return AssistantMessage(
        id: json['id']?.toString() ?? assistantMessageId(),
        role: role,
        text: json['content']?.toString() ?? '',
      );
    }
    return AssistantMessage.fromPayload(
      id: json['id']?.toString() ?? assistantMessageId(),
      payload: payload,
      fallbackText: json['content']?.toString(),
    );
  }

  factory AssistantMessage.fromPayload({
    required String id,
    required Map<String, dynamic> payload,
    String? fallbackText,
  }) {
    final tracks = (payload['tracks'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantTrackCard.fromJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);

    final playlistDraft = payload['playlist_draft'] is Map
        ? AssistantPlaylistDraft.fromJson(
            Map<String, dynamic>.from(payload['playlist_draft'] as Map),
          )
        : null;
    final targetPlaylist = payload['target_playlist'] is Map
        ? AssistantPlaylistSummary.fromJson(
            Map<String, dynamic>.from(payload['target_playlist'] as Map),
          )
        : null;
    final playlistOptions =
        (payload['playlist_options'] as List<dynamic>? ?? const [])
            .map((entry) => AssistantPlaylistSummary.fromJson(
                  Map<String, dynamic>.from(entry as Map),
                ))
            .toList(growable: false);
    final factCards = (payload['fact_cards'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantFactCard.fromJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);
    final sourceLinks = (payload['source_links'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantSourceLink.fromJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);
    final clarificationOptions =
        (payload['clarification_options'] as List<dynamic>? ?? const [])
            .map((entry) => AssistantClarificationOption.fromJson(
                  Map<String, dynamic>.from(entry as Map),
                ))
            .toList(growable: false);

    return AssistantMessage(
      id: id,
      role: 'assistant',
      text: payload['reply']?.toString() ??
          fallbackText ??
          'I found a few places to start.',
      tracks: tracks,
      playlistDraft: playlistDraft,
      targetPlaylist: targetPlaylist,
      playlistOptions: playlistOptions,
      factCards: factCards,
      sourceLinks: sourceLinks,
      clarificationOptions: clarificationOptions,
      followUpQuestion: payload['follow_up_question']?.toString(),
      actionType: payload['action_type']?.toString(),
      mode: payload['mode']?.toString(),
    );
  }

  Map<String, dynamic> toConversationJson() => {
        'role': role,
        'content': text,
      };
}

int _assistantParseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

String assistantMessageId() => DateTime.now().microsecondsSinceEpoch.toString();

DateTime? _assistantParseDateTime(dynamic value) {
  if (value == null) return null;
  if (value is num) {
    return DateTime.fromMillisecondsSinceEpoch(
      (value.toDouble() * 1000).round(),
      isUtc: true,
    ).toLocal();
  }
  final text = value.toString().trim();
  if (text.isEmpty) return null;
  final numeric = num.tryParse(text);
  if (numeric != null) {
    return DateTime.fromMillisecondsSinceEpoch(
      (numeric.toDouble() * 1000).round(),
      isUtc: true,
    ).toLocal();
  }
  return DateTime.tryParse(text)?.toLocal();
}
