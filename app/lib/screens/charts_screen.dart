import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider_queue.dart';
import '../logic/charts_provider.dart';
import '../navigation/player_navigation.dart';
import '../ui/app_theme_tokens.dart';
import '../widgets/app_artwork.dart';
import '../widgets/chart_rank_history.dart';

const _chartCountries = chartCountries;

const Map<String, String> _chartRanges = <String, String>{
  'week': 'Week',
  'month': 'Month',
  'quarter': '3 months',
  'year': 'Year',
  'all_time': 'All time',
};

const Map<String, String> _chartSources = <String, String>{
  'all': 'All sources',
  'listenbrainz': 'ListenBrainz',
  'lastfm': 'Last.fm',
  'youtube': 'YouTube',
};

class ChartsScreen extends ConsumerStatefulWidget {
  const ChartsScreen({super.key});

  @override
  ConsumerState<ChartsScreen> createState() => _ChartsScreenState();
}

class _ChartsScreenState extends ConsumerState<ChartsScreen> {
  String _country = 'GLOBAL';
  String _range = 'week';
  String _source = 'all';
  Timer? _refreshPoll;

  ChartQuery get _query => ChartQuery(
        country: _country,
        range: _range,
        source: _source,
      );

  @override
  void dispose() {
    _refreshPoll?.cancel();
    super.dispose();
  }

  void _scheduleRefreshPoll() {
    if (_refreshPoll?.isActive ?? false) return;
    _refreshPoll = Timer(const Duration(seconds: 3), () {
      if (!mounted) return;
      ref.invalidate(chartsProvider(_query));
    });
  }

