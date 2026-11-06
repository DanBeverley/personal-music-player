import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_provider.dart' show authProvider, storageRefreshTickProvider;
import 'history_manager.dart';
import 'track_hydration.dart';
import 'track_metadata.dart';

class FrequentlyPlayedNotifier
    extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  late final StreamSubscription<Map<String, dynamic>> _historySubscription;
  bool isLoading = false;
  int _requestVersion = 0;

  FrequentlyPlayedNotifier._internal(this.ref) : super(const []);

  factory FrequentlyPlayedNotifier(Ref ref) {
    final notifier = FrequentlyPlayedNotifier._internal(ref);
    notifier._historySubscription = HistoryManager.trackStream.listen((track) {
      notifier._applyImmediateTrack(track);
      unawaited(notifier.loadTracks());
    });
    return notifier;
  }

  void _applyImmediateTrack(Map<String, dynamic> rawTrack) {
    if (!mounted) return;
    final normalizedTrack = normalizeTrack(rawTrack);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId == null || trackId.isEmpty) return;
    final next = <Map<String, dynamic>>[normalizedTrack];
    for (final existing in state) {
      if (extractTrackId(existing) == trackId) continue;
      next.add(existing);
      if (next.length >= 8) break;
    }
    state = next;
  }

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<void> bootstrap() async {
    await loadTracks();
  }

  Future<void> loadTracks({
    bool forceRefresh = false,
  }) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      var frequentSnapshots =
          await HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 8);
      if (frequentSnapshots.isEmpty) {
        final hydrated =
            await HistoryManager.hydrateFromPersistedHistoryIfNeeded();
        if (!_isRequestCurrent(requestVersion)) return;
        if (hydrated) {
          frequentSnapshots =
              await HistoryManager.getFrequentlyPlayedTrackSnapshots(limit: 8);
        }
      }
      if (!_isRequestCurrent(requestVersion)) return;
      final resolved = <Map<String, dynamic>>[];
      final seen = <String>{};
      final snapshotFallbacks = <String, Map<String, dynamic>>{};
      for (final track in frequentSnapshots) {
        final trackId = extractTrackId(track);
        if (trackId == null || trackId.isEmpty || !seen.add(trackId)) continue;
        final normalizedTrack = normalizeTrack(track);
        if (isTrackMetadataIncomplete(normalizedTrack)) {
          snapshotFallbacks[trackId] = normalizedTrack;
        } else {
          resolved.add(normalizedTrack);
        }
        if (seen.length >= 8) break;
      }

      final trackIds =
          await HistoryManager.getFrequentlyPlayedTrackIds(limit: 8);
      if (!_isRequestCurrent(requestVersion)) return;
      if (trackIds.isEmpty && resolved.isEmpty) {
        if (!_isRequestCurrent(requestVersion)) return;
        state = const [];
        return;
      }
      for (final trackId in trackIds) {
        if (seen.length >= 8) break;
        if (!seen.contains(trackId)) {
          seen.add(trackId);
        }
      }
      final missingIds = {
        ...trackIds.where((trackId) {
          return !resolved.any((track) => extractTrackId(track) == trackId);
        }),
        ...snapshotFallbacks.keys.where((trackId) {
          return !resolved.any((track) => extractTrackId(track) == trackId);
        }),
      }.toList(growable: false);

      if (missingIds.isNotEmpty && resolved.length < 8) {
        final hydrated = await hydrateTracksFromIds(
          ref,
          missingIds,
          limit: 8 - resolved.length,
        );
        if (!_isRequestCurrent(requestVersion)) return;
        for (final track in hydrated) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId.isEmpty) continue;
          if (resolved.any((entry) => extractTrackId(entry) == trackId)) {
            continue;
          }
          resolved.add(track);
        }
      }

      for (final entry in snapshotFallbacks.entries) {
        if (resolved.any((track) => extractTrackId(track) == entry.key)) {
          continue;
        }
        resolved.add(entry.value);
        if (resolved.length >= 8) break;
      }

      if (!_isRequestCurrent(requestVersion)) return;
      state = resolved.take(8).toList(growable: false);
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  @override
  void dispose() {
    _historySubscription.cancel();
    _requestVersion++;
    super.dispose();
  }
}

