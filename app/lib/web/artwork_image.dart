import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';

/// Tries supplied artwork alternatives before showing the empty-image icon.
class ArtworkImage extends StatefulWidget {
  static final _preferred = <String, ({String url, DateTime until})>{};
  static final _googleSquareSize = RegExp(r'=w([0-9]{1,4})-h\1(?=-|$)');

  static List<String> sources(
      List<String> urls, double logicalSize, double dpr) {
    final pixels = logicalSize * dpr;
    final size = pixels <= 160
        ? 160
        : pixels <= 384
            ? 384
            : 768;
    final result = <String>[];
    for (final url in urls) {
      final uri = Uri.tryParse(url);
      if (uri != null &&
          RegExp(r'^/(?:artist|entity)_artwork/[a-f0-9]{32}$')
              .hasMatch(uri.path))
        result.add(uri.replace(queryParameters: {
          ...uri.queryParameters,
          'size': '$size'
        }).toString());
      if (logicalSize >= 280 &&
          uri != null &&
          uri.scheme == 'https' &&
          uri.host == 'lh3.googleusercontent.com' &&
          !uri.hasQuery) {
        final match = _googleSquareSize.firstMatch(uri.path);
        if (match != null && int.parse(match.group(1)!) < size) {
          result.add(uri.replace(path: uri.path.replaceRange(
              match.start, match.end, '=w$size-h$size')).toString());
        }
      }
      result.add(url);
    }
    return result.toSet().toList();
  }

  static List<String> _ordered(List<String> urls) {
    final saved = _preferred[urls.join('\n')];
    if (saved == null ||
        saved.until.isBefore(DateTime.now()) ||
        !urls.contains(saved.url)) return urls;
    return [saved.url, ...urls.where((url) => url != saved.url)];
  }

  static void _remember(List<String> urls, String url) {
    final key = urls.join('\n');
    _preferred.remove(key);
    _preferred[key] =
        (url: url, until: DateTime.now().add(const Duration(minutes: 5)));
    while (_preferred.length > 128) _preferred.remove(_preferred.keys.first);
  }

  static Future<bool> warm(BuildContext context, List<String> urls, double size,
      {bool Function()? active}) async {
    final candidates =
        sources(urls, size, MediaQuery.devicePixelRatioOf(context));
    final configuration = createLocalImageConfiguration(context);
    for (final url in _ordered(candidates)) {
      if (active?.call() == false) return false;
      final stream = NetworkImage(url).resolve(configuration);
      final done = Completer<bool>();
      final listener = ImageStreamListener((_, __) {
        if (!done.isCompleted) done.complete(true);
      }, onError: (_, __) {
        if (!done.isCompleted) done.complete(false);
      });
      stream.addListener(listener);
      final loaded = await done.future
          .timeout(const Duration(milliseconds: 1200), onTimeout: () => false);
      stream.removeListener(listener);
      if (loaded) {
        _remember(candidates, url);
        return true;
      }
    }
    return false;
  }

  const ArtworkImage(
      {super.key,
      required this.urls,
      required this.width,
      required this.height,
      this.icon = Icons.music_note,
      this.placeholder,
      this.diagnosticKey,
      this.cardKey,
      this.stallTimeout = const Duration(milliseconds: 500)});

  final List<String> urls;
  final double width, height;
  final IconData icon;
  final Widget? placeholder;
  /// Optional opaque identity for the currently visible player artwork.
  final String? diagnosticKey;
  final String? cardKey;
  final Duration stallTimeout;
  @override
  State<ArtworkImage> createState() => _ArtworkImageState();
}

class _ArtworkImageState extends State<ArtworkImage> {
  late List<String> _urls = widget.urls;
  List<String>? _base;
  int _index = 0, _generation = 0, _next = 1, _attempts = 1;
  int? _alternate, _winner;
  ArtworkDiagnosticsState? _telemetry;
  String? _diagnosticKey;
  bool _tracked = false;
  final _watch = Stopwatch()..start();
  final _bytesCompleteAt = <int, int>{};
  Timer? _stall;
  bool _loaded = false;

