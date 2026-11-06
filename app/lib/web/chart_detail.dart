import 'dart:async';
import 'package:flutter/material.dart';
import '../logic/charts_provider.dart';
import 'catalog_api.dart';
import 'design_system.dart';
import 'home_widgets.dart';
import 'web_player.dart';
import '../widgets/chart_rank_history.dart';

/// Ranks and movement always come from the selected server-owned chart.
class ChartDetail extends StatefulWidget {
  const ChartDetail(
      {super.key,
      required this.api,
      required this.player,
      required this.query,
      required this.title,
      this.initial,
      required this.onLike,
      this.onAddToPlaylist,
      required this.isLiked,
      required this.isActive});
  final CatalogApi api;
  final WebPlayer player;
  final ChartQuery query;
  final String title;
  final ChartsSnapshot? initial;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final bool Function(Map<String, dynamic>) isLiked;
  final bool Function() isActive;
  @override
  State<ChartDetail> createState() => _ChartDetailState();
}

class _ChartDetailState extends State<ChartDetail> {
  late ChartQuery query = widget.query;
  ChartsSnapshot? data;
  bool busy = false;
  String? error;
  int version = 0, polls = 0;
  Timer? poll;
  late final scope = widget.api.api.userScopeId;
  bool get canAct => widget.isActive() && scope == widget.api.api.userScopeId;
  @override
  void initState() {
    super.initState();
    data = widget.initial;
    unawaited(load());
  }

  Future<void> load() async {
    if (!canAct) return;
    poll?.cancel();
    final request = ++version;
    final scope = widget.api.api.userScopeId;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      final value = await widget.api.charts(query);
      if (!mounted || request != version || scope != widget.api.api.userScopeId)
        return;
      setState(() {
        data = value;
        busy = false;
      });
      if ((value.refreshing || value.isWarming) && polls++ < 12) {
        poll = Timer(const Duration(seconds: 5), () => unawaited(load()));
      }
    } catch (_) {
      if (mounted && request == version) {
        setState(() {
          busy = false;
          error = 'This chart could not update.';
        });
      }
    }
  }

  void change(String range) {
    polls = 0;
    setState(() {
      query = ChartQuery(
          country: query.country, source: query.source, range: range);
      data = null;
    });
    unawaited(load());
  }

  @override
  Widget build(BuildContext context) {
    final entries = data?.entries ?? const <ChartEntry>[];
    final tracks = entries.map((entry) => entry.track).toList();
    final longRanges =
        query.country == 'GLOBAL' && query.source == 'listenbrainz';
    return SafeArea(
        child: Column(children: [
      Padding(
          padding: const EdgeInsets.fromLTRB(24, 18, 12, 8),
          child: Row(children: [
            Expanded(
                child: Text(widget.title, style: NeatieDesign.display(30))),
            IconButton(
                tooltip: 'Close chart',
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close_rounded)),
          ])),
      Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Row(children: [
            Expanded(
                child: Text(
                    error ??
                        '${entries.length} songs · ${query.range == 'week' ? 'Weekly ranking' : query.range}',
                    style: const TextStyle(
                        color: NeatieDesign.muted, fontSize: 12))),
            IconButton(
                tooltip: 'Check for chart updates',
                onPressed: busy
                    ? null
                    : () {
                        polls = 0;
                        unawaited(load());
                      },
                icon: const Icon(Icons.refresh_rounded)),
            IconButton.filled(
                tooltip: 'Play chart',
                onPressed: tracks.isEmpty
                    ? null
                    : () {
                        if (canAct)
                          widget.player.startQueue(tracks, 0, widget.title);
                      },
                icon: const Icon(Icons.play_arrow_rounded)),
          ])),
      if (longRanges)
        Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Wrap(spacing: 6, children: [
              for (final range in const {
                'week': 'Week',
                'month': 'Month',
                'quarter': 'Quarter',
                'year': 'Year',
                'all_time': 'All time'
              }.entries)
                ChoiceChip(
                    label: Text(range.value),
                    selected: query.range == range.key,
                    onSelected: (_) => change(range.key)),
            ])),
      if (busy && entries.isEmpty)
        const Padding(
            padding: EdgeInsets.all(24),
            child: NeatieLoading(label: 'Loading chart', showLabel: true)),
      if (!busy && entries.isEmpty)
        const Padding(
            padding: EdgeInsets.all(24),
            child: Text(
                'This edition is not available yet. Check again shortly.')),
      if (entries.any((entry) => entry.history.length > 1))
        _ChartHistory(
            entries: entries
                .where((entry) => entry.history.length > 1)
                .take(8)
                .toList()),
      Expanded(
          child: ListView.builder(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
              itemCount: entries.length,
              itemBuilder: (context, index) {
                final entry = entries[index];
                final delta = entry.rankChange;
                return ListTile(
                    leading: Row(mainAxisSize: MainAxisSize.min, children: [
                      SizedBox(
                          width: 30,
                          child: Text('${entry.rank}',
                              style:
                                  const TextStyle(color: NeatieDesign.accent))),
                      HomeArtwork(
                          api: widget.api.api,
                          item: entry.track,
                          kind: 'track',
                          size: 44),
                    ]),
                    title: Text(entry.title,
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                    subtitle: Text(entry.artist,
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                    onTap: () {
                      if (canAct)
                        widget.player.startQueue(tracks, index, widget.title);
                    },
                    trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                      if (delta != null)
                        Tooltip(
                            message: 'Change since the previous edition',
                            child: Text(
                                delta == 0
                                    ? '—'
                                    : '${delta > 0 ? '↑' : '↓'}${delta.abs()}',
                                style: const TextStyle(
                                    fontSize: 11, color: NeatieDesign.muted)))
                      else if (entry.isNewEntry)
                        const Text('NEW', style: NeatieDesign.eyebrow),
                      PopupMenuButton<String>(
                          tooltip: 'Actions for ${entry.title}',
                          onSelected: (action) {
                            if (!canAct) return;
                            if (action == 'next')
                              widget.player.playNext(entry.track);
                            if (action == 'like')
                              unawaited(widget.onLike(entry.track));
                            if (action == 'playlist')
                              unawaited(widget.onAddToPlaylist!(entry.track));
                          },
                          itemBuilder: (_) => [
                                const PopupMenuItem(
                                    value: 'next', child: Text('Play next')),
                                if (widget.onAddToPlaylist != null)
                                  const PopupMenuItem(
                                      value: 'playlist',
                                      child: Text('Add to playlist')),
                                PopupMenuItem(
                                    value: 'like',
                                    child: Text(widget.isLiked(entry.track)
                                        ? 'Remove from likes'
                                        : 'Add to likes')),
                              ]),
                    ]));
              })),
    ]));
  }

  @override
  void dispose() {
    version++;
    poll?.cancel();
    super.dispose();
  }
}

