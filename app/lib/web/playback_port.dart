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
  double _resumeAdvance = 0;
  int _resumeSamples = 0;
  bool _confirmResume = false;
  bool get resumePending => requested && _confirmResume;

  /// Media Session describes native playback intent; progress remains separate
  /// proof that audio time is advancing. Starting/buffering is not a Pause.
  String mediaSessionState({required bool nativePaused}) {
    if (phase == PlaybackPhase.idle) return 'none';
    return requested &&
            !nativePaused &&
            const {
              PlaybackPhase.starting,
              PlaybackPhase.playing,
              PlaybackPhase.buffering
            }.contains(phase)
        ? 'playing'
        : 'paused';
  }

  void begin(double position, {bool confirmResume = false}) {
    requested = true;
    _position = position;
    _confirmResume = confirmResume;
    _resumeSamples = 0;
    _resumeAdvance = 0;
    phase = PlaybackPhase.starting;
  }

  void rebase(double position) {
    _position = position;
    _resumeSamples = 0;
    _resumeAdvance = 0;
  }

  bool observe(double position, {required bool paused, bool seeking = false}) {
    final advanced =
        requested && !paused && !seeking && position > _position + .02;
    if (advanced && _confirmResume) {
      _resumeAdvance += position - _position;
      _resumeSamples++;
      if (_resumeSamples >= 2 && _resumeAdvance >= .15) _confirmResume = false;
    } else if (paused || seeking || position < _position) {
      rebase(position);
    }
    if (advanced || paused || seeking || position < _position)
      _position = position;
    if (advanced && !_confirmResume) phase = PlaybackPhase.playing;
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
  String? get sourceFailureCode => null;
  bool get supportsStagedHandoff => false;

  Future<void> stage(Uri uri, Map<String, dynamic> track, {String? mimeType}) =>
      Future.error(UnsupportedError('Staged handoff is unavailable'));
  Future<void> commitStaged() =>
      Future.error(UnsupportedError('Staged handoff is unavailable'));
  void cancelStaged() {}

  /// An expired URL need not be replaced when all remaining audio is buffered.
  bool get fullyBuffered => false;
  String? get loadedTrackId;
  bool get sourceMatches;
  /// Assigned ownership survives the browser's asynchronous currentSrc change.
  /// Event filtering still uses sourceMatches to reject old-resource events.
  bool get sourceAssigned => sourceMatches;
  void syncMetadata(Map<String, dynamic> track);
  void load(Uri uri, Map<String, dynamic> track, {String? mimeType});

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

abstract class CheckpointSessionStore implements SessionStore {
  String? readCheckpoint();
  bool writeCheckpoint(String value);
}
