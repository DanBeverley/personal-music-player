import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'package:web/web.dart' as web;
import 'playback_port.dart';
import 'playback_diagnostics.dart';
import '../logic/track_metadata.dart';

extension type _MediaAction(JSObject _) implements JSObject {
  external double? get seekTime;
  external double? get seekOffset;
}

extension type _AudioNavigator(JSObject _) implements JSObject {
  external _AudioSession? get audioSession;
}

extension type _AudioSession(JSObject _) implements JSObject {
  external set type(String value);
  external String get state;
  external set onstatechange(JSFunction? value);
}

class BrowserSessionStore implements SessionStore {
  final String key;
  BrowserSessionStore(String backend, {String scope = 'guest'})
      : key = 'neatie:$scope:session:$backend';
  @override
  String? read() => web.window.localStorage.getItem(key);
  @override
  void write(String value) => web.window.localStorage.setItem(key, value);
}

/// One persistent media element owns playback across page rebuilds and tracks.
/// No Web Audio context or background timer is needed for queue advancement.
class BrowserAudio extends PlaybackPort {
  final web.HTMLAudioElement element = web.HTMLAudioElement();
  final _events = StreamController<String>.broadcast(sync: true);
  final Map<String, JSFunction> _listeners = {};
  final List<String> _actions = [];
  final _progress = PlaybackProgress();
  late final _reports = PlaybackDiagnostics({
    'userAgent': web.window.navigator.userAgent,
    'platform': web.window.navigator.platform,
    'language': web.window.navigator.language,
  });
  Timer? _watchdog;
  DateTime? _loadedAt, _pausedAt, _lastProgressAt, _deadline;
  bool _everAdvanced = false, _resume = false, _renewSource = false;
  bool _foreground = !web.document.hidden;
  int _attempt = 0, _pauseDurationMs = 0;
  String _diagnosticKey = '';
  _AudioSession? _audioSession;
  String _track = '';
  String _session = '';
  String _expectedSource = '';
  Map<String, dynamic> Function()? diagnosticContext;
  void Function(String)? onReconcile;
  @override
  String? get loadedTrackId => _track.isEmpty ? null : _track;
  @override
  bool get sourceMatches =>
      _expectedSource.isNotEmpty &&
      element.src == _expectedSource &&
      (element.currentSrc.isEmpty || element.currentSrc == _expectedSource);
  @override
  PlaybackPhase get phase => _progress.phase;
  @override
  bool get needsNewSource => _renewSource;
  Map<String, dynamic> diagnosticReport({bool current = false}) {
    final report = _reports.report(current: current);
    _persistFailures();
    return report;
  }

  void setDiagnosticScope(String backend, String scope) {
    final next = 'neatie:$scope:playback-failures:$backend';
    if (_diagnosticKey.isNotEmpty && _diagnosticKey != next) {
      pause();
      _track = _session = _expectedSource = '';
      _loadedAt = _pausedAt = _lastProgressAt = _deadline = null;
      _everAdvanced = _resume = _renewSource = false;
      _pauseDurationMs = 0;
      _progress.stop(PlaybackPhase.idle);
      element.removeAttribute('src');
      element.load();
      try {
        web.window.navigator.mediaSession.metadata = null;
        web.window.navigator.mediaSession.setPositionState();
      } catch (_) {}
      _updateSession();
    }
    _diagnosticKey = next;
    try {
      _reports.restore(web.window.localStorage.getItem(_diagnosticKey));
    } catch (_) {
      _reports.restore(null);
    }
    record('device');
  }

  void record(String event, [Map<String, dynamic> extra = const {}]) {
    _reports.record({
      'at': DateTime.now().toUtc().toIso8601String(),
      'event': event,
      'track': _track,
      'session': _session,
      'time': position,
      'paused': paused,
      'muted': element.muted,
      'volume': element.volume,
      'ready': element.readyState,
      'network': element.networkState,
      'mediaCode': element.error?.code,
      'mediaMessage': _safeErrorText(element.error?.message ?? ''),
      'bufferedEnd': element.buffered.length > 0
          ? element.buffered.end(element.buffered.length - 1)
          : 0,
      'visibility': _foreground && !web.document.hidden ? 'visible' : 'hidden',
      'audioSession': _audioSession?.state,
      ...extra,
      'phase': phase.name,
      'playRequested': _progress.requested,
      'resume': _resume,
      'pauseMs': _pausedAt == null
          ? null
          : phase == PlaybackPhase.paused
              ? DateTime.now().difference(_pausedAt!).inMilliseconds
              : _pauseDurationMs,
      'sourceAgeSeconds': _loadedAt == null
          ? null
          : DateTime.now().difference(_loadedAt!).inSeconds,
      'secondsSinceProgress': _lastProgressAt == null
          ? null
          : DateTime.now().difference(_lastProgressAt!).inSeconds,
      'repeatOne': element.loop,
      'sourceMatches': sourceMatches,
      'elementSession': sourceMatches
          ? Uri.tryParse(element.currentSrc)
              ?.queryParameters['playback_session']
          : null,
      ...?diagnosticContext?.call(),
    });
    _persistFailures();
  }