  void _arm() {
    _stall?.cancel();
    final generation = _generation;
    if (!_loaded && _index < _urls.length)
      _stall = Timer(
          _alternate == null && _next < _urls.length
              ? widget.stallTimeout
              : const Duration(seconds: 4),
          () {
        if (!mounted || generation != _generation || _loaded) return;
        if (_alternate != null || _next >= _urls.length) {
          PaintingBinding.instance.imageCache.evict(
              NetworkImage(_urls[_index]), includeLive: false);
          _advance(generation, _index);
          return;
        }
        setState(() {
          _alternate = _next++;
          _attempts++;
        });
        _arm();
      });
  }

  void _advance(int generation, int failed) {
    if (!mounted || generation != _generation || _loaded) return;
    setState(() {
      if (failed == _index) {
        final promoted = _alternate != null;
        _index = _alternate ?? _next++;
        _alternate = null;
        if (!promoted && _index < _urls.length) _attempts++;
      } else if (failed == _alternate) {
        _alternate = _next < _urls.length ? _next++ : null;
        if (_alternate != null) _attempts++;
      }
    });
    if (_index >= _urls.length) _report('failed');
    _arm();
  }

  void _report(String outcome, {bool synchronous = false}) {
    if (!_tracked) return;
    _tracked = false;
    _telemetry?.finish(
      outcome, _watch.elapsedMilliseconds, _attempts, synchronous,
      key: widget.cardKey ?? _diagnosticKey,
      bytesAt: _bytesCompleteAt[_winner],
    );
    if (outcome == 'ready' || outcome == 'failed') {
      _telemetry?.playerOutcome(_diagnosticKey, outcome, _attempts);
    }
  }

  @override
  void initState() {
    super.initState();
    _arm();
  }

