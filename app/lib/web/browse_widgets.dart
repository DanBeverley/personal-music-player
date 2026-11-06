import 'package:flutter/material.dart';
import 'design_system.dart';

abstract final class BrowsePalette {
  static const paper = Color(0xff19221f);
  static const ink = Color(0xfff0eee5);
  static const muted = Color(0xffb5beb6);
  static const signal = Color(0xffd6ec9a);
}

/// Reuses the displayed cover, without palette decoding or a live blur layer.
class ArtworkSurface extends StatelessWidget {
  const ArtworkSurface({super.key, required this.child, this.artwork});
  final Widget child;
  final Widget Function(double)? artwork;
  @override
  Widget build(BuildContext context) => ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: ColoredBox(
          color: BrowsePalette.paper,
          child: Stack(children: [
            if (artwork != null)
              Positioned.fill(
                  child: ExcludeSemantics(
                      child: IgnorePointer(
                          child: RepaintBoundary(
                              child: Opacity(
                                  opacity: .6,
                                  child: FittedBox(
                                      fit: BoxFit.cover,
                                      clipBehavior: Clip.hardEdge,
                                      child: SizedBox(
                                          width: 240,
                                          height: 240,
                                          child: artwork!(240)))))))),
            const Positioned.fill(
                child: IgnorePointer(
                    child: DecoratedBox(
                        decoration: BoxDecoration(
                            gradient: LinearGradient(
                                colors: [Color(0xf20b1412), Color(0x9e0b1412)],
                                begin: Alignment.centerLeft,
                                end: Alignment.bottomRight))))),
            child,
          ])));
}

String catalogReleaseYear(Map<String, dynamic> item) {
  final date = catalogReleaseDate(item);
  return RegExp(r'\d{4}$').firstMatch(date)?.group(0) ?? '';
}

/// Keep the precision of the supplied release metadata; never invent a day.
String catalogReleaseDate(Map<String, dynamic> item) {
  const months = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec'
  ];
  for (final key in [
    'release_date',
    'releaseDate',
    'first_release_date',
    'release_year',
    'year'
  ]) {
    final value = '${item[key] ?? ''}'.trim();
    final match = RegExp(r'^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T.*)?)?)?$')
        .firstMatch(value);
    if (match == null) continue;
    final year = int.parse(match[1]!);
    if (year < 1000) continue;
    if (match[2] == null) return '$year';
    final month = int.parse(match[2]!);
    if (month < 1 || month > 12) continue;
    if (match[3] == null) return '${months[month - 1]} $year';
    final day = int.parse(match[3]!);
    final date = DateTime.utc(year, month, day);
    if (date.year != year || date.month != month || date.day != day) continue;
    return '$day ${months[month - 1]} $year';
  }
  return '';
}

