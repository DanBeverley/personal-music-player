import 'dart:async';
import 'package:flutter/material.dart';
import '../logic/charts_provider.dart';
import '../logic/new_releases_provider.dart';
import '../logic/track_metadata.dart';
import 'catalog_api.dart';
import 'chart_detail.dart';
import 'chart_region.dart';
import 'design_system.dart';
import 'home_widgets.dart';
import 'web_player.dart';

const _globalEditions = [
  (
    title: 'Global pulse',
    subtitle: 'A blend of music & listening charts',
    country: 'GLOBAL',
    source: 'all',
    color: Color(0xff53644a)
  ),
  (
    title: 'YouTube Music',
    subtitle: 'Global music-video chart',
    country: 'GLOBAL',
    source: 'youtube',
    color: Color(0xff8a5b50)
  ),
  (
    title: 'Community favourites',
    subtitle: 'ListenBrainz · weekly listening',
    country: 'GLOBAL',
    source: 'listenbrainz',
    color: Color(0xff6a587d)
  ),
  (
    title: 'Last.fm favourites',
    subtitle: 'Global community chart',
    country: 'GLOBAL',
    source: 'lastfm',
    color: Color(0xff7a684b)
  ),
];

int _previewRank(ChartEntry entry, String source) => source == 'all'
    ? entry.rank
    : ((entry.raw['provider_ranks'] as Map?)?[source] as num?)?.toInt() ??
        entry.rank;

/// Independent live shelves: never participate in Home's promotion/readiness.
class DiscoveryShelves extends StatefulWidget {
  const DiscoveryShelves(
      {super.key,
      required this.api,
      required this.player,
      required this.active,
      required this.onOpenAlbum,
      required this.onLike,
      this.onAddToPlaylist,
      required this.isLiked,
      this.region});
  final CatalogApi api;
  final WebPlayer player;
  final bool active;
  final ValueChanged<NewReleaseEntry> onOpenAlbum;
  final Future<void> Function(Map<String, dynamic>) onLike;
  final Future<void> Function(Map<String, dynamic>)? onAddToPlaylist;
  final bool Function(Map<String, dynamic>) isLiked;
  final ChartRegion? region;
  @override
  State<DiscoveryShelves> createState() => _DiscoveryShelvesState();
}