class _ChartHistory extends StatefulWidget {
  const _ChartHistory({required this.entries});
  final List<ChartEntry> entries;
  @override
  State<_ChartHistory> createState() => _ChartHistoryState();
}

class _ChartHistoryState extends State<_ChartHistory> {
  String? selected;
  @override
  Widget build(BuildContext context) {
    final entry = widget.entries.firstWhere(
        (e) => e.playableSourceId == selected,
        orElse: () => widget.entries.first);
    final history = entry.history;
    String date(DateTime at) => '${at.day}/${at.month}';
    return Padding(
        padding: const EdgeInsets.fromLTRB(24, 12, 24, 16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Ranking history', style: NeatieDesign.eyebrow),
          DropdownButton<String>(
              isExpanded: true,
              value: entry.playableSourceId,
              items: [
                for (final track in widget.entries)
                  DropdownMenuItem(
                      value: track.playableSourceId,
                      child: Text(track.title,
                          maxLines: 1, overflow: TextOverflow.ellipsis))
              ],
              onChanged: (value) => setState(() => selected = value)),
          Semantics(
              label:
                  'Rank history for ${entry.title}: ${history.map((p) => '#${p.rank} on ${date(p.at)}').join(', ')}',
              child: RepaintBoundary(
                  child: SizedBox(
                      height: 90,
                      width: double.infinity,
                      child: CustomPaint(
                          painter: RankHistoryPainter(history, 1))))),
          const SizedBox(height: 8),
          Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
            Text(date(history.first.at)),
            Text('#${history.last.rank}'),
            Text(date(history.last.at)),
          ]),
        ]));
  }
}