  void _persistFailures() {
    if (_diagnosticKey.isEmpty) return;
    try {
      web.window.localStorage.setItem(_diagnosticKey, _reports.encodeSaved());
    } catch (_) {/* Failure history is best effort. */}
  }

  void Function()? onPlay;
  void Function()? onPause;
  void Function()? onNext;
  void Function()? onPrevious;

  BrowserAudio() {
    element.preload = 'auto';
    element.setAttribute('playsinline', '');
    element.style.display = 'none';
    web.document.body?.append(element);
    try {
      _audioSession = _AudioNavigator(web.window.navigator).audioSession;
      _audioSession?.type = 'playback';
      _audioSession?.onstatechange = ((web.Event _) {
        if (_audioSession?.state == 'interrupted' && _progress.requested) {
          _progress.interrupt();
          record('audio-session-interrupted');
          _reports.capture('audio-session-interrupted');
          _persistFailures();
          _events.add('interrupted');
        } else {
          record('audio-session');
          _sampleProgress();
        }
        _updateSession();
      }).toJS;
    } catch (_) {/* Audio Session is optional. */}
    for (final event in [
      'playing',
      'pause',
      'ended',
      'timeupdate',
      'loadedmetadata',
      'durationchange',
      'waiting',
      'stalled',
      'seeking',
      'seeked',
      'volumechange',
      'error',
      'abort',
      'emptied',
      'loadstart',
      'suspend',
      'canplay',
      'progress'
    ]) {
      final callback = ((web.Event _) {
        if (!sourceMatches) {
          if (event != 'timeupdate') record('ignored-event', {'kind': event});
          return;
        }
        var forwarded = event;
        if (event == 'seeking' ||
            event == 'seeked' ||
            event == 'loadedmetadata') {
          _progress.rebase(position);
          if (event == 'seeking') _progress.buffer();
        } else if (event == 'waiting' || event == 'stalled') {
          _progress.buffer();
        } else if (event == 'pause' && paused) {
          if (_progress.requested) _pausedAt = DateTime.now();
          if (_progress.requested &&
              (!_foreground ||
                  web.document.hidden ||
                  _audioSession?.state == 'interrupted')) {
            _progress.interrupt();
            forwarded = 'interrupted';
          } else {
            _progress.stop(PlaybackPhase.paused);
          }
          _pausedAt ??= DateTime.now();
        } else if (event == 'ended' && element.ended) {
          _progress.stop(PlaybackPhase.ended);
        } else if (event == 'error') {
          if (element.error?.code == 1) {
            _progress.interrupt();
          } else {
            _progress.stop(PlaybackPhase.failed);
            _renewSource = true;
          }
        }
        _sampleProgress();
        _updateSession();
        if (event != 'timeupdate' && event != 'progress') record(event);
        // An explicit media abort is different from a network/decode failure.
        _events.add(event == 'error' && element.error?.code == 1
            ? 'media-aborted'
            : forwarded);
      }).toJS;
      _listeners[event] = callback;
      element.addEventListener(event, callback);
    }
    _action('play', (_) => onPlay?.call());
    _action('pause', (_) => onPause?.call());
    _action('nexttrack', (_) => onNext?.call());
    _action('previoustrack', (_) => onPrevious?.call());
    _action('seekto', (details) => seek(details.seekTime ?? position));
    _action('seekbackward',
        (details) => seek(position - (details.seekOffset ?? 10)));
    _action('seekforward',
        (details) => seek(position + (details.seekOffset ?? 10)));
  }

