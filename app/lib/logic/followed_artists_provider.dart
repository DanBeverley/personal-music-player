import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';

String _artistKey(Map<String, dynamic> artist) {
  final id = (artist['id'] ?? artist['browseId'] ?? artist['artist_id'])
          ?.toString()
          .trim() ??
      '';
  if (id.isNotEmpty) return id;
  return (artist['name'] ?? artist['artist'] ?? artist['channel'])
          ?.toString()
          .trim()
          .toLowerCase() ??
      '';
}

class FollowedArtistsNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  FollowedArtistsNotifier(this.ref) : super(const []) {
    unawaited(load());
  }

  final Ref ref;

  Future<void> load() async {
    final scopeId = ref.read(authProvider).storageScopeId;
    try {
      final file = await getScopedDataFile('followed_artists.json', scopeId);
      if (!file.existsSync()) {
        state = const [];
        return;
      }
      final decoded = jsonDecode(await file.readAsString());
      final artists = decoded is List ? decoded : const [];
      state = artists
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((artist) => _artistKey(artist).isNotEmpty)
          .toList(growable: false);
    } catch (_) {
      state = const [];
    }
  }

  Future<void> _persist(List<Map<String, dynamic>> artists) async {
    final scopeId = ref.read(authProvider).storageScopeId;
    final file = await getScopedDataFile('followed_artists.json', scopeId);
    if (!file.parent.existsSync()) {
      file.parent.createSync(recursive: true);
    }
    await file.writeAsString(jsonEncode(artists));
  }

  bool isFollowing(Map<String, dynamic> artist) {
    final key = _artistKey(artist);
    if (key.isEmpty) return false;
    return state.any((entry) => _artistKey(entry) == key);
  }

  Future<void> toggle(Map<String, dynamic> artist) async {
    final key = _artistKey(artist);
    if (key.isEmpty) return;
    final normalized = <String, dynamic>{
      ...artist,
      'id': (artist['id'] ?? artist['browseId'] ?? artist['artist_id'] ?? key)
          .toString(),
      'name': (artist['name'] ?? artist['artist'] ?? artist['channel'] ?? 'Artist')
          .toString(),
      'followed_at': DateTime.now().toUtc().toIso8601String(),
    };
    final next = state.where((entry) => _artistKey(entry) != key).toList();
    if (next.length == state.length) {
      next.insert(0, normalized);
    }
    state = next;
    await _persist(next);
  }
}

final followedArtistsProvider = StateNotifierProvider<
    FollowedArtistsNotifier, List<Map<String, dynamic>>>((ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  return FollowedArtistsNotifier(ref);
});