  @override
  void didUpdateWidget(covariant ArtworkImage old) {
    super.didUpdateWidget(old);
    if (!listEquals(old.urls, widget.urls) ||
        old.diagnosticKey != widget.diagnosticKey) {
      _updateSources(force: old.diagnosticKey != widget.diagnosticKey);
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _updateSources();
  }

  void _updateSources({bool force = false}) {
    final base = ArtworkImage.sources(
        widget.urls,
        widget.width > widget.height ? widget.width : widget.height,
        MediaQuery.devicePixelRatioOf(context));
    if (!force && listEquals(base, _base)) return;
    _report('cancelled');
    _generation++;
    final current = _index < _urls.length ? _urls[_index] : null;
    _base = base;
    _urls = ArtworkImage._ordered(base);
    final retained = current == null ? -1 : _urls.indexOf(current);
    _index = _loaded && retained >= 0 ? retained : 0;
    if (retained < 0) _loaded = false;
    _winner = _loaded ? _index : null;
    _alternate = null;
    _next = _index + 1;
    _attempts = 1;
    _bytesCompleteAt.clear();
    _watch
      ..reset()
      ..start();
    _telemetry =
        context.getInheritedWidgetOfExactType<_ArtworkDiagnostics>()?.owner;
    _diagnosticKey = widget.diagnosticKey;
    if (_urls.isEmpty) {
      _telemetry?.noSource();
      _telemetry?.playerOutcome(_diagnosticKey, 'no-source', 0);
    } else if (!_loaded && _telemetry != null) {
      _tracked = true;
      _telemetry!.start();
    } else if (_loaded) {
      _telemetry?.playerOutcome(_diagnosticKey, 'ready', 0);
    }
    _arm();
  }

  @override
  void dispose() {
    _stall?.cancel();
    _generation++;
    _report('cancelled');
    super.dispose();
  }

  Widget _fallback() => SizedBox(
      width: widget.width,
      height: widget.height,
      child: widget.placeholder ?? Center(child: Icon(widget.icon)));

  Widget _image(int index) {
    if (index >= _urls.length) return _fallback();
    final generation = _generation;
    return Image.network(_urls[index],
        key: ValueKey(_urls[index]),
        width: widget.width,
        height: widget.height,
        fit: BoxFit.cover,
        loadingBuilder: _telemetry == null ? null : (_, child, progress) {
          if (generation == _generation &&
              progress != null &&
              progress.cumulativeBytesLoaded == progress.expectedTotalBytes) {
            _bytesCompleteAt.putIfAbsent(index, () => _watch.elapsedMilliseconds);
          }
          return child;
        },
        frameBuilder: (context, child, frame, synchronous) {
      if (frame != null && !_loaded && generation == _generation) {
        _loaded = true;
        _winner = index;
        ArtworkImage._remember(_base!, _urls[index]);
        _stall?.cancel();
        _report('ready', synchronous: synchronous);
        scheduleMicrotask(() {
          if (!mounted || generation != _generation) return;
          setState(() {
            _index = index;
            _alternate = null;
          });
        });
      }
      if (synchronous || MediaQuery.disableAnimationsOf(context))
        return Opacity(opacity: _winner == index ? 1 : 0, child: child);
      return AnimatedOpacity(
          opacity: frame != null && _winner == index ? 1 : 0,
          duration: const Duration(milliseconds: 160),
          curve: Curves.easeOut,
          child: child);
    }, errorBuilder: (_, __, ___) {
      scheduleMicrotask(() => _advance(generation, index));
      return SizedBox(width: widget.width, height: widget.height);
    });
  }

  @override
  Widget build(BuildContext context) => SizedBox(
      width: widget.width,
      height: widget.height,
      child: Stack(children: [
        if (!_loaded) _fallback(),
        if (_index < _urls.length) _image(_index),
        if (_alternate != null) _image(_alternate!),
      ]));
}

/// Completion counters only. One small event per ten seconds of image work;
/// capture flushes outstanding counters without individual URLs or image logs.
class ArtworkDiagnosticsScope extends StatefulWidget {
  const ArtworkDiagnosticsScope(
      {super.key, required this.onSummary, required this.child});
  final void Function(Map<String, dynamic>) onSummary;
  final Widget child;
  @override
  ArtworkDiagnosticsState createState() => ArtworkDiagnosticsState();
}

class ArtworkDiagnosticsState extends State<ArtworkDiagnosticsScope> {
  int _started = 0, _ready = 0, _failed = 0, _cancelled = 0, _pending = 0;
  int _attempts = 0, _cacheHits = 0, _maxMs = 0, _totalMs = 0;
  int _noSource = 0;
  String? _playerKey, _playerOutcome;
  int? _playerAttempts;
  String? _slowKey, _slowOutcome;
  int _slowMs = 0;
  int? _slowAttempts, _slowBytesAt;
  static final _validKey = RegExp(r'^[A-Za-z0-9_:-]{1,80}$');
  bool _dirty = false;
  Timer? _timer;
  void _schedule() {
    _dirty = true;
    _timer ??= Timer(const Duration(seconds: 10), flush);
  }

  void start() {
    _started++;
    _pending++;
    _schedule();
  }

  void noSource() {
    _noSource++;
    _schedule();
  }

  void playerOutcome(String? key, String outcome, int attempts) {
    if (key == null || !RegExp(r'^[A-Za-z0-9_-]{1,64}$').hasMatch(key)) return;
    _playerKey = key;
    _playerOutcome = outcome;
    _playerAttempts = attempts;
    _schedule();
  }

  void finish(String outcome, int ms, int attempts, bool cacheHit,
      {String? key, int? bytesAt}) {
    if (!mounted) return;
    if (_pending > 0) _pending--;
    if (outcome == 'ready') _ready++;
    if (outcome == 'failed') _failed++;
    if (outcome == 'cancelled') _cancelled++;
    _attempts += attempts;
    if (cacheHit) _cacheHits++;
    _totalMs += ms;
    if (ms > _maxMs) _maxMs = ms;
    if (outcome != 'cancelled' &&
        ms >= _slowMs &&
        key != null &&
        _validKey.hasMatch(key)) {
      _slowKey = key;
      _slowOutcome = outcome;
      _slowMs = ms;
      _slowAttempts = attempts;
      _slowBytesAt = bytesAt;
    }
    _schedule();
  }