  void _action(String action, void Function(_MediaAction) callback) {
    try {
      web.window.navigator.mediaSession.setActionHandler(
          action,
          ((_MediaAction details) {
            record('media-action', {'action': action});
            if (action == 'play' && (!_foreground || web.document.hidden)) {
              // Keep the remote command even if the browser suspends timers
              // and never resolves play(). It is a request, not proof of audio.
              _reports.capture('background-resume-request');
              _persistFailures();
            }
            onReconcile?.call('media-action:$action');
            callback(details);
          }).toJS);
      _actions.add(action);
    } catch (_) {
      record('media-action-unavailable', {'action': action});
    }
  }

  void _updateSession() {
    try {
      final session = web.window.navigator.mediaSession;
      session.playbackState = phase == PlaybackPhase.idle
          ? 'none'
          : phase == PlaybackPhase.playing
              ? 'playing'
              : 'paused';
      if (duration > 0)
        session.setPositionState(web.MediaPositionState(
            duration: duration,
            position: position.clamp(0, duration),
            playbackRate:
                element.playbackRate == 0 ? 1 : element.playbackRate));
    } catch (_) {/* Some browsers omit position reporting. */}
  }

  @override
  Stream<String> get events => _events.stream;
  @override
  double get position => element.currentTime.isFinite ? element.currentTime : 0;
  @override
  double get duration => element.duration.isFinite ? element.duration : 0;
  @override
  bool get paused => element.paused;

  bool get repeatOne => element.loop;
  set repeatOne(bool enabled) => element.loop = enabled;

  @override
  void load(Uri uri, Map<String, dynamic> track) {
    _attempt++;
    _watchdog?.cancel();
    _progress.stop(PlaybackPhase.idle);
    _loadedAt = DateTime.now();
    _lastProgressAt = null;
    _pausedAt = null;
    _pauseDurationMs = 0;
    _everAdvanced = _resume = _renewSource = false;
    _track = extractTrackId(track) ?? '';
    _session = uri.queryParameters['playback_session'] ?? '';
    element.src = uri.toString();
    _expectedSource = element.src;
    element.load();
    record('load');
    syncMetadata(track);
  }

  @override
  void syncMetadata(Map<String, dynamic> track) {
    try {
      final image = track['thumbnail']?.toString() ?? '';
      web.window.navigator.mediaSession.metadata =
          web.MediaMetadata(web.MediaMetadataInit(
        title: track['title']?.toString() ?? 'Track',
        artist: (track['artist'] ?? track['author'] ?? track['channel'])
                ?.toString() ??
            '',
        album: track['album']?.toString() ?? '',
        artwork: [if (image.isNotEmpty) web.MediaImage(src: image)].toJS,
      ));
    } catch (_) {/* Metadata support does not gate playback. */}
    _updateSession();
  }

  @override
  Future<void> play() async {
    final attempt = ++_attempt;
    _pauseDurationMs = _pausedAt == null
        ? 0
        : DateTime.now().difference(_pausedAt!).inMilliseconds;
    _resume = _everAdvanced;
    _progress.begin(position);
    _deadline = DateTime.now().add(Duration(seconds: _resume ? 12 : 90));
    try {
      _audioSession?.type = 'playback';
    } catch (_) {}
    _watchdog?.cancel();
    // Observation only: this timer does not keep a background page alive.
    _watchdog =
        Timer.periodic(const Duration(seconds: 1), (_) => _checkProgress());
    _updateSession();
    // A terminal MEDIA_ERR_ABORTED cannot be restarted by play() alone.
    // Reload the same selected URL; do not resolve a different source.
    if (sourceMatches && element.error?.code == 1) {
      element.load();
      record('aborted-source-reload');
    }
    record('play-call');
    try {
      await element.play().toDart;
      if (attempt != _attempt) return;
      record('play-accepted');
      _sampleProgress();
    } catch (error) {
      if (attempt != _attempt) return;
      final name = _domExceptionName(error);
      final cause = name == 'NotAllowedError'
          ? 'permission'
          : name == 'AbortError'
              ? 'abort'
              : 'unknown';
      final kind = cause == 'permission' || cause == 'abort'
          ? PlaybackStartFailure.interrupted
          : PlaybackStartFailure.failed;
      if (kind == PlaybackStartFailure.interrupted) {
        _progress.interrupt();
      } else {
        _progress.stop(PlaybackPhase.failed);
        _renewSource = true;
      }
      _watchdog?.cancel();
      _updateSession();
      record('play-rejected', {
        'cause': cause,
        'errorType': error.runtimeType.toString(),
        'exceptionName': name,
        'exceptionMessage': _safeErrorText(_domExceptionField(error, 'message'))
      });
      throw PlaybackStartException(kind, cause);
    }
  }

