import 'dart:async';
import 'dart:js_interop';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:web/web.dart' as web;

Widget vinylSurface(
        {required List<String> urls,
        required double diameter,
        Object? retryKey,
        required bool playing}) =>
    _VinylSurface(urls: urls, diameter: diameter, playing: playing, retryKey: retryKey);

/// CSS owns the decorative rotation, so it does not rasterize Flutter's scene
/// every display frame. Controls, queue and the sole audio element stay Flutter.
class _VinylSurface extends StatefulWidget {
  const _VinylSurface(
      {required this.urls, required this.diameter, required this.playing, this.retryKey});
  final List<String> urls;
  final Object? retryKey;
  final double diameter;
  final bool playing;
  @override
  State<_VinylSurface> createState() => _VinylSurfaceState();
}

class _VinylSurfaceState extends State<_VinylSurface> {
  web.HTMLDivElement? _disc;
  web.HTMLDivElement? _label, _hole;
  web.HTMLImageElement? _image;
  JSFunction? _error, _load;
  Timer? _stall;
  int _candidate = 0, _epoch = 0;

  void _clearImage() {
    final image = _image;
    if (image == null) return;
    if (_error != null) image.removeEventListener('error', _error!);
    if (_load != null) image.removeEventListener('load', _load!);
    _error = _load = null;
    _image = null;
    image.removeAttribute('src');
    image.remove();
  }

  void _source() {
    _stall?.cancel();
    final epoch = ++_epoch;
    _clearImage();
    final label = _label, hole = _hole;
    if (label == null || hole == null || _candidate >= widget.urls.length) return;
    final image = web.HTMLImageElement()..alt = '';
    image.style.cssText = 'width:100%;height:100%;object-fit:cover;';
    _image = image;
    _error = ((web.Event _) {
      if (mounted && epoch == _epoch && identical(_image, image)) _nextSource();
    }).toJS;
    _load = ((web.Event _) {
      if (mounted && epoch == _epoch && identical(_image, image)) _stall?.cancel();
    }).toJS;
    image.addEventListener('error', _error!);
    image.addEventListener('load', _load!);
    label.insertBefore(image, hole);
    image.src = widget.urls[_candidate];
    _stall = Timer(_candidate + 1 < widget.urls.length
        ? const Duration(milliseconds: 1200) : const Duration(seconds: 4), () {
      if (mounted && epoch == _epoch && identical(_image, image)) _nextSource();
    });
  }

  void _nextSource() {
    if (!mounted || _candidate >= widget.urls.length) return;
    _candidate++;
    _source();
  }

  void _sync() {
    _disc?.style.animationPlayState = widget.playing ? 'running' : 'paused';
    _disc?.style.willChange = widget.playing ? 'transform' : 'auto';
  }

  void _created(Object element) {
    final root = element as web.HTMLDivElement;
    root.setAttribute('aria-hidden', 'true');
    root.setAttribute('data-neatie-vinyl', 'compositor');
    root.style.cssText =
        'width:100%;height:100%;pointer-events:none;contain:strict;overflow:hidden;border-radius:50%;';
    final style = web.HTMLStyleElement()
      ..textContent =
          '@keyframes neatie-record-turn{to{transform:rotate(360deg)}} @media(prefers-reduced-motion:reduce){.neatie-native-record{animation:none!important}}';
    final disc = web.HTMLDivElement()..className = 'neatie-native-record';
    disc.style.cssText =
        'position:relative;width:100%;height:100%;border-radius:50%;animation:neatie-record-turn 28s linear infinite;'
        'background:repeating-radial-gradient(circle at center,transparent 0px,transparent 2.15px,rgba(122,128,102,.086) 2.15px,rgba(122,128,102,.086) 2.7px),radial-gradient(circle,#ececda 0%,#d2d5bd 60%,#e5e6d2 100%);';
    final label = web.HTMLDivElement();
    label.style.cssText =
        'position:absolute;left:39%;top:39%;width:22%;height:22%;overflow:hidden;border-radius:50%;background:#151714;';
    final hole = web.HTMLDivElement();
    hole.style.cssText =
        'position:absolute;left:50%;top:50%;width:9px;height:9px;border-radius:50%;background:#151714;transform:translate(-50%,-50%);';
    label.appendChild(hole);
    disc.appendChild(label);
    root.appendChild(style);
    root.appendChild(disc);
    _disc = disc;
    _label = label;
    _hole = hole;
    _source();
    _sync();
  }

  @override
  void didUpdateWidget(covariant _VinylSurface old) {
    super.didUpdateWidget(old);
    if (!listEquals(widget.urls, old.urls) ||
        (widget.retryKey != old.retryKey && _candidate >= widget.urls.length)) {
      _candidate = 0;
      _source();
    }
    _sync();
  }

  @override
  void dispose() {
    _stall?.cancel();
    _epoch++;
    _clearImage();
    _disc?.style.animationPlayState = 'paused';
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => SizedBox(
      width: widget.diameter,
      height: widget.diameter,
      child: HtmlElementView.fromTagName(
          tagName: 'div', onElementCreated: _created));
}
