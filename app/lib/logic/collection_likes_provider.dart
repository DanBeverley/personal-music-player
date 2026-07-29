import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart';

class CollectionLikesNotifier extends StateNotifier<Set<String>> {
  CollectionLikesNotifier(this.scopeId) : super(<String>{}) {
    unawaited(_load());
  }

  final String scopeId;

  Future<void> _load() async {
    try {
      final file = await getScopedDataFile('liked_collections.json', scopeId);
      if (!await file.exists()) return;
      final raw = jsonDecode(await file.readAsString());
      if (raw is! List) return;
      state = raw
          .map((item) => item.toString().trim())
          .where((item) => item.isNotEmpty)
          .toSet();
    } catch (error) {
      debugPrint('Collection likes load failed: $error');
    }
  }

  Future<void> toggle(String rawKey) async {
    final key = rawKey.trim();
    if (key.isEmpty) return;
    final next = Set<String>.from(state);
    next.contains(key) ? next.remove(key) : next.add(key);
    state = next;
    try {
      final file = await getScopedDataFile('liked_collections.json', scopeId);
      await file.parent.create(recursive: true);
      await file.writeAsString(jsonEncode(next.toList()..sort()));
    } catch (error) {
      debugPrint('Collection likes save failed: $error');
    }
  }
}

final collectionLikesProvider =
    StateNotifierProvider<CollectionLikesNotifier, Set<String>>((ref) {
  final scopeId = ref.watch(
    authProvider.select((state) => state.storageScopeId),
  );
  return CollectionLikesNotifier(scopeId);
});