  Future<void> _play(ChartsSnapshot snapshot, ChartEntry selected) async {
    final playable = snapshot.entries
        .where((entry) => entry.isPlayable)
        .map((entry) => entry.track)
        .toList(growable: false);
    if (playable.isEmpty) return;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId:
              'charts:${snapshot.country}:${snapshot.range}:${snapshot.source}',
          playlistName:
              '${_chartCountries[snapshot.country] ?? snapshot.country} charts',
          tracks: playable,
          currentTrack: selected.track,
        );
    if (mounted) {
      unawaited(openFullPlayer(context));
    }
  }

  String _updatedLabel(DateTime? generatedAt) {
    if (generatedAt == null) return '';
    final difference = DateTime.now().difference(generatedAt);
    if (difference.inMinutes < 1) return 'Updated just now';
    if (difference.inHours < 1) {
      return 'Updated ${difference.inMinutes}m ago';
    }
    return 'Updated ${difference.inHours}h ago';
  }

  @override
  Widget build(BuildContext context) {
    final asyncSnapshot = ref.watch(chartsProvider(_query));
    return Scaffold(
      backgroundColor: neatieInk,
      appBar: AppBar(
        backgroundColor: neatieInk,
        foregroundColor: neatieActive,
        title: const Text('Charts & trending'),
      ),
      body: RefreshIndicator(
        onRefresh: () => ref.refresh(chartsProvider(_query).future),
        child: CustomScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          slivers: <Widget>[
            SliverToBoxAdapter(child: _buildFilters()),
            SliverToBoxAdapter(
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 320),
                switchInCurve: Curves.easeOutCubic,
                switchOutCurve: Curves.easeInCubic,
                transitionBuilder: (child, animation) => FadeTransition(
                  opacity: animation,
                  child: SlideTransition(
                    position: animation.drive(
                      Tween<Offset>(
                        begin: const Offset(0, 0.025),
                        end: Offset.zero,
                      ),
                    ),
                    child: child,
                  ),
                ),
                child: asyncSnapshot.when(
                  loading: () => const _ChartsPreparing(
                    key: ValueKey('charts-loading'),
                    title: 'Loading charts',
                  ),
                  error: (error, _) => _ChartsMessage(
                    key: const ValueKey('charts-error'),
                    icon: Icons.cloud_off_rounded,
                    title: 'Charts are unavailable',
                    message:
                        'Pull down or tap retry once the service is online.',
                    onRetry: () => ref.invalidate(chartsProvider(_query)),
                  ),
                  data: (snapshot) {
                    if (snapshot.isWarming) {
                      _scheduleRefreshPoll();
                      return const _ChartsPreparing(
                        key: ValueKey('charts-preparing'),
                        title: 'Preparing live charts',
                      );
                    }
                    if (snapshot.isRefreshing) {
                      _scheduleRefreshPoll();
                    }
                    if (snapshot.isUnavailable) {
                      return _ChartsMessage(
                        key: const ValueKey('charts-unavailable'),
                        icon: Icons.cloud_off_rounded,
                        title: 'Live charts could not be prepared',
                        message:
                            'The chart providers returned too little verified data. Pull down to retry.',
                        onRetry: () => ref.invalidate(chartsProvider(_query)),
                      );
                    }
                    if (snapshot.entries.isEmpty) {
                      return const _ChartsMessage(
                        key: ValueKey('charts-empty'),
                        icon: Icons.bar_chart_rounded,
                        title: 'No verified tracks yet',
                        message: 'Try another country, range, or source.',
                      );
                    }
                    return KeyedSubtree(
                      key: ValueKey(
                        'charts:${snapshot.country}:${snapshot.range}:${snapshot.source}:${snapshot.generatedAt}',
                      ),
                      child: _buildChart(snapshot),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildFilters() {
    final countries = _range == 'week'
        ? _chartCountries
        : const <String, String>{'GLOBAL': 'Global'};
    final sources = _chartSources.entries.where((entry) {
      if (_range != 'week') {
        return entry.key == 'all' || entry.key == 'listenbrainz';
      }
      if (_country != 'GLOBAL') return entry.key != 'listenbrainz';
      return true;
    }).toList(growable: false);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              Expanded(
                child: _ChartDropdown(
                  value: _country,
                  values: countries,
                  onChanged: (value) => setState(() {
                    _country = value;
                    if (value != 'GLOBAL' && _source == 'listenbrainz') {
                      _source = 'all';
                    }
                  }),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _ChartDropdown(
                  value: _range,
                  values: _chartRanges,
                  onChanged: (value) => setState(() {
                    _range = value;
                    if (value != 'week') {
                      _country = 'GLOBAL';
                      if (_source != 'all' && _source != 'listenbrainz') {
                        _source = 'all';
                      }
                    }
                  }),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          SizedBox(
            height: 38,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              itemCount: sources.length,
              separatorBuilder: (_, __) => const SizedBox(width: 8),
              itemBuilder: (context, index) {
                final entry = sources[index];
                final selected = _source == entry.key;
                return ChoiceChip(
                  selected: selected,
                  label: Text(entry.value),
                  onSelected: (_) => setState(() => _source = entry.key),
                  selectedColor: neatieActive,
                  backgroundColor: neatieRaised,
                  side: const BorderSide(color: neatieStroke),
                  labelStyle: TextStyle(
                    color: selected ? neatieInk : neatieMutedText,
                    fontWeight: FontWeight.w600,
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildChart(ChartsSnapshot snapshot) {
    final topHistory = snapshot.entries
        .where((entry) => entry.history.length > 1)
        .take(5)
        .toList(growable: false);
    return Column(
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 2, 16, 8),
          child: Row(
            children: <Widget>[
              const Expanded(
                child: Text(
                  'Top tracks',
                  style: TextStyle(
                    color: neatieActive,
                    fontSize: 24,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ),
              Text(
                _updatedLabel(snapshot.generatedAt),
                style: const TextStyle(color: neatieDimText, fontSize: 12),
              ),
            ],
          ),
        ),
        if (snapshot.isRefreshing) const _RefreshingChartBanner(),
        _ProviderCoverage(status: snapshot.providerStatus),
        _LiveChartCard(entries: snapshot.entries.take(8).toList()),
        if (topHistory.isNotEmpty) _ChartHistoryCard(entries: topHistory),
        ...snapshot.entries.indexed.map(
          (indexed) => _AnimatedChartEntryTile(
            index: indexed.$1,
            entry: indexed.$2,
            onTap: () => _play(snapshot, indexed.$2),
          ),
        ),
        const SizedBox(height: 110),
      ],
    );
  }
}

class _ChartDropdown extends StatelessWidget {
  const _ChartDropdown({
    required this.value,
    required this.values,
    required this.onChanged,
  });

  final String value;
  final Map<String, String> values;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    return DropdownButtonFormField<String>(
      initialValue: value,
      dropdownColor: neatieRaised,
      decoration: const InputDecoration(
        filled: true,
        fillColor: neatieRaised,
        isDense: true,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.all(Radius.circular(neatieRadiusMedium)),
          borderSide: BorderSide(color: neatieStroke),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.all(Radius.circular(neatieRadiusMedium)),
          borderSide: BorderSide(color: neatieStroke),
        ),
      ),
      style: const TextStyle(color: neatieActive, fontWeight: FontWeight.w600),
      items: values.entries
          .map((entry) => DropdownMenuItem<String>(
                value: entry.key,
                child: Text(entry.value, overflow: TextOverflow.ellipsis),
              ))
          .toList(growable: false),
      onChanged: (next) {
        if (next != null) onChanged(next);
      },
    );
  }
}

class _ProviderCoverage extends StatelessWidget {
  const _ProviderCoverage({required this.status});

  final Map<String, dynamic> status;

  @override
  Widget build(BuildContext context) {
    const labels = <String, String>{
      'listenbrainz': 'ListenBrainz',
      'lastfm': 'Last.fm',
      'youtube': 'YouTube Music',
    };
    final providers = labels.entries.map((entry) {
      final raw = status[entry.key];
      final value = raw is Map ? Map<String, dynamic>.from(raw) : const {};
      final available = value['status'] == 'ok' &&
          ((value['count'] as num?)?.toInt() ?? 0) > 0;
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
        decoration: BoxDecoration(
          color: available
              ? const Color(0xFF65D6A6).withValues(alpha: 0.12)
              : neatieRaised,
          borderRadius: BorderRadius.circular(99),
          border: Border.all(
            color: available
                ? const Color(0xFF65D6A6).withValues(alpha: 0.45)
                : neatieStroke,
          ),
        ),
        child: Text(
          entry.value,
          style: TextStyle(
            color: available ? const Color(0xFF65D6A6) : neatieDimText,
            fontSize: 10,
            fontWeight: FontWeight.w600,
          ),
        ),
      );
    }).toList(growable: false);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      child: Wrap(spacing: 7, runSpacing: 7, children: providers),
    );
  }
}

class _LiveChartCard extends StatelessWidget {
  const _LiveChartCard({required this.entries});

  final List<ChartEntry> entries;

  static String _compact(int value) {
    if (value >= 1000000000) {
      return '${(value / 1000000000).toStringAsFixed(1)}B';
    }
    if (value >= 1000000) return '${(value / 1000000).toStringAsFixed(1)}M';
    if (value >= 1000) return '${(value / 1000).toStringAsFixed(1)}K';
    return '$value';
  }

  static (int, String) _metric(ChartEntry entry) {
    if (entry.viewCount > 0) return (entry.viewCount, 'views');
    if (entry.listenCount > 0) return (entry.listenCount, 'plays');
    if (entry.listenerCount > 0) return (entry.listenerCount, 'listeners');
    return (math.max(101 - entry.rank, 1), 'rank score');
  }

  @override
  Widget build(BuildContext context) {
    if (entries.isEmpty) return const SizedBox.shrink();
    final maximum = entries
        .map((entry) => _metric(entry).$1)
        .fold<int>(1, (value, next) => math.max(value, next));
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 16),
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 12),
      decoration: BoxDecoration(
        color: neatieRaised,
        borderRadius: BorderRadius.circular(neatieRadiusLarge),
        border: Border.all(color: neatieStroke),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Text(
            'Current leaders',
            style: TextStyle(
              color: neatieActive,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 3),
          const Text(
            'Audience signal reported by the selected chart source',
            style: TextStyle(color: neatieDimText, fontSize: 11),
          ),
          const SizedBox(height: 12),
          ...entries.take(6).map((entry) {
            final metric = _metric(entry);
            final ratio = metric.$1 / maximum;
            return Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  Row(
                    children: <Widget>[
                      SizedBox(
                        width: 30,
                        child: Text(
                          '#${entry.rank}',
                          style: const TextStyle(
                            color: neatieDimText,
                            fontSize: 11,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      Expanded(
                        child: Text(
                          entry.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: neatieMutedText,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      Text(
                        '${_compact(metric.$1)} ${metric.$2}',
                        style: const TextStyle(
                          color: neatieDimText,
                          fontSize: 10,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(99),
                    child: ColoredBox(
                      color: neatieHairline,
                      child: TweenAnimationBuilder<double>(
                        tween: Tween<double>(
                          begin: 0,
                          end: ratio.clamp(0.06, 1).toDouble(),
                        ),
                        duration: const Duration(milliseconds: 700),
                        curve: Curves.easeOutCubic,
                        builder: (context, value, _) => FractionallySizedBox(
                          alignment: Alignment.centerLeft,
                          widthFactor: value,
                          child: Container(
                            height: 7,
                            decoration: BoxDecoration(
                              gradient: const LinearGradient(
                                colors: <Color>[
                                  Color(0xFF7BDFF2),
                                  Color(0xFF65D6A6),
                                ],
                              ),
                              borderRadius: BorderRadius.circular(99),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            );
          }),
        ],
      ),
    );
  }
}

class _ChartHistoryCard extends StatefulWidget {
  const _ChartHistoryCard({required this.entries});

  final List<ChartEntry> entries;

  @override
  State<_ChartHistoryCard> createState() => _ChartHistoryCardState();
}

class _ChartHistoryCardState extends State<_ChartHistoryCard> {
  int _selectedIndex = 0;

  String _dateLabel(DateTime value) => '${value.month}/${value.day}';

  @override
  Widget build(BuildContext context) {
    final selectedIndex = _selectedIndex.clamp(0, widget.entries.length - 1);
    final entry = widget.entries[selectedIndex];
    final history = entry.history;
    final maxRank = history.fold<int>(
      1,
      (value, point) => math.max(value, point.rank),
    );
    final unchanged = history.map((point) => point.rank).toSet().length == 1;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 4, 16, 16),
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 14),
      decoration: BoxDecoration(
        color: neatieRaised,
        borderRadius: BorderRadius.circular(neatieRadiusLarge),
        border: Border.all(color: neatieStroke),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Text(
            'Rank over time',
            style: TextStyle(
              color: neatieActive,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 3),
          const Text(
            'Higher on the graph means a stronger chart position',
            style: TextStyle(color: neatieDimText, fontSize: 11),
          ),
          const SizedBox(height: 10),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: widget.entries.indexed.map((indexed) {
                final selected = indexed.$1 == selectedIndex;
                return Padding(
                  padding: const EdgeInsets.only(right: 7),
                  child: ChoiceChip(
                    selected: selected,
                    showCheckmark: false,
                    label: Text(
                      indexed.$2.title,
                      overflow: TextOverflow.ellipsis,
                    ),
                    labelStyle: TextStyle(
                      color: selected ? neatieInk : neatieMutedText,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                    selectedColor: neatieActive,
                    backgroundColor: neatieInk,
                    side: const BorderSide(color: neatieStroke),
                    onSelected: (_) =>
                        setState(() => _selectedIndex = indexed.$1),
                  ),
                );
              }).toList(growable: false),
            ),
          ),
          const SizedBox(height: 8),
          SizedBox(
            height: 160,
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                SizedBox(
                  width: 34,
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      const Text('#1',
                          style: TextStyle(color: neatieDimText, fontSize: 10)),
                      Text(
                        '#${math.max(maxRank, 2)}',
                        style:
                            const TextStyle(color: neatieDimText, fontSize: 10),
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: AnimatedSwitcher(
                    duration: const Duration(milliseconds: 300),
                    child: TweenAnimationBuilder<double>(
                      key: ValueKey('${entry.title}:${entry.artist}'),
                      tween: Tween<double>(begin: 0, end: 1),
                      duration: const Duration(milliseconds: 750),
                      curve: Curves.easeOutCubic,
                      builder: (context, progress, _) => CustomPaint(
                        painter: RankHistoryPainter(history, progress),
                        size: Size.infinite,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 6),
          Row(
            children: <Widget>[
              Text(
                _dateLabel(history.first.at),
                style: const TextStyle(color: neatieDimText, fontSize: 10),
              ),
              const Spacer(),
              if (unchanged)
                Text(
                  'Unchanged at #${history.last.rank}',
                  style: const TextStyle(color: neatieMutedText, fontSize: 10),
                ),
              const Spacer(),
              Text(
                _dateLabel(history.last.at),
                style: const TextStyle(color: neatieDimText, fontSize: 10),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _AnimatedChartEntryTile extends StatelessWidget {
  const _AnimatedChartEntryTile({
    required this.index,
    required this.entry,
    required this.onTap,
  });

  final int index;
  final ChartEntry entry;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween<double>(begin: 0, end: 1),
      duration: Duration(milliseconds: 320 + math.min(index, 10) * 35),
      curve: Curves.easeOutCubic,
      builder: (context, value, child) => Opacity(
        opacity: value,
        child: Transform.translate(
          offset: Offset(0, 12 * (1 - value)),
          child: child,
        ),
      ),
      child: _ChartEntryTile(entry: entry, onTap: onTap),
    );
  }
}

class _ChartEntryTile extends StatelessWidget {
  const _ChartEntryTile({required this.entry, required this.onTap});

  final ChartEntry entry;
  final VoidCallback onTap;

  String _compactCount(int value) {
    if (value >= 1000000000) {
      return '${(value / 1000000000).toStringAsFixed(1)}B';
    }
    if (value >= 1000000) return '${(value / 1000000).toStringAsFixed(1)}M';
    if (value >= 1000) return '${(value / 1000).toStringAsFixed(1)}K';
    return '$value';
  }

  @override
  Widget build(BuildContext context) {
    final metric = entry.viewCount > 0
        ? '${_compactCount(entry.viewCount)} views'
        : entry.listenCount > 0
            ? '${_compactCount(entry.listenCount)} plays'
            : entry.listenerCount > 0
                ? '${_compactCount(entry.listenerCount)} listeners'
                : entry.providers.join(' + ');
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: Row(
          children: <Widget>[
            SizedBox(
              width: 36,
              child: Text(
                '${entry.rank}',
                style: const TextStyle(
                  color: neatieActive,
                  fontSize: 19,
                  fontWeight: FontWeight.w800,
                ),
              ),
            ),
            AppArtwork(
              thumbnail: entry.thumbnail,
              videoId: entry.playableSourceId,
              width: 58,
              height: 58,
              radius: 9,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(
                    entry.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: neatieActive,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    entry.artist,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: neatieMutedText),
                  ),
                  if (metric.isNotEmpty)
                    Text(
                      metric,
                      style:
                          const TextStyle(color: neatieDimText, fontSize: 11),
                    ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            _RankMovement(
              change: entry.rankChange,
              isNewEntry: entry.isNewEntry,
            ),
            const SizedBox(width: 4),
            const Icon(Icons.play_arrow_rounded, color: neatieMutedText),
          ],
        ),
      ),
    );
  }
}

class _RankMovement extends StatelessWidget {
  const _RankMovement({required this.change, required this.isNewEntry});

  final int? change;
  final bool isNewEntry;

  @override
  Widget build(BuildContext context) {
    if (isNewEntry) {
      return const Text('NEW',
          style: TextStyle(color: Color(0xFF7BDFF2), fontSize: 10));
    }
    if (change == null) {
      return const Icon(Icons.remove_rounded, color: neatieDimText, size: 18);
    }
    if (change == 0) {
      return const Icon(Icons.remove_rounded, color: neatieDimText, size: 18);
    }
    final rising = change! > 0;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(
          rising ? Icons.arrow_drop_up_rounded : Icons.arrow_drop_down_rounded,
          color: rising ? const Color(0xFF65D6A6) : const Color(0xFFF07B7B),
          size: 20,
        ),
        Text(
          '${change!.abs()}',
          style: TextStyle(
            color: rising ? const Color(0xFF65D6A6) : const Color(0xFFF07B7B),
            fontSize: 11,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    );
  }
}

class _RefreshingChartBanner extends StatelessWidget {
  const _RefreshingChartBanner();

  @override
  Widget build(BuildContext context) {
    return const Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            'Refreshing live provider data',
            style: TextStyle(color: neatieMutedText, fontSize: 11),
          ),
          SizedBox(height: 6),
          ClipRRect(
            borderRadius: BorderRadius.all(Radius.circular(99)),
            child: LinearProgressIndicator(
              minHeight: 3,
              color: neatieActive,
              backgroundColor: neatieHairline,
            ),
          ),
        ],
      ),
    );
  }
}

class _ChartsPreparing extends StatelessWidget {
  const _ChartsPreparing({super.key, required this.title});

  final String title;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 430),
      child: Center(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 34),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              const Icon(
                Icons.query_stats_rounded,
                color: neatieMutedText,
                size: 42,
              ),
              const SizedBox(height: 14),
              Text(
                title,
                style: const TextStyle(
                  color: neatieActive,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 12),
              const ClipRRect(
                borderRadius: BorderRadius.all(Radius.circular(99)),
                child: LinearProgressIndicator(
                  minHeight: 4,
                  color: neatieActive,
                  backgroundColor: neatieHairline,
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                'Collecting and verifying regional chart tracks',
                textAlign: TextAlign.center,
                style: TextStyle(color: neatieDimText, fontSize: 12),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ChartsMessage extends StatelessWidget {
  const _ChartsMessage({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    this.onRetry,
  });

  final IconData icon;
  final String title;
  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 430),
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Icon(icon, color: neatieMutedText, size: 44),
              const SizedBox(height: 14),
              Text(
                title,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: neatieActive,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                message,
                textAlign: TextAlign.center,
                style: const TextStyle(color: neatieMutedText),
              ),
              if (onRetry != null) ...<Widget>[
                const SizedBox(height: 16),
                OutlinedButton(onPressed: onRetry, child: const Text('Retry')),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
