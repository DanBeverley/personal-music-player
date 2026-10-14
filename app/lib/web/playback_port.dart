import 'dart:async';

enum PlaybackStartFailure { interrupted, failed }

enum PlaybackPhase {
  idle,
  starting,
  playing,
  buffering,
  paused,
  interrupted,
  failed,
  ended
}

/// A play Promise and a seek are not evidence of advancing audio.
class PlaybackProgress {
  PlaybackPhase phase = PlaybackPhase.idle;
  bool requested = false;
  double _position = 0;

  void begin(double position) {
    requested = true;
    _position = position;
    phase = PlaybackPhase.starting;
  }

  void rebase(double position) => _position = position;

  bool observe(double position, {required bool paused, bool seeking = false}) {
    final advanced =
        requested && !paused && !seeking && position > _position + .02;
    if (advanced || paused || seeking || position < _position)
      _position = position;
    if (advanced) phase = PlaybackPhase.playing;
    return advanced;
  }

  void buffer() {
    if (requested) phase = PlaybackPhase.buffering;
  }

  void interrupt() {
    if (requested) phase = PlaybackPhase.interrupted;
  }

  void stop(PlaybackPhase next) {
    requested = false;
    phase = next;
  }
}

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
  PlaybackPhase get phase;
  bool get needsNewSource => false;

  /// An expired URL need not be replaced when all remaining audio is buffered.
  bool get fullyBuffered => false;
  String? get loadedTrackId;
  bool get sourceMatches;
  void syncMetadata(Map<String, dynamic> track);
  void load(Uri uri, Map<String, dynamic> track);

  /// Reload the selected resource without changing its playback session.
  void reloadSource();
  Future<void> play();
  void pause();
  void seek(double seconds);
  void dispose();
  void foreground() {}
  void record(String event, [Map<String, dynamic> extra = const {}]) {}
}

abstract class SessionStore {
  String? read();
  void write(String value);
}