  String _domExceptionName(Object error) {
    return _domExceptionField(error, 'name');
  }

  String _safeErrorText(String value) {
    return PlaybackDiagnostics.safeText(value);
  }

  void _sampleProgress() {
    final previous = phase;
    if (_progress.observe(position,
        paused: paused ||
            element.error != null ||
            element.readyState < 3 ||
            _audioSession?.state == 'interrupted',
        seeking: element.seeking)) {
      _lastProgressAt = DateTime.now();
      _deadline = _lastProgressAt!.add(const Duration(seconds: 20));
      _everAdvanced = true;
    }
    if (phase != previous) {
      _updateSession();
      record('playback-state');
      _events.add('playback-state');
    }
  }

  void _checkProgress() {
    if (!_progress.requested || !sourceMatches) return;
    _sampleProgress();
    final now = DateTime.now();
    if (phase == PlaybackPhase.playing &&
        _lastProgressAt != null &&
        now.difference(_lastProgressAt!).inSeconds >= 4 &&
        !element.seeking) {
      _progress.buffer();
      _updateSession();
      record('playback-stalled');
      _events.add('playback-state');
    }
    // Suspension pauses the observation budget; foreground samples first.
    if (!_foreground ||
        web.document.hidden ||
        _audioSession?.state == 'interrupted' ||
        element.seeking) return;
    if (_deadline != null && now.isAfter(_deadline!)) {
      _renewSource = true;
      _progress.interrupt();
      _watchdog?.cancel();
      final reason = _resume
          ? 'resume-stalled'
          : _everAdvanced
              ? 'playback-stalled-timeout'
              : 'startup-stalled';
      _updateSession();
      record(reason, {'failed': true});
      _events.add(reason);
    }
  }

  @override
  void foreground() {
    _foreground = true;
    if (!_progress.requested) return;
    _sampleProgress();
    _deadline = DateTime.now().add(Duration(seconds: _everAdvanced ? 12 : 90));
    _updateSession();
    record('foreground-check');
  }

  void background() => _foreground = false;

  web.File _reportFile(Map<String, dynamic> report) => web.File(
      [const JsonEncoder.withIndent('  ').convert(report).toJS].toJS,
      'neatie-playback-${report['reportId']}.txt',
      web.FilePropertyBag(type: 'text/plain;charset=utf-8'));

  bool canShareReport(Map<String, dynamic> report) {
    try {
      return web.window.navigator
          .canShare(web.ShareData(files: [_reportFile(report)].toJS));
    } catch (_) {
      return false;
    }
  }

  Future<void> shareReport(Map<String, dynamic> report) async {
    // Invoke share directly within the user's tap, before any await.
    await web.window.navigator
        .share(web.ShareData(
            title: 'Neatie playback report', files: [_reportFile(report)].toJS))
        .toDart;
  }

  void downloadReport(Map<String, dynamic> report) {
    final file = _reportFile(report);
    final url = web.URL.createObjectURL(file);
    final anchor = web.HTMLAnchorElement()
      ..href = url
      ..download = file.name;
    anchor.style.display = 'none';
    web.document.body?.append(anchor);
    anchor.click();
    anchor.remove();
    Timer(const Duration(seconds: 60), () => web.URL.revokeObjectURL(url));
  }

  String _domExceptionField(Object error, String field) {
    // JS promises may reject with DOMException objects that do not preserve
    // their Dart wrapper type across browser implementations.
    try {
      final value = (error as JSObject).getProperty<JSString>(field.toJS);
      return value.toDart;
    } catch (_) {
      return '';
    }
  }

  @override
  void pause() {
    _attempt++;
    _pausedAt = DateTime.now();
    _progress.stop(PlaybackPhase.paused);
    _watchdog?.cancel();
    element.pause();
    _updateSession();
  }

  @override
  void seek(double seconds) {
    if (duration > 0) {
      element.currentTime = seconds.clamp(0, duration);
      _progress.rebase(element.currentTime);
    }
    _updateSession();
  }

  @override
  void dispose() {
    _attempt++;
    _watchdog?.cancel();
    _audioSession?.onstatechange = null;
    for (final entry in _listeners.entries)
      element.removeEventListener(entry.key, entry.value);
    for (final action in _actions) {
      web.window.navigator.mediaSession.setActionHandler(action, null);
    }
    element.pause();
    element.removeAttribute('src');
    element.load();
    element.remove();
    _events.close();
  }
}
