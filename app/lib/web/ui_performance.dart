import 'dart:async';
import 'dart:js_interop';
import 'dart:ui' show FrameTiming;
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:web/web.dart' as web;
import 'playback_diagnostics.dart';

/// Six-second foreground samples, at most one per twelve seconds. RAF runs
/// only inside a sample; Flutter timing callbacks never request more frames.
class UiPerformanceProbe extends StatefulWidget {
  const UiPerformanceProbe(
      {super.key,
      required this.surface,
      required this.enabled,
      required this.onSummary,
      required this.child});
  final String surface;
  final bool enabled;
  final void Function(Map<String, dynamic>) onSummary;
  final Widget child;
  @override
  UiPerformanceProbeState createState() => UiPerformanceProbeState();
}

class UiPerformanceProbeState extends State<UiPerformanceProbe> {
  FrameTimeDistribution? _frames, _build, _raster, _raf;
  Timer? _end;
  int? _request;
  late final JSFunction _callback, _visibility;
  final _clock = Stopwatch()..start();
  int _lastStart = -12000, _started = 0, _changes = 0;
  double? _lastRaf;
  String _surface = '', _trigger = '';

  @override
  void initState() {
    super.initState();
    _callback = ((double now) {
      _request = null;
      if (_frames == null) return;
      if (!widget.enabled || web.document.hidden) {
        _finish();
        return;
      }
      if (_lastRaf != null) _raf!.add(now - _lastRaf!);
      _lastRaf = now;
      _request = web.window.requestAnimationFrame(_callback);
    }).toJS;
    _visibility = ((web.Event _) {
      if (web.document.hidden) _finish();
    }).toJS;
    web.document.addEventListener('visibilitychange', _visibility);
    SchedulerBinding.instance.addTimingsCallback(_timings);
    _start('view');
  }

  void _start(String trigger) {
    if (!widget.enabled ||
        web.document.hidden ||
        _frames != null ||
        _clock.elapsedMilliseconds - _lastStart < 12000) return;
    _lastStart = _started = _clock.elapsedMilliseconds;
    _surface = widget.surface;
    _trigger = trigger;
    _changes = 0;
    _frames = FrameTimeDistribution();
    _build = FrameTimeDistribution();
    _raster = FrameTimeDistribution();
    _raf = FrameTimeDistribution();
    _lastRaf = null;
    _request = web.window.requestAnimationFrame(_callback);
    _end = Timer(const Duration(seconds: 6), _finish);
  }

  void _timings(List<FrameTiming> timings) {
    if (_frames == null || !widget.enabled || web.document.hidden) return;
    for (final timing in timings) {
      _frames!.add(timing.totalSpan.inMicroseconds / 1000);
      _build!.add(timing.buildDuration.inMicroseconds / 1000);
      _raster!.add(timing.rasterDuration.inMicroseconds / 1000);
    }
  }

  void _finish({bool emit = true}) {
    _end?.cancel();
    _end = null;
    if (_request != null) web.window.cancelAnimationFrame(_request!);
    _request = null;
    final frames = _frames;
    if (frames == null) return;
    _frames = null;
    if (emit && (_raf!.count > 0 || frames.count > 0)) {
      widget.onSummary({
        'surface': _surface,
        'endSurface': widget.surface,
        'trigger': _trigger,
        'windowMs': _clock.elapsedMilliseconds - _started,
        'viewChanges': _changes,
        'frameCount': frames.count,
        'frameTimingSupported': frames.count > 0,
        'buildP95UpperMs': _build!.p95UpperMs,
        'rasterP95UpperMs': _raster!.p95UpperMs,
        'frameP95UpperMs': frames.p95UpperMs,
        'frameMaxMs': frames.maxMs,
        'frameOver16Count': frames.over16,
        'frameOver33Count': frames.over33,
        'rafCount': _raf!.count,
        'rafP95UpperMs': _raf!.p95UpperMs,
        'rafMaxMs': _raf!.maxMs,
        'rafOver33Count': _raf!.over33,
        'rafOver50Count': _raf!.over50,
      });
    }
  }

  void flush() => _finish();

  @override
  void didUpdateWidget(covariant UiPerformanceProbe oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!widget.enabled) {
      _finish();
      return;
    }
    if (oldWidget.surface != widget.surface) {
      if (_frames != null) _changes++;
      _start('view');
    }
    if (!oldWidget.enabled) _start('foreground');
  }

  @override
  void dispose() {
    _finish(emit: false);
    SchedulerBinding.instance.removeTimingsCallback(_timings);
    web.document.removeEventListener('visibilitychange', _visibility);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Listener(
      onPointerDown: (_) => _start('interaction'),
      onPointerSignal: (_) => _start('scroll'),
      child: NotificationListener<ScrollNotification>(
          onNotification: (_) {
            _start('scroll');
            return false;
          },
          child: widget.child));
}