final frequentlyPlayedProvider =
    StateNotifierProvider<FrequentlyPlayedNotifier, List<Map<String, dynamic>>>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = FrequentlyPlayedNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});

class LastPlayedNotifier extends StateNotifier<List<Map<String, dynamic>>> {
  final Ref ref;
  late final StreamSubscription<Map<String, dynamic>> _historySubscription;
  bool isLoading = false;
  int _requestVersion = 0;

  LastPlayedNotifier._internal(this.ref) : super(const []);

  factory LastPlayedNotifier(Ref ref) {
    final notifier = LastPlayedNotifier._internal(ref);
    notifier._historySubscription = HistoryManager.trackStream.listen((track) {
      notifier._applyImmediateTrack(track);
      unawaited(notifier.loadTracks());
    });
    return notifier;
  }

  void _applyImmediateTrack(Map<String, dynamic> rawTrack) {
    if (!mounted) return;
    final normalizedTrack = normalizeTrack(rawTrack);
    final trackId = extractTrackId(normalizedTrack);
    if (trackId == null || trackId.isEmpty) return;
    final next = <Map<String, dynamic>>[normalizedTrack];
    for (final existing in state) {
      if (extractTrackId(existing) == trackId) continue;
      next.add(existing);
      if (next.length >= 8) break;
    }
    state = next;
  }

  bool _isRequestCurrent(int requestVersion) =>
      mounted && requestVersion == _requestVersion;

  Future<void> bootstrap() async {
    await loadTracks();
  }

  Future<void> loadTracks({bool forceRefresh = false}) async {
    final requestVersion = ++_requestVersion;
    isLoading = true;
    if (mounted) {
      state = [...state];
    }
    try {
      var tracks = await HistoryManager.getLastPlayedTrackSnapshots(limit: 8);
      if (tracks.isEmpty) {
        final hydrated =
            await HistoryManager.hydrateFromPersistedHistoryIfNeeded();
        if (!_isRequestCurrent(requestVersion)) return;
        if (hydrated) {
          tracks = await HistoryManager.getLastPlayedTrackSnapshots(limit: 8);
        }
      }
      if (!_isRequestCurrent(requestVersion)) return;
      final resolved = <Map<String, dynamic>>[];
      final pendingIds = <String>[];
      final fallbacks = <String, Map<String, dynamic>>{};
      for (final track in tracks) {
        final normalizedTrack = normalizeTrack(track);
        final trackId = extractTrackId(normalizedTrack);
        if (trackId == null || trackId.isEmpty) continue;
        if (isTrackMetadataIncomplete(normalizedTrack)) {
          pendingIds.add(trackId);
          fallbacks[trackId] = normalizedTrack;
        } else {
          resolved.add(normalizedTrack);
        }
        if (resolved.length + pendingIds.length >= 8) break;
      }
      if (pendingIds.isNotEmpty) {
        final hydrated = await hydrateTracksFromIds(
          ref,
          pendingIds,
          limit: pendingIds.length,
        );
        if (!_isRequestCurrent(requestVersion)) return;
        for (final track in hydrated) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId.isEmpty) continue;
          fallbacks.remove(trackId);
          resolved.add(track);
        }
      }
      if (!_isRequestCurrent(requestVersion)) return;
      for (final fallback in fallbacks.values) {
        resolved.add(fallback);
        if (resolved.length >= 8) break;
      }
      state = resolved.take(8).toList(growable: false);
    } finally {
      if (_isRequestCurrent(requestVersion)) {
        isLoading = false;
        state = [...state];
      }
    }
  }

  @override
  void dispose() {
    _historySubscription.cancel();
    _requestVersion++;
    super.dispose();
  }
}

final lastPlayedProvider =
    StateNotifierProvider<LastPlayedNotifier, List<Map<String, dynamic>>>(
        (ref) {
  ref.watch(authProvider.select((state) => state.storageScopeId));
  ref.watch(storageRefreshTickProvider);
  final notifier = LastPlayedNotifier(ref);
  unawaited(notifier.bootstrap());
  return notifier;
});