class _DiscoveryShelvesState extends State<DiscoveryShelves>
    with AutomaticKeepAliveClientMixin {
  bool panelOpen = false;
  @override
  bool get wantKeepAlive => panelOpen;
  void _panel(bool value) {
    panelOpen = value;
    updateKeepAlive();
  }

  late final region = widget.region ?? ChartRegion();
  late String selectedCountry;
  List<({String title, String subtitle, String country, String source, Color color})>
      get editions => [
            _globalEditions.first,
            if (region.country != 'GLOBAL')
              (
                title: '${chartCountries[region.country]} pulse',
                subtitle: 'Local favourites, current charts',
                country: region.country,
                source: 'all',
                color: const Color(0xff4f6574)
              ),
            ..._globalEditions.skip(1),
          ];
  final charts = <String, ChartsSnapshot>{};
  final errors = <String>{}, pending = <String>{};
  final polls = <String, int>{};
  final timers = <String, Timer>{};
  final chartScroll = ScrollController(), releaseScroll = ScrollController();
  NewReleasesSnapshot? releases;
  String releaseKind = 'album';
  int requestVersion = 0;
  @override
  void initState() {
    super.initState();
    selectedCountry = region.country;
    region.addListener(_regionChanged);
    if (widget.active) {
      _loadAll();
      unawaited(region.initialize());
    }
  }

  void _regionChanged() {
    if (!mounted) return;
    if (selectedCountry != region.country) {
      final oldCountry = selectedCountry;
      selectedCountry = region.country;
      if (oldCountry != 'GLOBAL') timers.remove(oldCountry)?.cancel();
      charts.removeWhere((key, _) => key != 'GLOBAL' && key != selectedCountry);
      if (selectedCountry != 'GLOBAL') {
        polls[selectedCountry] = 0;
        unawaited(_load(selectedCountry));
      }
    }
    setState(() {});
  }

  bool _wanted(String key) =>
      key == 'GLOBAL' || key == 'releases' || key == region.country;

  @override
  void didUpdateWidget(covariant DiscoveryShelves old) {
    super.didUpdateWidget(old);
    if (!widget.active) {
      for (final timer in timers.values) {
        timer.cancel();
      }
      timers.clear();
    } else if (!old.active) {
      _loadAll();
      unawaited(region.initialize());
    }
  }

  void _loadAll() {
    for (final key in {'GLOBAL', region.country, 'releases'}) {
      polls[key] = 0;
      unawaited(_load(key));
    }
  }

  Future<void> _load(String key, {bool refresh = false}) async {
    if (!widget.active || !_wanted(key) || pending.contains(key)) return;
    timers.remove(key)?.cancel();
    final version = requestVersion;
    final scope = widget.api.api.userScopeId;
    setState(() {
      pending.add(key);
      errors.remove(key);
    });
    bool warming = false;
    try {
      if (key == 'releases') {
        final value = await widget.api.cachedNewReleases(refresh: refresh);
        if (!mounted ||
            version != requestVersion ||
            scope != widget.api.api.userScopeId ||
            !_wanted(key)) return;
        setState(() => releases = value);
        warming = value.status == 'preparing' || value.status == 'stale';
      } else {
        final value = await widget.api.cachedCharts(ChartQuery(country: key), refresh: refresh);
        if (!mounted ||
            version != requestVersion ||
            scope != widget.api.api.userScopeId ||
            !_wanted(key)) return;
        setState(() => charts[key] = value);
        warming = value.isWarming || value.refreshing;
      }
    } catch (_) {
      if (mounted && version == requestVersion && _wanted(key))
        setState(() => errors.add(key));
    } finally {
      if (mounted && version == requestVersion)
        setState(() => pending.remove(key));
    }
    final attempts = polls.update(key, (value) => value + 1, ifAbsent: () => 1);
    if (mounted && widget.active && _wanted(key) && warming && attempts < 12) {
      timers[key] =
          Timer(const Duration(seconds: 5), () => unawaited(_load(key)));
    }
  }

  void _retry(String key) {
    polls[key] = 0;
    unawaited(_load(key, refresh: true));
  }

  List<NewReleaseEntry> get releaseEntries =>
      ((releases?.entries ?? const <NewReleaseEntry>[])
          .where((entry) =>
              entry.raw['entry_type'] == releaseKind &&
              (releaseKind != 'album' || entry.raw['detail_ready'] == true))
          .toList()
        ..sort((a, b) => b.releaseDate.compareTo(a.releaseDate)));
  Future<void> _openChart(String country, String source, String title) async {
    _panel(true);
    try {
      await showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          constraints: const BoxConstraints(maxWidth: 940),
          backgroundColor: NeatieDesign.background,
          builder: (context) => SizedBox(
              height: MediaQuery.sizeOf(context).height * .9,
              child: ChartDetail(
                  api: widget.api,
                  player: widget.player,
                  query: ChartQuery(country: country, source: source),
                  title: title,
                  initial: source == 'all' ? charts[country] : null,
                  onLike: widget.onLike,
                  onAddToPlaylist: widget.onAddToPlaylist,
                  isLiked: widget.isLiked,
                  isActive: () => mounted && widget.active)));
    } finally {
      if (mounted) _panel(false);
    }
  }

  void _playReleases(List<NewReleaseEntry> entries, int index) {
    widget.player.startQueue(
        entries
            .map((entry) => normalizeTrack({
                  ...entry.raw,
                  'channel': entry.artist,
                }))
            .toList(),
        index,
        'New releases');
  }

  Future<void> _browseReleases() async {
    final entries = releaseEntries;
    final kind = releaseKind;
    final scope = widget.api.api.userScopeId;
    _panel(true);
    try {
      await showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          constraints: const BoxConstraints(maxWidth: 940),
          backgroundColor: NeatieDesign.background,
          builder: (sheetContext) => SafeArea(
              child: SizedBox(
                  height: MediaQuery.sizeOf(sheetContext).height * .85,
                  child: Column(children: [
                    Padding(
                        padding: const EdgeInsets.fromLTRB(24, 18, 12, 8),
                        child: Row(children: [
                          Expanded(
                              child: Text(
                                  'New ${releaseKind == 'album' ? 'albums' : 'songs'}',
                                  style: NeatieDesign.display(30))),
                          IconButton(
                              tooltip: 'Close new releases',
                              onPressed: () => Navigator.pop(sheetContext),
                              icon: const Icon(Icons.close_rounded)),
                        ])),
                    Expanded(
                        child: ListView.builder(
                            itemCount: entries.length,
                            padding: const EdgeInsets.all(16),
                            itemBuilder: (_, index) {
                              final entry = entries[index];
                              return ListTile(
                                  leading: HomeArtwork(
                                      api: widget.api.api,
                                      item: entry.raw,
                                      kind: releaseKind,
                                      size: 48),
                                  title: Text(entry.title,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis),
                                  subtitle: Text(
                                      '${entry.artist}${entry.releaseDate.isEmpty ? '' : ' · ${entry.releaseDate}'}',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis),
                                  trailing: Icon(releaseKind == 'album'
                                      ? Icons.arrow_forward_rounded
                                      : Icons.play_arrow_rounded),
                                  onTap: () {
                                    Navigator.pop(sheetContext);
                                    if (!mounted ||
                                        !widget.active ||
                                        scope != widget.api.api.userScopeId)
                                      return;
                                    if (kind == 'album')
                                      widget.onOpenAlbum(entry);
                                    else
                                      _playReleases(entries, index);
                                  });
                            })),
                  ]))));
    } finally {
      if (mounted) _panel(false);
    }
  }

  Widget _paging(ScrollController controller, String label) =>
      Row(mainAxisSize: MainAxisSize.min, children: [
        for (final direction in [-1, 1])
          IconButton.outlined(
              tooltip: '${direction < 0 ? 'Previous' : 'Next'} $label',
              onPressed: () {
                if (!controller.hasClients) return;
                final target = (controller.offset +
                        direction * controller.position.viewportDimension * .8)
                    .clamp(0.0, controller.position.maxScrollExtent)
                    .toDouble();
                if (MediaQuery.disableAnimationsOf(context))
                  controller.jumpTo(target);
                else
                  unawaited(controller.animateTo(target,
                      duration: const Duration(milliseconds: 520),
                      curve: Curves.easeInOutCubic));
              },
              icon: Icon(
                  direction < 0
                      ? Icons.chevron_left_rounded
                      : Icons.chevron_right_rounded,
                  size: 20)),
      ]);
  @override
  Widget build(BuildContext context) {
    super.build(context);
    final entries = releaseEntries;
    final textScale = MediaQuery.textScalerOf(context).scale(1).clamp(1.0, 2.0);
    return TickerMode(
        enabled: widget.active,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SectionHeading(
              title: 'Featured charts', action: _paging(chartScroll, 'charts')),
          const Text('What people are listening to, here and everywhere.',
              style: TextStyle(color: NeatieDesign.muted, fontSize: 12)),
          const SizedBox(height: 14),
          Semantics(
              container: true,
              child: SizedBox(
                  width: 320,
                  child: DropdownButtonFormField<String>(
                      initialValue: region.selection,
                      key: ValueKey(region.selection),
                      isExpanded: true,
                      decoration: InputDecoration(
                          labelText: 'Chart region',
                          helperText: region.description,
                          helperMaxLines: 2),
                      items: [
                        const DropdownMenuItem(
                            value: 'AUTO', child: Text('Automatic')),
                        for (final country in chartCountries.entries)
                          DropdownMenuItem(
                              value: country.key, child: Text(country.value))
                      ],
                      onChanged: (value) {
                        if (value != null) unawaited(region.choose(value));
                      }))),
          const SizedBox(height: 18),
          SizedBox(
              height: 336 * textScale,
              child: ListView.separated(
                  controller: chartScroll,
                  scrollDirection: Axis.horizontal,
                  itemCount: editions.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 16),
                  itemBuilder: (_, index) {
                    final edition = editions[index];
                    final snapshot = charts[edition.country];
                    final candidates =
                        (snapshot?.entries ?? const <ChartEntry>[])
                            .where((entry) =>
                                edition.source == 'all' ||
                                entry.providers.contains(edition.source))
                            .toList();
                    candidates.sort((a, b) => _previewRank(a, edition.source)
                        .compareTo(_previewRank(b, edition.source)));
                    final preview = candidates.take(3).toList();
                    final error = errors.contains(edition.country);
                    return SizedBox(
                        key: ValueKey('${edition.country}:${edition.source}'),
                        width: 248,
                        child: EditorialSurface(
                            color: edition.color.withValues(alpha: .15),
                            child: Material(
                                color: Colors.transparent,
                                child: InkWell(
                                    onTap: () => _openChart(edition.country,
                                        edition.source, edition.title),
                                    child: Padding(
                                        padding: const EdgeInsets.all(20),
                                        child: Column(
                                            crossAxisAlignment:
                                                CrossAxisAlignment.start,
                                            children: [
                                              Row(children: [
                                                const Icon(
                                                    Icons.equalizer_rounded,
                                                    size: 20),
                                                const Spacer(),
                                                Text(
                                                    edition.country == 'GLOBAL'
                                                        ? 'GLOBAL'
                                                        : edition.country,
                                                    style: NeatieDesign.eyebrow)
                                              ]),
                                              const SizedBox(height: 18),
                                              Text(edition.title,
                                                  maxLines: 2,
                                                  overflow:
                                                      TextOverflow.ellipsis,
                                                  style:
                                                      NeatieDesign.display(28)),
                                              const SizedBox(height: 6),
                                              Text(edition.subtitle,
                                                  maxLines: 2,
                                                  overflow:
                                                      TextOverflow.ellipsis,
                                                  style: const TextStyle(
                                                      color: NeatieDesign.muted,
                                                      fontSize: 11)),
                                              const Spacer(),
                                              for (final entry
                                                  in preview.take(2))
                                                Text(
                                                    '${_previewRank(entry, edition.source)}. ${entry.title}',
                                                    maxLines: 1,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: const TextStyle(
                                                        color:
                                                            NeatieDesign.muted,
                                                        fontSize: 11)),
                                              if (preview.isNotEmpty)
                                                const SizedBox(height: 10),
                                              if (preview.isNotEmpty)
                                                Row(children: [
                                                  for (final entry in preview)
                                                    Padding(
                                                        padding:
                                                            const EdgeInsets
                                                                .only(right: 8),
                                                        child: HomeArtwork(
                                                            api: widget.api.api,
                                                            item: entry.track,
                                                            kind: 'track',
                                                            size: 48)),
                                                ])
                                              else
                                                Text(
                                                    error
                                                        ? 'Could not check this edition'
                                                        : snapshot?.status ==
                                                                    'unavailable' ||
                                                                (polls[edition
                                                                            .country] ??
                                                                        0) >=
                                                                    12
                                                            ? 'Edition not available yet'
                                                            : 'Preparing this edition',
                                                    style: const TextStyle(
                                                        color:
                                                            NeatieDesign.muted,
                                                        fontSize: 12)),
                                              const SizedBox(height: 14),
                                              Row(children: [
                                                const Spacer(),
                                                const Icon(
                                                    Icons.arrow_forward_rounded,
                                                    size: 18)
                                              ]),
                                            ]))))));
                  })),
          SectionHeading(
              title: 'New releases',
              action: Row(mainAxisSize: MainAxisSize.min, children: [
                IconButton(
                    tooltip: 'Check for release updates',
                    onPressed: pending.contains('releases')
                        ? null
                        : () => _retry('releases'),
                    icon: const Icon(Icons.refresh_rounded, size: 20)),
                BrowseMoreButton(
                    label: 'Browse new releases',
                    onPressed: entries.isEmpty ? null : _browseReleases),
              ])),
          Wrap(
              spacing: 8,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                for (final kind in ['album', 'track'])
                  ChoiceChip(
                      label: Text(kind == 'album' ? 'Albums' : 'Songs'),
                      selected: releaseKind == kind,
                      onSelected: (_) => setState(() {
                            releaseKind = kind;
                            if (releaseScroll.hasClients)
                              releaseScroll.jumpTo(0);
                          })),
              ]),
          const SizedBox(height: 16),
          if (releases?.refreshStatus == 'partial')
            const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text(
                    'Showing the latest available releases. Some dates are unavailable.',
                    style: TextStyle(color: NeatieDesign.muted, fontSize: 11))),
          if (entries.isEmpty)
            EditorialSurface(
                child: Padding(
                    padding: const EdgeInsets.all(20),
                    child: Row(children: [
                      Expanded(
                          child: Text(
                              errors.contains('releases')
                                  ? 'New releases could not load.'
                                  : (polls['releases'] ?? 0) >= 12 ||
                                          releases?.status == 'unavailable'
                                      ? 'No recent releases are available yet.'
                                      : 'Preparing recent releases. Home is ready to explore.',
                              style:
                                  const TextStyle(color: NeatieDesign.muted))),
                      TextButton(
                          onPressed: pending.contains('releases')
                              ? null
                              : () => _retry('releases'),
                          child: const Text('Check again')),
                    ])))
          else
            PageEntrance(
                key: ValueKey(releaseKind),
                active: widget.active,
                duration: const Duration(milliseconds: 520),
                child: SizedBox(
                    height: 294 * textScale,
                    child: ListView.separated(
                        controller: releaseScroll,
                        scrollDirection: Axis.horizontal,
                        itemCount: entries.length,
                        separatorBuilder: (_, __) => const SizedBox(width: 16),
                        itemBuilder: (_, index) {
                          final entry = entries[index];
                          return SizedBox(
                              width: 192,
                              child: EditorialSurface(
                                  child: Material(
                                      color: Colors.transparent,
                                      child: InkWell(
                                          onTap: () {
                                            if (releaseKind == 'album')
                                              widget.onOpenAlbum(entry);
                                            else
                                              _playReleases(entries, index);
                                          },
                                          child: Padding(
                                              padding: const EdgeInsets.all(12),
                                              child: Column(
                                                  crossAxisAlignment:
                                                      CrossAxisAlignment.start,
                                                  children: [
                                                    Stack(children: [
                                                      HomeArtwork(
                                                          api: widget.api.api,
                                                          item: entry.raw,
                                                          kind: releaseKind,
                                                          size: 166),
                                                      Positioned(
                                                          right: 6,
                                                          bottom: 6,
                                                          child: Container(
                                                              padding:
                                                                  const EdgeInsets
                                                                      .all(8),
                                                              decoration: const BoxDecoration(
                                                                  color:
                                                                      NeatieDesign
                                                                          .accent,
                                                                  shape: BoxShape
                                                                      .circle),
                                                              child: Icon(
                                                                  releaseKind ==
                                                                          'album'
                                                                      ? Icons
                                                                          .arrow_forward_rounded
                                                                      : Icons
                                                                          .play_arrow_rounded,
                                                                  color: NeatieDesign
                                                                      .background,
                                                                  size: 20))),
                                                    ]),
                                                    const SizedBox(height: 12),
                                                    Text(entry.title,
                                                        maxLines: 1,
                                                        overflow: TextOverflow
                                                            .ellipsis,
                                                        style: const TextStyle(
                                                            fontWeight:
                                                                FontWeight
                                                                    .w600)),
                                                    Text(entry.artist,
                                                        maxLines: 1,
                                                        overflow: TextOverflow
                                                            .ellipsis,
                                                        style: const TextStyle(
                                                            color: NeatieDesign
                                                                .muted,
                                                            fontSize: 12)),
                                                    const Spacer(),
                                                    Text(
                                                        entry.releaseDate
                                                                .isNotEmpty
                                                            ? entry.releaseDate
                                                            : 'Date not supplied',
                                                        style: const TextStyle(
                                                            color: NeatieDesign
                                                                .muted,
                                                            fontSize: 10)),
                                                  ]))))));
                        }))),
          if (entries.isNotEmpty)
            Align(
                alignment: Alignment.centerRight,
                child: _paging(releaseScroll, 'releases')),
        ]));
  }

  @override
  void dispose() {
    region.removeListener(_regionChanged);
    if (widget.region == null) region.dispose();
    requestVersion++;
    for (final timer in timers.values) {
      timer.cancel();
    }
    chartScroll.dispose();
    releaseScroll.dispose();
    super.dispose();
  }
}
