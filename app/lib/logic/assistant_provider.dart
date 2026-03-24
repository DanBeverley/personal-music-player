import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'audio_provider.dart';
import 'auth_provider.dart';
import 'playlist_provider.dart';

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
      name: json['name']?.toString() ?? 'EBB Mix',
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
        id: json['id']?.toString() ?? _assistantId(),
        role: role,
        text: json['content']?.toString() ?? '',
      );
    }
    return AssistantMessage.fromPayload(
      id: json['id']?.toString() ?? _assistantId(),
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

class AssistantState {
  final List<AssistantMessage> messages;
  final List<AssistantSessionSummary> sessions;
  final bool thinkingMode;
  final bool isSending;
  final bool isLoadingSessions;
  final bool isLoadingSession;
  final bool isInitialized;
  final String? currentSessionId;
  final String? currentSessionTitle;
  final String? scopeId;
  final String? error;

  const AssistantState({
    this.messages = const [],
    this.sessions = const [],
    this.thinkingMode = true,
    this.isSending = false,
    this.isLoadingSessions = false,
    this.isLoadingSession = false,
    this.isInitialized = false,
    this.currentSessionId,
    this.currentSessionTitle,
    this.scopeId,
    this.error,
  });

  AssistantState copyWith({
    List<AssistantMessage>? messages,
    List<AssistantSessionSummary>? sessions,
    bool? thinkingMode,
    bool? isSending,
    bool? isLoadingSessions,
    bool? isLoadingSession,
    bool? isInitialized,
    Object? currentSessionId = _assistantUnset,
    Object? currentSessionTitle = _assistantUnset,
    Object? scopeId = _assistantUnset,
    Object? error = _assistantUnset,
  }) {
    return AssistantState(
      messages: messages ?? this.messages,
      sessions: sessions ?? this.sessions,
      thinkingMode: thinkingMode ?? this.thinkingMode,
      isSending: isSending ?? this.isSending,
      isLoadingSessions: isLoadingSessions ?? this.isLoadingSessions,
      isLoadingSession: isLoadingSession ?? this.isLoadingSession,
      isInitialized: isInitialized ?? this.isInitialized,
      currentSessionId: identical(currentSessionId, _assistantUnset)
          ? this.currentSessionId
          : currentSessionId as String?,
      currentSessionTitle: identical(currentSessionTitle, _assistantUnset)
          ? this.currentSessionTitle
          : currentSessionTitle as String?,
      scopeId: identical(scopeId, _assistantUnset) ? this.scopeId : scopeId as String?,
      error: identical(error, _assistantUnset) ? this.error : error as String?,
    );
  }
}

const Object _assistantUnset = Object();

class _AssistantSessionDetail {
  final AssistantSessionSummary session;
  final List<AssistantMessage> messages;

  const _AssistantSessionDetail({
    required this.session,
    required this.messages,
  });
}

int _assistantParseInt(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

String _assistantId() => DateTime.now().microsecondsSinceEpoch.toString();

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

Uri _assistantProxyUri(String path, [Map<String, dynamic>? queryParameters]) {
  final base = buildProxyUri(path);
  if (queryParameters == null || queryParameters.isEmpty) {
    return base;
  }
  final filtered = <String, String>{};
  queryParameters.forEach((key, value) {
    final text = value?.toString();
    if (text != null && text.isNotEmpty) {
      filtered[key] = text;
    }
  });
  if (filtered.isEmpty) return base;
  return base.replace(queryParameters: filtered);
}

class AssistantNotifier extends StateNotifier<AssistantState> {
  final Ref ref;

  AssistantNotifier(this.ref) : super(const AssistantState());

  void toggleThinkingMode() {
    state = state.copyWith(thinkingMode: !state.thinkingMode);
  }

  String get _scopeId => ref.read(authProvider).storageScopeId;

  List<Map<String, dynamic>> _lastAssistantTracks(
    List<AssistantMessage> messages, {
    int limit = 10,
  }) {
    for (final message in messages.reversed) {
      if (message.role != 'assistant' || message.tracks.isEmpty) continue;
      return message.tracks
          .take(limit)
          .map((track) => track.toContextJson())
          .toList(growable: false);
    }
    return const [];
  }

  List<Map<String, dynamic>> _lastPlaylistDraftTracks(
    List<AssistantMessage> messages, {
    int limit = 10,
  }) {
    for (final message in messages.reversed) {
      final draft = message.playlistDraft;
      if (message.role != 'assistant' || draft == null || draft.tracks.isEmpty) {
        continue;
      }
      return draft.tracks
          .take(limit)
          .map((track) => track.toContextJson())
          .toList(growable: false);
    }
    return const [];
  }

  List<Map<String, dynamic>> _recentAssistantTracks(
    List<AssistantMessage> messages, {
    int limit = 18,
  }) {
    final deduped = <String, Map<String, dynamic>>{};
    final ordered = <Map<String, dynamic>>[];
    for (final message in messages.reversed) {
      if (message.role != 'assistant') continue;
      for (final card in message.tracks.reversed) {
        final entry = card.toContextJson();
        final id = entry['id']?.toString();
        if (id == null || id.isEmpty || deduped.containsKey(id)) continue;
        deduped[id] = entry;
        ordered.add(entry);
        if (ordered.length >= limit) {
          return ordered.reversed.toList(growable: false);
        }
      }
      final draft = message.playlistDraft;
      if (draft == null) continue;
      for (final card in draft.tracks.reversed) {
        final entry = card.toContextJson();
        final id = entry['id']?.toString();
        if (id == null || id.isEmpty || deduped.containsKey(id)) continue;
        deduped[id] = entry;
        ordered.add(entry);
        if (ordered.length >= limit) {
          return ordered.reversed.toList(growable: false);
        }
      }
    }
    return ordered.reversed.toList(growable: false);
  }

  List<AssistantSessionSummary> _mergeSessionSummary(
    List<AssistantSessionSummary> sessions,
    AssistantSessionSummary summary,
  ) {
    final next = [
      summary,
      ...sessions.where((entry) => entry.id != summary.id),
    ];
    next.sort((a, b) {
      if (a.isArchived != b.isArchived) {
        return a.isArchived ? 1 : -1;
      }
      if (a.isPinned != b.isPinned) {
        return a.isPinned ? -1 : 1;
      }
      final aPinned = a.pinnedAt?.millisecondsSinceEpoch ?? 0;
      final bPinned = b.pinnedAt?.millisecondsSinceEpoch ?? 0;
      if (aPinned != bPinned) {
        return bPinned.compareTo(aPinned);
      }
      final aTime = a.updatedAt?.millisecondsSinceEpoch ?? 0;
      final bTime = b.updatedAt?.millisecondsSinceEpoch ?? 0;
      return bTime.compareTo(aTime);
    });
    return next;
  }

  Future<List<AssistantSessionSummary>> _fetchSessions(
    String scopeId, {
    bool includeArchived = true,
  }) async {
    final response = await appHttpClient.get(
      _assistantProxyUri('/assistant/sessions', {
        'user_scope_id': scopeId,
        'include_archived': includeArchived,
      }),
    );
    if (response.statusCode != 200) {
      throw Exception('Failed to load sessions: ${response.body}');
    }
    final payload = jsonDecode(response.body) as Map<String, dynamic>;
    return (payload['sessions'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantSessionSummary.fromJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);
  }

  Future<_AssistantSessionDetail> _fetchSessionDetail(
    String scopeId,
    String sessionId,
  ) async {
    final response = await appHttpClient.get(
      _assistantProxyUri('/assistant/sessions/$sessionId', {
        'user_scope_id': scopeId,
      }),
    );
    if (response.statusCode != 200) {
      throw Exception('Failed to open session: ${response.body}');
    }
    final payload = jsonDecode(response.body) as Map<String, dynamic>;
    final session = AssistantSessionSummary.fromJson(
      Map<String, dynamic>.from(payload['session'] as Map),
    );
    final messages = (payload['messages'] as List<dynamic>? ?? const [])
        .map((entry) => AssistantMessage.fromStoredJson(
              Map<String, dynamic>.from(entry as Map),
            ))
        .toList(growable: false);
    return _AssistantSessionDetail(session: session, messages: messages);
  }

  Future<void> ensureInitialized({bool force = false}) async {
    final scopeId = _scopeId;
    if (!force && state.isInitialized && state.scopeId == scopeId) return;

    state = state.copyWith(
      isInitialized: true,
      isLoadingSessions: true,
      isLoadingSession: false,
      scopeId: scopeId,
      error: null,
    );

    try {
      final sessions = await _fetchSessions(scopeId, includeArchived: true);
      if (sessions.isEmpty) {
        state = state.copyWith(
          sessions: const [],
          messages: const [],
          currentSessionId: null,
          currentSessionTitle: null,
          isLoadingSessions: false,
          isLoadingSession: false,
          error: null,
        );
        return;
      }

      final desiredSessionId = state.scopeId == scopeId ? state.currentSessionId : null;
      final chosenSession = sessions.firstWhere(
        (entry) => entry.id == desiredSessionId,
        orElse: () => sessions.first,
      );
      final detail = await _fetchSessionDetail(scopeId, chosenSession.id);
      state = state.copyWith(
        sessions: _mergeSessionSummary(sessions, detail.session),
        messages: detail.messages,
        currentSessionId: detail.session.id,
        currentSessionTitle: detail.session.title,
        isLoadingSessions: false,
        isLoadingSession: false,
        error: null,
      );
    } catch (error) {
      debugPrint('Assistant bootstrap failed: $error');
      state = state.copyWith(
        isLoadingSessions: false,
        isLoadingSession: false,
        error: error.toString(),
      );
    }
  }

  void clearConversation() {
    state = state.copyWith(
      messages: const [],
      currentSessionId: null,
      currentSessionTitle: null,
      error: null,
    );
  }

  Future<void> openSession(String sessionId) async {
    final scopeId = _scopeId;
    if (sessionId.isEmpty || state.isLoadingSession) return;
    state = state.copyWith(isLoadingSession: true, error: null, scopeId: scopeId);
    try {
      final detail = await _fetchSessionDetail(scopeId, sessionId);
      state = state.copyWith(
        sessions: _mergeSessionSummary(state.sessions, detail.session),
        messages: detail.messages,
        currentSessionId: detail.session.id,
        currentSessionTitle: detail.session.title,
        isLoadingSession: false,
        error: null,
      );
    } catch (error) {
      debugPrint('Assistant session open failed: $error');
      state = state.copyWith(
        isLoadingSession: false,
        error: error.toString(),
      );
    }
  }

  Future<void> renameSession(String sessionId, String title) async {
    final scopeId = _scopeId;
    final trimmed = title.trim();
    if (sessionId.isEmpty || trimmed.isEmpty) return;
    try {
      final response = await appHttpClient.patch(
        _assistantProxyUri('/assistant/sessions/$sessionId'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'user_scope_id': scopeId,
          'title': trimmed,
        }),
      );
      if (response.statusCode != 200) {
        throw Exception('Failed to rename session: ${response.body}');
      }
      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final session = AssistantSessionSummary.fromJson(
        Map<String, dynamic>.from(payload['session'] as Map),
      );
      state = state.copyWith(
        sessions: _mergeSessionSummary(state.sessions, session),
        currentSessionTitle:
            state.currentSessionId == session.id ? session.title : state.currentSessionTitle,
      );
    } catch (error) {
      debugPrint('Assistant session rename failed: $error');
      state = state.copyWith(error: error.toString());
    }
  }

  Future<void> deleteSession(String sessionId) async {
    final scopeId = _scopeId;
    if (sessionId.isEmpty) return;
    try {
      final response = await appHttpClient.delete(
        _assistantProxyUri('/assistant/sessions/$sessionId', {
          'user_scope_id': scopeId,
        }),
      );
      if (response.statusCode != 200) {
        throw Exception('Failed to delete session: ${response.body}');
      }
      final remaining = state.sessions.where((entry) => entry.id != sessionId).toList();
      if (state.currentSessionId == sessionId) {
        if (remaining.isEmpty) {
          state = state.copyWith(
            sessions: remaining,
            messages: const [],
            currentSessionId: null,
            currentSessionTitle: null,
          );
        } else {
          state = state.copyWith(sessions: remaining);
          await openSession(remaining.first.id);
        }
        return;
      }
      state = state.copyWith(sessions: remaining);
    } catch (error) {
      debugPrint('Assistant session delete failed: $error');
      state = state.copyWith(error: error.toString());
    }
  }

  Future<void> archiveSession(String sessionId, bool archived) async {
    final scopeId = _scopeId;
    if (sessionId.isEmpty) return;
    try {
      final response = await appHttpClient.patch(
        _assistantProxyUri('/assistant/sessions/$sessionId'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'user_scope_id': scopeId,
          'archived': archived,
        }),
      );
      if (response.statusCode != 200) {
        throw Exception('Failed to update session archive state: ${response.body}');
      }
      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final session = AssistantSessionSummary.fromJson(
        Map<String, dynamic>.from(payload['session'] as Map),
      );
      final sessions = _mergeSessionSummary(
        state.sessions.where((entry) => entry.id != session.id).toList(),
        session,
      );
      if (archived && state.currentSessionId == sessionId) {
        final nextActive = sessions.firstWhere(
          (entry) => !entry.isArchived,
          orElse: () => session,
        );
        if (nextActive.id != session.id) {
          state = state.copyWith(sessions: sessions);
          await openSession(nextActive.id);
          return;
        }
      }
      state = state.copyWith(
        sessions: sessions,
        currentSessionTitle:
            state.currentSessionId == session.id ? session.title : state.currentSessionTitle,
      );
    } catch (error) {
      debugPrint('Assistant session archive failed: $error');
      state = state.copyWith(error: error.toString());
    }
  }

  Future<void> pinSession(String sessionId, bool pinned) async {
    final scopeId = _scopeId;
    if (sessionId.isEmpty) return;
    try {
      final response = await appHttpClient.patch(
        _assistantProxyUri('/assistant/sessions/$sessionId'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'user_scope_id': scopeId,
          'pinned': pinned,
        }),
      );
      if (response.statusCode != 200) {
        throw Exception('Failed to update session pin state: ${response.body}');
      }
      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final session = AssistantSessionSummary.fromJson(
        Map<String, dynamic>.from(payload['session'] as Map),
      );
      state = state.copyWith(
        sessions: _mergeSessionSummary(state.sessions, session),
        currentSessionTitle:
            state.currentSessionId == session.id ? session.title : state.currentSessionTitle,
      );
    } catch (error) {
      debugPrint('Assistant session pin failed: $error');
      state = state.copyWith(error: error.toString());
    }
  }

  Future<void> retryLastTurn() async {
    if (state.isSending) return;
    final lastUserIndex =
        state.messages.lastIndexWhere((message) => message.role == 'user');
    if (lastUserIndex < 0) return;
    final retryText = state.messages[lastUserIndex].text.trim();
    if (retryText.isEmpty) return;
    final trimmedMessages = state.messages.take(lastUserIndex).toList(growable: false);
    state = state.copyWith(messages: trimmedMessages, error: null);
    await sendMessage(retryText);
  }

  Future<void> editAndResendLastUserMessage(String updatedMessage) async {
    final trimmed = updatedMessage.trim();
    if (trimmed.isEmpty || state.isSending) return;
    final lastUserIndex =
        state.messages.lastIndexWhere((message) => message.role == 'user');
    if (lastUserIndex < 0) {
      await sendMessage(trimmed);
      return;
    }
    final trimmedMessages = state.messages.take(lastUserIndex).toList(growable: false);
    state = state.copyWith(messages: trimmedMessages, error: null);
    await sendMessage(trimmed);
  }

  Future<void> sendMessage(String rawMessage) async {
    final message = rawMessage.trim();
    if (message.isEmpty || state.isSending) return;

    if (!state.isInitialized || state.scopeId != _scopeId) {
      await ensureInitialized(force: true);
    }

    final userMessage = AssistantMessage(
      id: _assistantId(),
      role: 'user',
      text: message,
    );
    state = state.copyWith(
      messages: [...state.messages, userMessage],
      isSending: true,
      error: null,
    );

    try {
      final authState = ref.read(authProvider);
      final playlists = ref.read(playlistProvider);
      final libraryTracks = ref.read(libraryProvider).valueOrNull ?? const [];
      final recentTrackIds = await HistoryManager.getRecentSeeds(limit: 8);
      final recentQueries = await getRecentCloudSearchQueries(limit: 8);
      final previousMessages =
          state.messages.take(state.messages.length - 1).toList(growable: false);
      final conversationWindow = previousMessages.length > 12
          ? previousMessages.sublist(previousMessages.length - 12)
          : previousMessages;

      final body = {
        'message': message,
        'user_scope_id': authState.storageScopeId,
        'session_id': state.currentSessionId,
        'thinking_mode': state.thinkingMode,
        'conversation': conversationWindow
            .map((entry) => entry.toConversationJson())
            .toList(growable: false),
        'last_assistant_tracks': _lastAssistantTracks(previousMessages),
        'last_playlist_draft_tracks': _lastPlaylistDraftTracks(previousMessages),
        'recent_assistant_tracks': _recentAssistantTracks(previousMessages),
        'playlist_summaries': playlists
            .map((playlist) => {
                  'id': playlist.id,
                  'name': playlist.name,
                  'track_count': playlist.tracks.length,
                })
            .toList(growable: false),
        'recent_track_ids': recentTrackIds,
        'recent_queries': recentQueries,
        'library_tracks': libraryTracks
            .take(20)
            .map((track) => {
                  'id': extractTrackId(track),
                  'title': track['title'],
                  'artist': track['author'] ?? track['artist'] ?? track['channel'],
                  'album': track['album'] ?? track['album_title'],
                })
            .toList(growable: false),
        'limit': 10,
      };

      final response = await appHttpClient.post(
        _assistantProxyUri('/assistant/chat'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      );

      if (response.statusCode != 200) {
        throw Exception('Assistant failed: ${response.body}');
      }

      final payload = jsonDecode(response.body) as Map<String, dynamic>;
      final assistantMessage = AssistantMessage.fromPayload(
        id: _assistantId(),
        payload: payload,
      );
      final session = payload['session'] is Map
          ? AssistantSessionSummary.fromJson(
              Map<String, dynamic>.from(payload['session'] as Map),
            )
          : null;

      state = state.copyWith(
        messages: [...state.messages, assistantMessage],
        sessions: session == null
            ? state.sessions
            : _mergeSessionSummary(state.sessions, session),
        currentSessionId:
            session?.id ?? payload['session_id']?.toString() ?? state.currentSessionId,
        currentSessionTitle: session?.title ??
            payload['session_title']?.toString() ??
            state.currentSessionTitle,
        isSending: false,
        error: null,
      );
    } catch (error) {
      debugPrint('Assistant request failed: $error');
      final assistantMessage = AssistantMessage(
        id: _assistantId(),
        role: 'assistant',
        text:
            'I hit a snag talking to the assistant service. Please try again in a moment.',
      );
      state = state.copyWith(
        messages: [...state.messages, assistantMessage],
        isSending: false,
        error: error.toString(),
      );
    }
  }
}

final assistantProvider =
    StateNotifierProvider<AssistantNotifier, AssistantState>((ref) {
  return AssistantNotifier(ref);
});
