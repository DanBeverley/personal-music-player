import 'dart:async';

enum PlaybackStartFailure { interrupted, failed }

class PlaybackStartException implements Exception {
  final PlaybackStartFailure kind;
  final String cause;
  const PlaybackStartException(this.kind, this.cause);
  @override
  String toString() => 'PlaybackStartException($cause)';
}

/// Platform boundary: no filesystem, FFI, or browser imports in the controller.
abstract class PlaybackPort {
  Stream<String> get events;
  double get position;
  double get duration;
  bool get paused;
  String? get loadedTrackId;
  bool get sourceMatches;
  void syncMetadata(Map<String, dynamic> track);
  void load(Uri uri, Map<String, dynamic> track);
  Future<void> play();
  void pause();
  void seek(double seconds);
  void dispose();
}

abstract class SessionStore {
  String? read();
  void write(String value);
}