  void flush() {
    _timer?.cancel();
    _timer = null;
    if (!_dirty) return;
    _dirty = false;
    widget.onSummary({
      'imageCount': _started,
      'imageNoSourceCount': _noSource,
      if (_playerKey != null) 'imagePlayerKey': _playerKey,
      if (_playerOutcome != null) 'imagePlayerOutcome': _playerOutcome,
      if (_playerAttempts != null) 'imagePlayerAttempts': _playerAttempts,
      'imageReadyCount': _ready,
      'imageFailedCount': _failed,
      'imageCancelledCount': _cancelled,
      'imagePendingCount': _pending,
      'imageAttemptCount': _attempts,
      'imageCacheHitCount': _cacheHits,
      'imageMaxMs': _maxMs,
      if (_slowKey != null) ...{
        'imageSlowKey': _slowKey,
        'imageSlowOutcome': _slowOutcome,
        'imageSlowMs': _slowMs,
        'imageSlowAttempts': _slowAttempts,
        if (_slowBytesAt != null) ...{
          'imageSlowBytesMs': _slowBytesAt,
          'imageSlowFrameAfterBytesMs': _slowMs - _slowBytesAt!,
        },
      },
      'imageTotalMs': _totalMs
    });
    _started = _ready =
        _failed = _cancelled = _attempts = _cacheHits = _maxMs = _totalMs = 0;
    _noSource = 0;
    _playerKey = _playerOutcome = null;
    _playerAttempts = null;
    _slowKey = _slowOutcome = null;
    _slowMs = 0;
    _slowAttempts = _slowBytesAt = null;
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      _ArtworkDiagnostics(owner: this, child: widget.child);
}

class _ArtworkDiagnostics extends InheritedWidget {
  const _ArtworkDiagnostics({required this.owner, required super.child});
  final ArtworkDiagnosticsState owner;
  @override
  bool updateShouldNotify(_ArtworkDiagnostics oldWidget) =>
      owner != oldWidget.owner;
}

/// Warm a bounded preview of accepted sections without mounting their widgets.
class ArtworkWarmup extends StatefulWidget {
  const ArtworkWarmup(
      {super.key,
      required this.items,
      required this.enabled,
      required this.child});
  final List<({List<String> urls, double size})> items;
  final bool enabled;
  final Widget child;
  @override
  State<ArtworkWarmup> createState() => _ArtworkWarmupState();
}

class _ArtworkWarmupState extends State<ArtworkWarmup> {
  int _generation = 0;
  bool _running = false;
  Timer? _timer;
  final _done = <String>{};
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _schedule();
  }

  @override
  void didUpdateWidget(covariant ArtworkWarmup old) {
    super.didUpdateWidget(old);
    if (old.enabled == widget.enabled &&
        old.items.length == widget.items.length &&
        List.generate(
                widget.items.length,
                (i) =>
                    old.items[i].size == widget.items[i].size &&
                    listEquals(old.items[i].urls, widget.items[i].urls))
            .every((same) => same)) return;
    _schedule();
  }

  void _schedule() {
    final generation = ++_generation;
    _timer?.cancel();
    if (!widget.enabled || _running) return;
    _timer = Timer(const Duration(milliseconds: 250), () {
      _running = true;
      final items = widget.items.take(32).toList();
      var next = 0;
      Future<void> worker() async {
        while (mounted &&
            generation == _generation &&
            widget.enabled &&
            next < items.length) {
          final item = items[next++];
          final key = ArtworkImage.sources(
                  item.urls, item.size, MediaQuery.devicePixelRatioOf(context))
              .join('\n');
          if (_done.contains(key)) continue;
          if (await ArtworkImage.warm(context, item.urls, item.size,
              active: () =>
                  mounted && generation == _generation && widget.enabled)) {
            _done.add(key);
            while (_done.length > 128) _done.remove(_done.first);
          }
        }
      }

      unawaited(Future.wait([worker(), worker()]).whenComplete(() {
        _running = false;
        if (mounted && generation != _generation) _schedule();
      }));
    });
  }

  @override
  void dispose() {
    ++_generation;
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