/// Shared presentation for catalog and saved music; no fetching or playback state.
class BrowseHero extends StatelessWidget {
  const BrowseHero(
      {super.key,
      required this.title,
      required this.eyebrow,
      required this.artwork,
      required this.actions,
      this.subtitle,
      this.metadata = '',
      this.artist = false,
      this.compact = false});
  final String title, eyebrow, metadata;
  final Widget? subtitle;
  final Widget Function(double size) artwork;
  final List<Widget> actions;
  final bool artist, compact;

  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final narrow = bounds.maxWidth < 640;
        final size = artist
            ? (narrow ? bounds.maxWidth : 420.0)
            : narrow
                ? (compact ? 106.0 : 230.0)
                : (compact ? 240.0 : 316.0);
        final ink = artist ? NeatieDesign.ink : BrowsePalette.ink;
        final muted = artist ? NeatieDesign.muted : BrowsePalette.muted;
        final copy =
            Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Container(
                width: 18,
                height: 2,
                color: artist ? BrowsePalette.signal : ink),
            const SizedBox(width: 10),
            Flexible(
                child: Text(eyebrow,
                    style: NeatieDesign.eyebrow
                        .copyWith(color: muted, fontSize: 9)))
          ]),
          const SizedBox(height: 22),
          Text(title,
              maxLines: artist ? 2 : 3,
              overflow: TextOverflow.ellipsis,
              style: NeatieDesign.display(narrow
                      ? (compact ? 30 : 44)
                      : (artist
                          ? 82
                          : compact
                              ? 46
                              : 64))
                  .copyWith(color: ink)),
          if (subtitle != null) ...[const SizedBox(height: 12), subtitle!],
          if (metadata.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(metadata,
                style: TextStyle(fontSize: 12, color: muted, height: 1.6))
          ],
          if (actions.isNotEmpty) ...[
            const SizedBox(height: 24),
            Wrap(
                spacing: 12,
                runSpacing: 10,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: actions)
          ],
        ]);
        final art = Container(
            width: size,
            height: size,
            decoration: BoxDecoration(boxShadow: [
              BoxShadow(
                  color: Colors.black.withValues(alpha: artist ? .25 : .18),
                  blurRadius: artist ? 32 : 0,
                  offset: artist ? const Offset(0, 16) : const Offset(10, 10))
            ]),
            child: ClipRRect(
                borderRadius: BorderRadius.circular(artist ? 8 : 2),
                child: artwork(size)));
        final base = Theme.of(context);
        return Theme(
          data: base.copyWith(
            colorScheme:
                base.colorScheme.copyWith(primary: ink, onSurface: ink),
            iconTheme: IconThemeData(color: ink),
            iconButtonTheme: IconButtonThemeData(
                style: IconButton.styleFrom(foregroundColor: ink)),
            textButtonTheme: TextButtonThemeData(
                style: TextButton.styleFrom(foregroundColor: ink)),
            filledButtonTheme: FilledButtonThemeData(
                style: FilledButton.styleFrom(
                    backgroundColor:
                        artist ? BrowsePalette.signal : BrowsePalette.ink,
                    foregroundColor:
                        BrowsePalette.paper,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 24, vertical: 22))),
          ),
          child: ArtworkSurface(
              artwork: artist ? null : artwork,
              child: Container(
                key: const ValueKey('browse-hero'),
                decoration: BoxDecoration(
                    color:
                        artist ? const Color(0xff18251e) : Colors.transparent,
                    borderRadius: BorderRadius.circular(artist ? 10 : 4)),
                clipBehavior: Clip.antiAlias,
                child: artist
                    ? Stack(children: [
                        if (narrow)
                          Positioned(top: 0, right: 0, child: art)
                        else
                          Positioned(
                            right: 0,
                            top: 0,
                            bottom: 0,
                            width: bounds.maxWidth * .56,
                            child: FittedBox(
                              fit: BoxFit.cover,
                              clipBehavior: Clip.hardEdge,
                              child: SizedBox.square(
                                  dimension: size, child: artwork(size)),
                            ),
                          ),
                        Positioned.fill(
                            child: IgnorePointer(
                                child: DecoratedBox(
                                    decoration: BoxDecoration(
                                        gradient: LinearGradient(
                                            begin: Alignment.centerLeft,
                                            end: Alignment.centerRight,
                                            colors: [
                              const Color(0xff101913),
                              const Color(0xff101913)
                                  .withValues(alpha: narrow ? .25 : .85),
                              Colors.transparent
                            ],
                                            stops: const [
                              0,
                              .5,
                              1
                            ]))))),
                        Positioned.fill(
                            child: IgnorePointer(
                                child: DecoratedBox(
                                    decoration: BoxDecoration(
                                        gradient: LinearGradient(
                                            begin: Alignment.topCenter,
                                            end: Alignment.bottomCenter,
                                            colors: [
                              Colors.transparent,
                              const Color(0xff101913).withValues(alpha: .98)
                            ],
                                            stops: const [
                              .3,
                              1
                            ]))))),
                        Padding(
                            padding: EdgeInsets.all(narrow ? 24 : 40),
                            child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  SizedBox(height: narrow ? size * .52 : 70),
                                  SizedBox(
                                      width: narrow
                                          ? double.infinity
                                          : bounds.maxWidth * .67,
                                      child: copy),
                                ])),
                      ])
                    : Padding(
                        padding: EdgeInsets.all(narrow ? 24 : 40),
                        child: narrow && compact && bounds.maxWidth >= 400
                            ? Row(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                    art,
                                    const SizedBox(width: 20),
                                    Expanded(child: copy)
                                  ])
                            : narrow
                                ? Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                        Align(
                                            alignment: Alignment.centerLeft,
                                            child: art),
                                        const SizedBox(height: 38),
                                        copy,
                                      ])
                                : Row(children: [
                                    art,
                                    const SizedBox(width: 48),
                                    Expanded(child: copy)
                                  ])),
              )),
        );
      });
}

