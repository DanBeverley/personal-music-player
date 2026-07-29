import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';
import 'proxy_runtime.dart';

class RecommendationPreferencesState {
  const RecommendationPreferencesState({
    this.isLoading = false,
    this.isSaving = false,
    this.tasteMode = 'neatie',
    this.effectiveTasteMode = 'neatie',
    this.listenBrainzUsername = '',
    this.listenBrainzStatus = 'not_linked',
    this.error = '',
  });

  final bool isLoading;
  final bool isSaving;
  final String tasteMode;
  final String effectiveTasteMode;
  final String listenBrainzUsername;
  final String listenBrainzStatus;
  final String error;

  RecommendationPreferencesState copyWith({
    bool? isLoading,
    bool? isSaving,
    String? tasteMode,
    String? effectiveTasteMode,
    String? listenBrainzUsername,
    String? listenBrainzStatus,
    String? error,
  }) {
    return RecommendationPreferencesState(
      isLoading: isLoading ?? this.isLoading,
      isSaving: isSaving ?? this.isSaving,
      tasteMode: tasteMode ?? this.tasteMode,
      effectiveTasteMode: effectiveTasteMode ?? this.effectiveTasteMode,
      listenBrainzUsername:
          listenBrainzUsername ?? this.listenBrainzUsername,
      listenBrainzStatus: listenBrainzStatus ?? this.listenBrainzStatus,
      error: error ?? this.error,
    );
  }
}

class RecommendationPreferencesNotifier
    extends StateNotifier<RecommendationPreferencesState> {
  RecommendationPreferencesNotifier()
      : super(const RecommendationPreferencesState()) {
    load();
  }

  String get _scopeId => activeStorageScopeId;

  Future<void> load() async {
    state = state.copyWith(isLoading: true, error: '');
    try {
      final uri = buildProxyUri('/recommendation/preferences').replace(
        queryParameters: {'user_scope_id': _scopeId},
      );
      final response = await appHttpClient.get(uri);
      if (response.statusCode != 200) {
        throw StateError('Could not load recommendation settings.');
      }
      _applyPayload(jsonDecode(response.body));
    } catch (error) {
      state = state.copyWith(isLoading: false, error: error.toString());
    }
  }

  void setTasteMode(String value) {
    state = state.copyWith(tasteMode: value, error: '');
  }

  void setListenBrainzUsername(String value) {
    state = state.copyWith(listenBrainzUsername: value, error: '');
  }

  Future<bool> save() async {
    state = state.copyWith(isSaving: true, error: '');
    try {
      final response = await appHttpClient.put(
        buildProxyUri('/recommendation/preferences'),
        headers: const {'Content-Type': 'application/json'},
        body: jsonEncode({
          'user_scope_id': _scopeId,
          'taste_mode': state.tasteMode,
          'listenbrainz_username': state.listenBrainzUsername.trim(),
        }),
      );
      if (response.statusCode != 200) {
        throw StateError('Could not save recommendation settings.');
      }
      _applyPayload(jsonDecode(response.body));
      return true;
    } catch (error) {
      state = state.copyWith(isSaving: false, error: error.toString());
      return false;
    }
  }

  void _applyPayload(Object? raw) {
    final payload = raw is Map
        ? Map<String, dynamic>.from(raw)
        : const <String, dynamic>{};
    state = RecommendationPreferencesState(
      tasteMode: payload['taste_mode']?.toString() ?? 'neatie',
      effectiveTasteMode:
          payload['effective_taste_mode']?.toString() ?? 'neatie',
      listenBrainzUsername:
          payload['listenbrainz_username']?.toString() ?? '',
      listenBrainzStatus:
          payload['listenbrainz_status']?.toString() ?? 'not_linked',
    );
  }
}

final recommendationPreferencesProvider = StateNotifierProvider<
    RecommendationPreferencesNotifier, RecommendationPreferencesState>((ref) {
  return RecommendationPreferencesNotifier();
});
