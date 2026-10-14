import 'dart:async';
import 'package:flutter/material.dart';
import '../logic/track_metadata.dart';
import 'design_system.dart';
import 'web_player.dart';

/// Presentation for Android's existing /lyrics contract; shares the live player.
class LyricsPanel extends StatefulWidget {
  const LyricsPanel({super.key, required this.player});
  final WebPlayer player;
  @override
  State<LyricsPanel> createState() => _LyricsPanelState();
}

class _LyricsPanelState extends State<LyricsPanel> {
  Map<String, dynamic> data = {};
  String selected = '';
  int generation = 0, active = -1;
  bool loading = false, failed = false, follow = true;
  List<GlobalKey> lineKeys = [];
  List<Map<String, dynamic>> get lines => (data['lines'] as List? ?? [])
      .whereType<Map>()
      .map((value) => Map<String, dynamic>.from(value))
      .where((value) => '${value['text'] ?? ''}'.trim().isNotEmpty)
      .toList();
  bool get synced => data['has_timestamps'] == true;
  String get currentKey =>
      '${widget.player.api.userScopeId}|${extractTrackId(widget.player.current) ?? ''}';
  @override
  void initState() {
    super.initState();
    widget.player.addListener(_changed);
    unawaited(_load());
  }

  void _changed() {
    if (currentKey != selected) {
      unawaited(_load());
      return;
    }
    final position = widget.player.position * 1000;
    var index = -1;
    final currentLines = lines;
    if (synced) {
      for (var i = 0; i < currentLines.length; i++) {
        final start = currentLines[i]['start_time_ms'];
        final end = currentLines[i]['end_time_ms'];
        if (start is num &&
            start >= 0 &&
            position >= start &&
            (end is! num || position < end)) index = i;
      }
    }
    if (index != active && mounted) {
      setState(() => active = index);
      _scroll();
    }
  }

  void _scroll() {
    if (!follow || active < 0 || active >= lineKeys.length) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !follow || active < 0 || active >= lineKeys.length)
        return;
      final context = lineKeys[active].currentContext;
      if (context != null)
        Scrollable.ensureVisible(context,
            alignment: .4,
            duration: MediaQuery.disableAnimationsOf(context)
                ? Duration.zero
                : const Duration(milliseconds: 400),
            curve: Curves.easeInOutCubic);
    });
  }

  Future<void> _load({bool refresh = false}) async {
    final track = widget.player.current;
    final key = currentKey;
    final version = ++generation;
    final ready =
        !refresh && track != null ? widget.player.cachedLyrics(track) : null;
    selected = key;
    setState(() {
      data = ready ?? {};
      active = -1;
      failed = false;
      loading = ready == null;
      follow = true;
      lineKeys = [];
    });
    if (track == null) {
      setState(() => loading = false);
      return;
    }
    try {
      final result =
          ready ?? await widget.player.lyricsFor(track, refresh: refresh);
      if (!mounted || version != generation || key != currentKey) return;
      setState(() {
        data = result;
        loading = false;
        failed = result['lyrics_status'] == 'provider_error';
        lineKeys = List.generate(lines.length, (_) => GlobalKey());
      });
      _changed();
    } catch (_) {
      if (mounted && version == generation && key == currentKey)
        setState(() {
          loading = false;
          failed = true;
        });
    }
  }

  @override
  void dispose() {
    generation++;
    widget.player.removeListener(_changed);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final currentLines = lines;
    final hasLyrics = data['has_lyrics'] == true && currentLines.isNotEmpty;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Padding(
          padding: const EdgeInsets.fromLTRB(24, 12, 16, 12),
          child: Row(children: [
            Expanded(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                  Text('Lyrics', style: NeatieDesign.display(28)),
                  Text('${widget.player.current?['title'] ?? ''}',
                      maxLines: 1, overflow: TextOverflow.ellipsis),
                  if (hasLyrics)
                    Text(
                        '${synced ? 'Synced lyrics' : 'Lyrics'}${data['source'] == null ? '' : ' · ${data['source']}'}',
                        style: const TextStyle(
                            color: NeatieDesign.muted, fontSize: 12)),
                ])),
            IconButton(
                tooltip: 'Retry lyrics',
                onPressed: loading ? null : () => _load(refresh: true),
                icon: const Icon(Icons.refresh)),
            IconButton(
                tooltip: 'Close lyrics',
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close)),
          ])),
      if (synced && !follow)
        Align(
            alignment: Alignment.centerRight,
            child: TextButton(
                onPressed: () {
                  setState(() => follow = true);
                  _scroll();
                },
                child: const Text('Follow lyrics'))),
      Expanded(
          child: loading
              ? const Center(
                  child:
                      NeatieLoading(label: 'Finding lyrics', showLabel: true))
              : !hasLyrics
                  ? Center(
                      child: Padding(
                          padding: const EdgeInsets.all(24),
                          child: Text(
                              failed
                                  ? 'Lyrics are unavailable right now. Tap Retry to try again.'
                                  : data['lyrics_status'] == 'instrumental'
                                      ? 'This recording is instrumental.'
                                      : 'No lyrics found for this recording.',
                              textAlign: TextAlign.center)))
                  : NotificationListener<UserScrollNotification>(
                      onNotification: (_) {
                        if (follow) setState(() => follow = false);
                        return false;
                      },
                      child: SingleChildScrollView(
                          padding: const EdgeInsets.fromLTRB(24, 16, 24, 40),
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                for (var i = 0; i < currentLines.length; i++)
                                  Padding(
                                      key: lineKeys[i],
                                      padding: const EdgeInsets.symmetric(
                                          vertical: 7),
                                      child: TextButton(
                                          style: TextButton.styleFrom(
                                              alignment: Alignment.centerLeft,
                                              padding: const EdgeInsets.symmetric(
                                                  horizontal: 12, vertical: 8)),
                                          onPressed:
                                              synced && currentLines[i]['start_time_ms'] is num && (currentLines[i]['start_time_ms'] as num) >= 0
                                                  ? () => widget.player.seek(
                                                      (currentLines[i]['start_time_ms'] as num).toDouble() /
                                                          1000)
                                                  : null,
                                          child: AnimatedDefaultTextStyle(
                                              duration: MediaQuery.disableAnimationsOf(context)
                                                  ? Duration.zero
                                                  : const Duration(milliseconds: 250),
                                              style: TextStyle(fontSize: 24, height: 1.45, fontWeight: active == i ? FontWeight.w700 : FontWeight.w500, color: !synced || active == i ? NeatieDesign.ink : NeatieDesign.muted),
                                              child: Text('${currentLines[i]['text']}')))),
                              ])))),
    ]);
  }
}