/// Pointer, keyboard and touch use the same action. Hover is supplementary.
class BrowseTrackRow extends StatelessWidget {
  const BrowseTrackRow(
      {super.key,
      required this.title,
      required this.artist,
      required this.artwork,
      required this.onTap,
      required this.actions,
      this.duration = '',
      this.album,
      this.albumTitle = '',
      this.number,
      this.selected = false});
  final String title, artist, duration, albumTitle;
  final Widget artwork;
  final VoidCallback? onTap;
  final List<Widget> actions;
  final Widget? album;
  final int? number;
  final bool selected;
  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final wide = bounds.maxWidth >= 620;
        return Material(
            color: selected ? NeatieDesign.raised : Colors.transparent,
            borderRadius: BorderRadius.circular(12),
            child: InkWell(
                onTap: onTap,
                borderRadius: BorderRadius.circular(12),
                hoverColor: NeatieDesign.accent.withValues(alpha: .055),
                child: Padding(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 10, vertical: 10),
                    child: Row(children: [
                      if (number != null && wide) ...[
                        SizedBox(
                            width: 25,
                            child: Text(number.toString().padLeft(2, '0'),
                                style: const TextStyle(
                                    fontSize: 11, color: NeatieDesign.muted))),
                        const SizedBox(width: 12)
                      ],
                      artwork,
                      const SizedBox(width: 14),
                      Expanded(
                          flex: 4,
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(title,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: TextStyle(
                                        fontSize: 13,
                                        fontWeight: FontWeight.w600,
                                        color: selected
                                            ? NeatieDesign.accent
                                            : NeatieDesign.ink)),
                                const SizedBox(height: 3),
                                Text(artist,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                        fontSize: 11,
                                        color: NeatieDesign.muted)),
                                if (!wide && albumTitle.isNotEmpty)
                                  Text(albumTitle,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                          fontSize: 10,
                                          color: NeatieDesign.muted)),
                              ])),
                      if (wide && album != null)
                        Expanded(flex: 3, child: album!),
                      if (duration.isNotEmpty &&
                          (wide || bounds.maxWidth > 370)) ...[
                        const SizedBox(width: 8),
                        SizedBox(
                            width: 38,
                            child: Text(duration,
                                textAlign: TextAlign.end,
                                style: const TextStyle(
                                    fontSize: 11, color: NeatieDesign.muted)))
                      ],
                      ...actions,
                    ]))));
      });
}

class BrowseSectionHeading extends StatelessWidget {
  const BrowseSectionHeading(
      {super.key, required this.title, this.caption, this.action});
  final String title;
  final String? caption;
  final Widget? action;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.only(top: 36, bottom: 18),
      child: Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
        Expanded(
            child:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: NeatieDesign.display(28)),
          if (caption != null) ...[
            const SizedBox(height: 6),
            Text(caption!,
                style: const TextStyle(fontSize: 12, color: NeatieDesign.muted))
          ],
        ])),
        if (action != null) action!,
      ]));
}

class SearchMasthead extends StatelessWidget {
  const SearchMasthead({super.key});
  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final narrow = bounds.maxWidth < 640;
        return Padding(
            padding: const EdgeInsets.only(top: 30, bottom: 30),
            child:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                const Text('NEATIE / DISCOVER', style: NeatieDesign.eyebrow),
                const Spacer(),
                if (!narrow)
                  const Text('SONGS  /  ARTISTS  /  ALBUMS',
                      style: NeatieDesign.eyebrow)
              ]),
            ]));
      });
}

class BrowseFact extends StatelessWidget {
  const BrowseFact({super.key, required this.label, required this.value});
  final String label, value;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.symmetric(vertical: 16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(label.toUpperCase(),
            style: NeatieDesign.eyebrow.copyWith(fontSize: 9)),
        const SizedBox(height: 8),
        Text(value,
            style: const TextStyle(
                fontSize: 14, color: NeatieDesign.ink, height: 1.5)),
      ]));
}
