import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'package:web/web.dart' as web;
import 'playback_port.dart';
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
  BrowserSessionStore(String backend, {String scope = 'guest'}) : key = 'neatie:$scope:session:$backend';
  @override
  String? read() => web.window.localStorage.getItem(key);
  @override
  void write(String value) => web.window.localStorage.setItem(key, value);
}

/// One persistent media element owns playback across page rebuilds and tracks.
/// No Web Audio context or background timer is needed for queue advancement.
class BrowserAudio implements PlaybackPort {
  final web.HTMLAudioElement element = web.HTMLAudioElement();
  final _events = StreamController<String>.broadcast(sync: true);
  final Map<String, JSFunction> _listeners = {};
  final List<String> _actions = [];
  final List<Map<String, dynamic>> _diagnostics = [];
  final List<List<Map<String, dynamic>>> _failures = [];
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
  bool get sourceMatches => _expectedSource.isNotEmpty &&
      element.src == _expectedSource &&
      (element.currentSrc.isEmpty || element.currentSrc == _expectedSource);
  String get diagnostics {
    _restoreFailures();
    return [
    for (final failure in _failures) ...failure,
    ..._diagnostics,
  ].map(jsonEncode).toSet().join('\n');
  }

  void setDiagnosticScope(String backend, String scope) {
    _diagnosticKey = 'neatie:$scope:playback-failures:$backend';
    _diagnostics.clear();
    _failures.clear();
    _restoreFailures();
  }

  void _restoreFailures() {
    if (_diagnosticKey.isEmpty || _failures.isNotEmpty) return;
    try {
      final saved = web.window.localStorage.getItem(_diagnosticKey);
      if (saved == null || saved.length > 65536) return;
      final decoded = jsonDecode(saved) as List;
      for (final item in decoded.take(8)) {
        _failures.add((item as List).cast<Map>().map((e) =>
            Map<String, dynamic>.from(e)).toList());
      }
    } catch (_) { /* Storage may be disabled or full. */ }
  }

  void record(String event, [Map<String, dynamic> extra = const {}]) {
    _diagnostics.add({
      'at': DateTime.now().toUtc().toIso8601String(), 'event': event,
      'track': _track, 'session': _session, 'time': position,
      'paused': paused, 'muted': element.muted, 'volume': element.volume,
      'ready': element.readyState, 'network': element.networkState,
      'mediaCode': element.error?.code,
      'mediaMessage': _safeErrorText(element.error?.message ?? ''),
      'bufferedEnd': element.buffered.length > 0 ? element.buffered.end(element.buffered.length - 1) : 0,
      'visibility': web.document.visibilityState,
      'audioSession': _audioSession?.state, ...extra,
      'repeatOne': element.loop,
      'sourceMatches': sourceMatches,
      'elementSession': Uri.tryParse(element.currentSrc)?.queryParameters['playback_session'],
      ...?diagnosticContext?.call(),
    });
    if (event == 'error' || event.contains('failed') || event.contains('rejected') ||
        event == 'api-failure' || extra['failed'] == true || (extra['status'] is int && (extra['status'] as int) >= 400)) {
      _failures.add(_diagnostics.skip((_diagnostics.length - 12).clamp(0, _diagnostics.length)).toList());
      if (_failures.length > 10) _failures.removeAt(0);
      _persistFailures();
    }
    if (_diagnostics.length > 100) _diagnostics.removeAt(0);
  }

  void _persistFailures() {
    if (_diagnosticKey.isEmpty) return;
    try {
      const allowed = {'at', 'event', 'track', 'session', 'time', 'paused',
        'muted', 'volume', 'ready', 'network', 'mediaCode', 'mediaMessage', 'bufferedEnd',
        'visibility', 'audioSession', 'repeatOne', 'sourceMatches',
        'elementSession', 'selectedTrack', 'loadedTrack', 'queueIndex',
        'operation', 'loading', 'startupInterrupted', 'cause', 'errorType',
        'exceptionName', 'exceptionMessage', 'awaitingFirstPlayback', 'hiddenDuringStartup',
        'path', 'ms', 'requestedTrack', 'status', 'action', 'kind', 'reason',
        'error', 'failed', 'prepared', 'fullyCached', 'build'};
      final bounded = _failures.skip((_failures.length - 8).clamp(0, _failures.length)).map((snapshot) => snapshot.take(12).map(
          (row) => Map<String, dynamic>.fromEntries(row.entries.where(
              (entry) => allowed.contains(entry.key)))).toList()).toList();
      var encoded = jsonEncode(bounded);
      while (encoded.length > 65536 && bounded.length > 1) {
        bounded.removeAt(0);
        encoded = jsonEncode(bounded);
      }
      if (encoded.length <= 65536) web.window.localStorage.setItem(_diagnosticKey, encoded);
    } catch (_) { /* Failure history is best effort. */ }
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
      _audioSession?.onstatechange = ((web.Event _) => record('audio-session')).toJS;
    } catch (_) { /* Audio Session is optional. */ }
    record('device', {'agent': web.window.navigator.userAgent});
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
      'error'
    ]) {
      final callback = ((web.Event _) {
        if (!sourceMatches) {
          if (event != 'timeupdate') record('ignored-event', {'kind': event});
          return;
        }
        _updateSession();
        if (event != 'timeupdate') record(event);
        // An explicit media abort is different from a network/decode failure.
        _events.add(event == 'error' && element.error?.code == 1
            ? 'media-aborted' : event);
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
      web.window.navigator.mediaSession.setActionHandler(action, ((_MediaAction details) {
        record('media-action', {'action': action});
        onReconcile?.call('media-action:$action');
        callback(details);
      }).toJS);
      _actions.add(action);
    } catch (_) { record('media-action-unavailable', {'action': action}); }
  }

  void _updateSession() {
    try {
      final session = web.window.navigator.mediaSession;
      session.playbackState = paused ? 'paused' : 'playing';
      if (duration > 0)
        session.setPositionState(web.MediaPositionState(
            duration: duration,
            position: position.clamp(0, duration),
            playbackRate: 1));
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
    // A terminal MEDIA_ERR_ABORTED cannot be restarted by play() alone.
    // Reload the same selected URL; do not resolve a different source.
    if (sourceMatches && element.error?.code == 1) {
      element.load();
      record('aborted-source-reload');
    }
    record('play-call');
    try {
      await element.play().toDart;
      record('play-accepted');
    } catch (error) {
      final name = _domExceptionName(error);
      final cause = name == 'NotAllowedError' ? 'permission' :
          name == 'AbortError' ? 'abort' : 'unknown';
      final kind = cause == 'permission' || cause == 'abort'
          ? PlaybackStartFailure.interrupted : PlaybackStartFailure.failed;
      record('play-rejected', {'cause': cause, 'errorType': error.runtimeType.toString(),
        'exceptionName': name, 'exceptionMessage': _safeErrorText(_domExceptionField(error, 'message'))});
      throw PlaybackStartException(kind, cause);
    }
  }

  String _domExceptionName(Object error) {
    return _domExceptionField(error, 'name');
  }

  String _safeErrorText(String value) {
    final cleaned = value.replaceAll(RegExp(r'https?://[^\s]+'), '[url]');
    return cleaned.substring(0, cleaned.length.clamp(0, 240));
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
  void pause() => element.pause();
  @override
  void seek(double seconds) {
    if (duration > 0) element.currentTime = seconds.clamp(0, duration);
    _updateSession();
  }

  @override
  void dispose() {
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
