import 'dart:async';

import 'package:flutter/material.dart';

import '../../logic/recommendation_feed_models.dart';
import '../../logic/track_metadata.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';
import 'track_menu_button.dart';

enum NeatieHomeTab {
  all('all', 'All', Icons.graphic_eq_rounded),
  chill('chill', 'Chill', Icons.nightlight_round),
  workout('workout', 'Workout', Icons.bolt_rounded),
  focus('focus', 'Focus', Icons.adjust_rounded),
  mood('mood', 'Mood', Icons.favorite_border_rounded);

  const NeatieHomeTab(this.id, this.label, this.icon);

  final String id;
  final String label;
  final IconData icon;
}

class NeatieQuietPicksLoadingIndicator extends StatefulWidget {
  const NeatieQuietPicksLoadingIndicator({super.key});

  @override
  State<NeatieQuietPicksLoadingIndicator> createState() =>
      _NeatieQuietPicksLoadingIndicatorState();
}

class _NeatieQuietPicksLoadingIndicatorState
    extends State<NeatieQuietPicksLoadingIndicator>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1250),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 42,
      height: 26,
      child: AnimatedBuilder(
        animation: _controller,
        builder: (context, child) {
          return CustomPaint(
            painter: _NeatieMarkLoadingPainter(_controller.value),
          );
        },
      ),
    );
  }
}

class _NeatieMarkLoadingPainter extends CustomPainter {
  const _NeatieMarkLoadingPainter(this.progress);

  final double progress;

  Path _markPath(Size size) {
    const barHeights = <double>[10, 18, 26, 32, 26, 18, 10];
    final scale = size.height / 34;
    final barWidth = 4.2 * scale;
    final gap = 2.5 * scale;
    final totalWidth = (barWidth * barHeights.length) +
        (gap * (barHeights.length - 1));
    final startX = (size.width - totalWidth) / 2;
    final centerY = size.height / 2;
    final path = Path();
    for (var index = 0; index < barHeights.length; index++) {
      final left = startX + index * (barWidth + gap);
      final height = barHeights[index] * scale;
      path.addRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(left, centerY - height / 2, barWidth, height),
          Radius.circular(barWidth / 2),
        ),
      );
      if (index < barHeights.length - 1) {
        path.addRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTWH(
              left + barWidth - 0.5,
              centerY - 1.35 * scale,
              gap + 1,
              2.7 * scale,
            ),
            Radius.circular(1.35 * scale),
          ),
        );
      }
    }
    return path;
  }

  @override
  void paint(Canvas canvas, Size size) {
    final path = _markPath(size);
    canvas.drawPath(
      path,
      Paint()
        ..color = Colors.white.withValues(alpha: 0.24)
        ..style = PaintingStyle.fill,
    );
    final highlightCenter = (-0.2 + (progress * 1.4)) * size.width;
    final highlightWidth = size.width * 0.46;
    canvas.drawPath(
      path,
      Paint()
        ..shader = const LinearGradient(
          colors: [
            Colors.transparent,
            Colors.white,
            Colors.transparent,
          ],
          stops: [0, 0.5, 1],
        ).createShader(
          Rect.fromLTWH(
            highlightCenter - highlightWidth / 2,
            0,
            highlightWidth,
            size.height,
          ),
        )
        ..style = PaintingStyle.fill,
    );
  }

  @override
  bool shouldRepaint(covariant _NeatieMarkLoadingPainter oldDelegate) =>
      oldDelegate.progress != progress;
}

class NeatieHomeTopBar extends StatelessWidget {
  const NeatieHomeTopBar({
    super.key,
    required this.displayName,
    required this.logoAsset,
    required this.avatarUrl,
    required this.onSearch,
    required this.onProfile,
  });

  final String displayName;
  final String logoAsset;
  final String? avatarUrl;
  final VoidCallback onSearch;
  final VoidCallback onProfile;

  @override
  Widget build(BuildContext context) {
    final hour = DateTime.now().hour;
    final greeting = hour < 12
        ? 'Good morning'
        : hour < 18
            ? 'Good afternoon'
            : 'Good evening';
    return SizedBox(
      height: 96,
      child: Stack(
        children: [
          Align(
            alignment: Alignment.topCenter,
            child: Image.asset(
              logoAsset,
              width: 50,
              height: 44,
              fit: BoxFit.contain,
              errorBuilder: (_, __, ___) => const Icon(
                Icons.graphic_eq_rounded,
                color: Colors.white,
                size: 40,
              ),
            ),
          ),
          Positioned(
            left: 0,
            top: 30,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  greeting,
                  style: const TextStyle(
                    color: neatieMutedText,
                    fontSize: 16,
                    fontWeight: FontWeight.w500,
                    letterSpacing: -0.4,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  displayName.isEmpty ? 'Listener' : displayName,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 25,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -1.0,
                  ),
                ),
              ],
            ),
          ),
          Positioned(
            right: 0,
            top: 30,
            child: Row(
              children: [
                IconButton(
                  onPressed: onSearch,
                  icon: const Icon(Icons.search_rounded),
                  color: Colors.white,
                  iconSize: 28,
                  tooltip: 'Search',
                ),
                const SizedBox(width: 8),
                GestureDetector(
                  onTap: onProfile,
                  child: Container(
                    width: 40,
                    height: 40,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: neatieStroke, width: 1.4),
                    ),
                    child: ClipOval(
                      child: avatarUrl != null && avatarUrl!.isNotEmpty
                          ? Image.network(
                              avatarUrl!,
                              fit: BoxFit.cover,
                              errorBuilder: (_, __, ___) =>
                                  const _AvatarFallback(),
                            )
                          : const _AvatarFallback(),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _AvatarFallback extends StatelessWidget {
  const _AvatarFallback();

  @override
  Widget build(BuildContext context) {
    return const ColoredBox(
      color: Color(0xFF111111),
      child: Icon(Icons.person_rounded, color: Colors.white70),
    );
  }
}

class NeatieFeaturedAlbumHero extends StatefulWidget {
  const NeatieFeaturedAlbumHero({
    super.key,
    required this.albums,
    required this.onPlay,
    required this.onOpen,
  });

  final List<Map<String, dynamic>> albums;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onOpen;

  @override
  State<NeatieFeaturedAlbumHero> createState() =>
      _NeatieFeaturedAlbumHeroState();
}

class _NeatieFeaturedAlbumHeroState extends State<NeatieFeaturedAlbumHero> {
  static const _advanceInterval = Duration(seconds: 7);
  final PageController _pageController = PageController();
  Timer? _advanceTimer;
  int _activeIndex = 0;

  @override
  void initState() {
    super.initState();
    _resetAdvanceTimer();
  }

  @override
  void didUpdateWidget(covariant NeatieFeaturedAlbumHero oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.albums.length != oldWidget.albums.length ||
        _activeIndex >= widget.albums.length) {
      _activeIndex = 0;
      if (_pageController.hasClients) {
        _pageController.jumpToPage(0);
      }
    }
    _resetAdvanceTimer();
  }

  @override
  void dispose() {
    _advanceTimer?.cancel();
    _pageController.dispose();
    super.dispose();
  }

  void _resetAdvanceTimer() {
    _advanceTimer?.cancel();
    if (widget.albums.length < 2) return;
    _advanceTimer = Timer(_advanceInterval, _advance);
  }

  void _advance() {
    if (!mounted || !_pageController.hasClients || widget.albums.length < 2) {
      _resetAdvanceTimer();
      return;
    }
    final nextIndex = (_activeIndex + 1) % widget.albums.length;
    _pageController
        .animateToPage(
          nextIndex,
          duration: const Duration(milliseconds: 650),
          curve: Curves.easeInOutCubic,
        )
        .whenComplete(_resetAdvanceTimer);
  }

  void _goToPage(int index) {
    _resetAdvanceTimer();
    _pageController.animateToPage(
      index,
      duration: const Duration(milliseconds: 450),
      curve: Curves.easeOutCubic,
    );
  }

  @override
  Widget build(BuildContext context) {
    if (widget.albums.isEmpty) {
      return const SizedBox.shrink();
    }
    final compact = MediaQuery.of(context).size.width < 420;
    final heroHeight = compact ? 206.0 : 214.0;
    return NeatieSurface(
      height: heroHeight,
      radius: neatieRadiusXLarge,
      padding: EdgeInsets.zero,
      color: Colors.white.withValues(alpha: 0.025),
      blur: false,
      child: Listener(
        onPointerDown: (_) => _resetAdvanceTimer(),
        child: Stack(
          fit: StackFit.expand,
          children: [
            NotificationListener<UserScrollNotification>(
              onNotification: (notification) {
                _resetAdvanceTimer();
                return false;
              },
              child: PageView.builder(
                controller: _pageController,
                itemCount: widget.albums.length,
                onPageChanged: (index) {
                  setState(() => _activeIndex = index);
                  _resetAdvanceTimer();
                },
                itemBuilder: (context, index) => _FeaturedAlbumPage(
                  album: widget.albums[index],
                  compact: compact,
                  onOpen: () => widget.onOpen(widget.albums[index]),
                  onPlay: () => widget.onPlay(widget.albums[index]),
                ),
              ),
            ),
            if (widget.albums.length > 1)
              Positioned(
                right: compact ? 16 : 18,
                bottom: 17,
                child: _FeaturedCarouselIndicator(
                  itemCount: widget.albums.length,
                  activeIndex: _activeIndex,
                  onSelected: _goToPage,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _FeaturedCarouselIndicator extends StatefulWidget {
  const _FeaturedCarouselIndicator({
    required this.itemCount,
    required this.activeIndex,
    required this.onSelected,
  });

  final int itemCount;
  final int activeIndex;
  final ValueChanged<int> onSelected;

  @override
  State<_FeaturedCarouselIndicator> createState() =>
      _FeaturedCarouselIndicatorState();
}

class _FeaturedCarouselIndicatorState
    extends State<_FeaturedCarouselIndicator> {
  int _direction = 1;

  @override
  void didUpdateWidget(covariant _FeaturedCarouselIndicator oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.activeIndex == widget.activeIndex || widget.itemCount <= 0) {
      return;
    }
    final forwardDistance =
        (widget.activeIndex - oldWidget.activeIndex) % widget.itemCount;
    final backwardDistance =
        (oldWidget.activeIndex - widget.activeIndex) % widget.itemCount;
    _direction = forwardDistance <= backwardDistance ? 1 : -1;
  }

  @override
  Widget build(BuildContext context) {
    if (widget.itemCount <= 0) return const SizedBox.shrink();
    final visibleCount = widget.itemCount.clamp(0, 5).toInt();
    final offsets = switch (visibleCount) {
      1 => const [0],
      2 => const [0, 1],
      3 => const [-1, 0, 1],
      4 => const [-1, 0, 1, 2],
      _ => const [-2, -1, 0, 1, 2],
    };
    return ClipRect(
      child: AnimatedSwitcher(
        duration: const Duration(milliseconds: 420),
        reverseDuration: const Duration(milliseconds: 360),
        switchInCurve: Curves.easeOutCubic,
        switchOutCurve: Curves.easeInCubic,
        transitionBuilder: (child, animation) {
          final entering = child.key == ValueKey(widget.activeIndex);
          final begin = entering
              ? Offset(0.32 * _direction, 0)
              : Offset(-0.32 * _direction, 0);
          return FadeTransition(
            opacity: animation,
            child: SlideTransition(
              position: Tween<Offset>(
                begin: begin,
                end: Offset.zero,
              ).animate(animation),
              child: child,
            ),
          );
        },
        child: Row(
          key: ValueKey(widget.activeIndex),
          mainAxisSize: MainAxisSize.min,
          children: offsets.map((offset) {
            final index =
                (widget.activeIndex + offset) % widget.itemCount;
            final isActive = offset == 0;
            final isEdge = widget.itemCount > visibleCount &&
                (offset == offsets.first || offset == offsets.last);
            return GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: () => widget.onSelected(index),
              child: Padding(
                padding: const EdgeInsets.all(5),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 320),
                  curve: Curves.easeOutCubic,
                  width: isActive ? 18 : 6,
                  height: 6,
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(
                      alpha: isActive
                          ? 1
                          : isEdge
                              ? 0.14
                              : 0.38,
                    ),
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
              ),
            );
          }).toList(growable: false),
        ),
      ),
    );
  }
}

class _FeaturedAlbumPage extends StatelessWidget {
  const _FeaturedAlbumPage({
    required this.album,
    required this.compact,
    required this.onOpen,
    required this.onPlay,
  });

  final Map<String, dynamic> album;
  final bool compact;
  final VoidCallback onOpen;
  final VoidCallback onPlay;

  @override
  Widget build(BuildContext context) {
    final title = album['title']?.toString().trim();
    final artist = album['artist']?.toString().trim();
    final displayTitle =
        title?.isNotEmpty == true ? title! : 'Featured album';
    final heroPadding = compact ? 16.0 : 18.0;
    return InkWell(
      borderRadius: BorderRadius.circular(neatieRadiusXLarge),
      onTap: onOpen,
      child: Stack(
        fit: StackFit.expand,
        children: [
          Opacity(
            opacity: 0.56,
            child: AppArtwork(
              thumbnail: album['thumbnail'],
              width: double.infinity,
              height: double.infinity,
              radius: neatieRadiusXLarge,
            ),
          ),
          Container(
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(neatieRadiusXLarge),
              gradient: const LinearGradient(
                begin: Alignment.centerLeft,
                end: Alignment.centerRight,
                colors: [
                  Color(0xF0000000),
                  Color(0xBB000000),
                  Color(0x18000000),
                ],
              ),
            ),
          ),
          Positioned(
            left: heroPadding,
            right: heroPadding,
            top: heroPadding,
            child: const Text(
              'F E A T U R E D',
              style: TextStyle(
                color: neatieMutedText,
                fontSize: 11,
                fontWeight: FontWeight.w800,
                letterSpacing: 5,
              ),
            ),
          ),
          Positioned(
            left: heroPadding,
            right: compact ? 28 : 64,
            top: compact ? 50 : 52,
            child: Text(
              displayTitle,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: Colors.white,
                fontSize: compact ? 20.0 : 23.0,
                fontWeight: FontWeight.w300,
                letterSpacing: compact ? 4.0 : 5.0,
              ),
            ),
          ),
          Positioned(
            left: heroPadding,
            right: heroPadding,
            top: compact ? 84 : 90,
            child: Text(
              artist?.isNotEmpty == true ? artist! : 'Album selected for you',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: neatieMutedText,
                fontSize: compact ? 13.0 : 14.0,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          Positioned(
            left: heroPadding,
            bottom: 14,
            child: IconButton.filled(
              onPressed: onPlay,
              style: IconButton.styleFrom(
                backgroundColor: Colors.white,
                foregroundColor: Colors.black,
                fixedSize: Size.square(compact ? 44 : 48),
              ),
              icon: const Icon(Icons.play_arrow_rounded),
              tooltip: artist?.isNotEmpty == true
                  ? 'Play $displayTitle by $artist'
                  : 'Play $displayTitle',
            ),
          ),
        ],
      ),
    );
  }
}

class NeatieHomeTabBar extends StatelessWidget {
  const NeatieHomeTabBar({
    super.key,
    required this.selected,
    required this.availableTabs,
    required this.onSelected,
  });

  final NeatieHomeTab selected;
  final Set<NeatieHomeTab> availableTabs;
  final ValueChanged<NeatieHomeTab> onSelected;

  @override
  Widget build(BuildContext context) {
    final tabs = NeatieHomeTab.values
        .where((tab) => tab == NeatieHomeTab.all || availableTabs.contains(tab))
        .toList(growable: false);
    return SizedBox(
      height: 48,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        physics: const BouncingScrollPhysics(),
        itemCount: tabs.length,
        separatorBuilder: (_, __) => const SizedBox(width: 9),
        itemBuilder: (context, index) {
          final tab = tabs[index];
          return Center(
            child: NeatiePill(
              label: tab.label,
              icon: tab.icon,
              selected: selected == tab,
              onTap: () => onSelected(tab),
            ),
          );
        },
      ),
    );
  }
}

class NeatieSectionTitle extends StatelessWidget {
  const NeatieSectionTitle({
    super.key,
    required this.title,
    this.onViewAll,
  });

  final String title;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        children: [
          Expanded(
            child: Text(
              title,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 19,
                fontWeight: FontWeight.w800,
                letterSpacing: -0.5,
              ),
            ),
          ),
          if (onViewAll != null)
            IconButton(
              onPressed: onViewAll,
              tooltip: 'Open $title',
              icon: const Icon(Icons.chevron_right_rounded, size: 20),
              color: neatieMutedText,
            ),
        ],
      ),
    );
  }
}

class NeatieTrackStrip extends StatelessWidget {
  const NeatieTrackStrip({
    super.key,
    required this.title,
    required this.tracks,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.onPrime,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
  final ValueChanged<Map<String, dynamic>>? onPrime;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title, onViewAll: onViewAll),
        SizedBox(
          height: 172,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: tracks.length,
            separatorBuilder: (_, __) => const SizedBox(width: 12),
            itemBuilder: (context, index) {
              final track = tracks[index];
              return SizedBox(
                width: 120,
                child: _SquareTrackCard(
                  track: track,
                  onPlay: () => onPlay(track),
                  onPrime: onPrime == null ? null : () => onPrime!(track),
                  onMenuDetails: () => onMenuDetails(track),
                  onAddToPlaylist: onAddToPlaylist,
                  onStartStation: onStartStation,
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class NeatieAssistantEntryCard extends StatelessWidget {
  const NeatieAssistantEntryCard({
    super.key,
    required this.onTap,
  });

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      radius: 18,
      padding: const EdgeInsets.fromLTRB(14, 13, 14, 13),
      color: Colors.white.withValues(alpha: 0.035),
      blur: false,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(18),
        child: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.08),
                borderRadius: BorderRadius.circular(13),
              ),
              child: const Icon(
                Icons.auto_awesome_rounded,
                color: Colors.white,
                size: 21,
              ),
            ),
            const SizedBox(width: 12),
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Ask Neatie',
                    style: TextStyle(
                      color: Colors.white,
                      fontSize: 14.5,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  SizedBox(height: 2),
                  Text(
                    'Describe a mood, artist, era, or playlist idea.',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: neatieMutedText, fontSize: 11.5),
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_right_rounded, color: neatieMutedText),
          ],
        ),
      ),
    );
  }
}

class NeatieHotPickCard extends StatelessWidget {
  const NeatieHotPickCard({
    super.key,
    required this.track,
    required this.onPlay,
    this.onPrime,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback? onPrime;

  @override
  Widget build(BuildContext context) {
    final title = track['title']?.toString().trim();
    final subtitle = formatTrackSubtitle(track);
    return NeatieSurface(
      radius: 20,
      color: Colors.white.withValues(alpha: 0.055),
      blur: false,
      padding: const EdgeInsets.all(14),
      child: InkWell(
        onTap: onPlay,
        onTapDown: onPrime == null ? null : (_) => onPrime!(),
        borderRadius: BorderRadius.circular(20),
        child: Row(
          children: [
            AppArtwork(
              thumbnail: track['thumbnail'],
              videoId: extractPlaybackSourceId(track),
              width: 78,
              height: 78,
              radius: 14,
            ),
            const SizedBox(width: 15),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'HOT FOR YOU',
                    style: TextStyle(
                      color: neatieDimText,
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 1.2,
                    ),
                  ),
                  const SizedBox(height: 7),
                  Text(
                    title?.isNotEmpty == true ? title! : 'Today\'s pick',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 16,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: neatieMutedText),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 10),
            Container(
              width: 56,
              height: 56,
              decoration: const BoxDecoration(
                color: Colors.white,
                shape: BoxShape.circle,
              ),
              child: const Icon(
                Icons.play_arrow_rounded,
                color: Colors.black,
                size: 32,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class NeatieYourLikesRow extends StatelessWidget {
  const NeatieYourLikesRow({
    super.key,
    required this.tracks,
    required this.onPlay,
    this.onPrime,
    this.onOpen,
    this.onViewAll,
  });

  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>>? onPrime;
  final VoidCallback? onOpen;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    final visible = tracks.take(4).toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: 'Your likes', onViewAll: onViewAll),
        NeatieSurface(
          radius: 18,
          color: Colors.transparent,
          blur: false,
          padding: EdgeInsets.zero,
          child: DecoratedBox(
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(18),
              gradient: const LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  Color(0xFF37110A),
                  Color(0xFF151515),
                  Color(0xFF090909),
                ],
              ),
            ),
            child: Material(
              color: Colors.transparent,
              child: InkWell(
                onTap: onOpen ?? onViewAll,
                borderRadius: BorderRadius.circular(18),
                child: Padding(
                  padding: const EdgeInsets.all(10),
                  child: Row(
                    children: [
                      Container(
                        width: 42,
                        height: 42,
                        decoration: BoxDecoration(
                          color: const Color(0xFFFF6A3D)
                              .withValues(alpha: 0.18),
                          borderRadius: BorderRadius.circular(14),
                        ),
                        child: const Icon(
                          Icons.favorite_rounded,
                          color: Colors.white,
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: SizedBox(
                          height: 58,
                          child: ListView.separated(
                            scrollDirection: Axis.horizontal,
                            itemCount: visible.length,
                            separatorBuilder: (_, __) =>
                                const SizedBox(width: 10),
                            itemBuilder: (context, index) => SizedBox(
                              width: 152,
                              child: _LikedMiniTile(
                                track: visible[index],
                                onTap: () => onPlay(visible[index]),
                                onPrime: onPrime == null
                                    ? null
                                    : () => onPrime!(visible[index]),
                              ),
                            ),
                          ),
                        ),
                      ),
                      if (onViewAll != null) ...[
                        const SizedBox(width: 8),
                        IconButton(
                          onPressed: onViewAll,
                          icon: const Icon(Icons.shuffle_rounded),
                          color: Colors.white,
                        ),
                      ],
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class _LikedMiniTile extends StatelessWidget {
  const _LikedMiniTile({
    required this.track,
    required this.onTap,
    this.onPrime,
  });

  final Map<String, dynamic> track;
  final VoidCallback onTap;
  final VoidCallback? onPrime;

  @override
  Widget build(BuildContext context) {
    final title = track['title']?.toString().trim();
    final subtitle = formatTrackSubtitle(track);
    return InkWell(
      onTap: onTap,
      onTapDown: onPrime == null ? null : (_) => onPrime!(),
      borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.06),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          children: [
            AppArtwork(
              thumbnail: track['thumbnail'],
              videoId: extractPlaybackSourceId(track),
              width: 42,
              height: 42,
              radius: 8,
            ),
            const SizedBox(width: 9),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title?.isNotEmpty == true ? title! : 'Liked song',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 12,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: neatieMutedText,
                      fontSize: 10.5,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SquareTrackCard extends StatelessWidget {
  const _SquareTrackCard({
    required this.track,
    required this.onPlay,
    this.onPrime,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback? onPrime;
  final VoidCallback onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  @override
  Widget build(BuildContext context) {
    final title = track['title']?.toString().trim();
    final subtitle = formatTrackSubtitle(track);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        AspectRatio(
          aspectRatio: 1,
          child: InkWell(
            onTap: onPlay,
            onTapDown: onPrime == null ? null : (_) => onPrime!(),
            borderRadius: BorderRadius.circular(11),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(11),
              child: AppArtwork(
                thumbnail: track['thumbnail'],
                videoId: extractPlaybackSourceId(track),
                width: double.infinity,
                height: double.infinity,
                radius: 11,
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        InkWell(
          onTap: onPlay,
          onTapDown: onPrime == null ? null : (_) => onPrime!(),
          child: Text(
            title?.isNotEmpty == true ? title! : 'Unknown track',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 12.5,
              fontWeight: FontWeight.w700,
            ),
          ),
        ),
        const SizedBox(height: 2),
        Row(
          children: [
            Expanded(
              child: Text(
                subtitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: neatieMutedText,
                  fontSize: 11,
                ),
              ),
            ),
            TrackMenuButton(
              track: track,
              onOpenDetails: onMenuDetails,
              onAddToPlaylist: onAddToPlaylist,
              onStartStation: onStartStation,
              buttonSize: 24,
              iconSize: 17,
            ),
          ],
        ),
      ],
    );
  }
}

class NeatieMadeForYouRow extends StatelessWidget {
  const NeatieMadeForYouRow({
    super.key,
    required this.items,
    required this.onPlay,
    this.onViewAll,
  });

  final List<NeatieMixTileData> items;
  final ValueChanged<NeatieMixTileData> onPlay;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) return const SizedBox.shrink();
    const mixColors = <Color>[
      Color(0xFF08D8D1),
      Color(0xFFE4F637),
      Color(0xFFFF3A35),
      Color(0xFF8EA7FF),
      Color(0xFFFFB04A),
      Color(0xFF74E08A),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: 'Made for you', onViewAll: onViewAll),
        SizedBox(
          height: 188,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: items.length,
            separatorBuilder: (_, __) => const SizedBox(width: 18),
            itemBuilder: (context, index) {
              final item = items[index];
              final accent = mixColors[index % mixColors.length];
              return SizedBox(
                width: 124,
                child: InkWell(
                  onTap: () => onPlay(item),
                  borderRadius: BorderRadius.circular(8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      SizedBox(
                        height: 124,
                        child: Stack(
                          clipBehavior: Clip.none,
                          fit: StackFit.expand,
                          children: [
                            ClipRRect(
                              borderRadius: BorderRadius.circular(7),
                              child: item.thumbnail != null
                                  ? Transform.scale(
                                      scale: 1.16,
                                      child: AppArtwork(
                                        thumbnail: item.thumbnail,
                                        videoId: item.videoId,
                                        width: 124,
                                        height: 124,
                                        radius: 7,
                                        fit: BoxFit.cover,
                                      ),
                                    )
                                  : ColoredBox(
                                      color: Colors.white.withValues(alpha: 0.08),
                                      child: const Icon(
                                        Icons.graphic_eq_rounded,
                                        color: Colors.white,
                                        size: 34,
                                      ),
                                    ),
                            ),
                            Positioned(
                              left: 6,
                              bottom: 9,
                              child: Container(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 4,
                                  vertical: 2,
                                ),
                                color: accent,
                                constraints: const BoxConstraints(maxWidth: 112),
                                child: Text(
                                  item.title,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Colors.black,
                                    fontSize: 10.5,
                                    fontWeight: FontWeight.w900,
                                    height: 1,
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 9),
                      Text(
                        item.subtitle.isNotEmpty ? item.subtitle : item.title,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: neatieMutedText,
                          fontSize: 10.5,
                          fontWeight: FontWeight.w600,
                          height: 1.18,
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class NeatieMixTileData {
  const NeatieMixTileData({
    required this.title,
    required this.subtitle,
    required this.tracks,
    this.mix = const <String, dynamic>{},
    this.thumbnail,
    this.videoId,
  });

  final String title;
  final String subtitle;
  final List<Map<String, dynamic>> tracks;
  final Map<String, dynamic> mix;
  final dynamic thumbnail;
  final String? videoId;
}

class NeatiePopularRadioRow extends StatelessWidget {
  const NeatiePopularRadioRow({
    super.key,
    this.title = 'Popular Radio',
    required this.radios,
    required this.onOpen,
    required this.onPlay,
    required this.isLiked,
    required this.onLike,
    required this.colorSeed,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> radios;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final bool Function(Map<String, dynamic>) isLiked;
  final ValueChanged<Map<String, dynamic>> onLike;
  final int colorSeed;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (radios.isEmpty) return const SizedBox.shrink();
    final visible = radios.take(16).toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title, onViewAll: onViewAll),
        const SizedBox(height: 8),
        SizedBox(
          height: 238,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: visible.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final radio = visible[index];
              return _PopularRadioCard(
                radio: radio,
                index: index,
                colorSeed: colorSeed,
                isLiked: isLiked(radio),
                onOpen: () => onOpen(radio),
                onPlay: () => onPlay(radio),
                onLike: () => onLike(radio),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _PopularRadioCard extends StatelessWidget {
  const _PopularRadioCard({
    required this.radio,
    required this.index,
    required this.colorSeed,
    required this.isLiked,
    required this.onOpen,
    required this.onPlay,
    required this.onLike,
  });

  final Map<String, dynamic> radio;
  final int index;
  final int colorSeed;
  final bool isLiked;
  final VoidCallback onOpen;
  final VoidCallback onPlay;
  final VoidCallback onLike;

  static const _palette = <Color>[
    Color(0xFFB98270),
    Color(0xFF71A397),
    Color(0xFF738CA6),
    Color(0xFF8D7894),
    Color(0xFFA68A5F),
    Color(0xFFA77479),
    Color(0xFF778D70),
    Color(0xFF8E8175),
  ];

  Color get _accent {
    final identity =
        '${radio['id'] ?? radio['artist_name'] ?? radio['title'] ?? index}';
    return _palette[(identity.hashCode ^ colorSeed).abs() % _palette.length];
  }

  List<String> get _collageImages {
    final raw = radio['collage_images'];
    final Iterable<String> images = raw is List
        ? raw.map((item) => item.toString()).where((item) => item.isNotEmpty)
        : const <String>[];
    final thumbnail = radio['thumbnail']?.toString().trim();
    final artistThumbnail = radio['artist_thumbnail']?.toString().trim();
    final rawTracks = radio['tracks'] ?? radio['items'];
    final trackArtistImages = rawTracks is List
        ? rawTracks.whereType<Map>().map(
              (track) => track['artist_thumbnail']?.toString().trim() ?? '',
            )
        : const Iterable<String>.empty();
    return <String>{
      if (thumbnail != null && thumbnail.isNotEmpty) thumbnail,
      if (artistThumbnail != null && artistThumbnail.isNotEmpty)
        artistThumbnail,
      ...images,
      ...trackArtistImages.where((image) => image.isNotEmpty),
    }.take(5).toList(growable: false);
  }

  String get _title {
    final artist = radio['artist_name']?.toString().trim();
    if (artist != null && artist.isNotEmpty) return artist;
    return radio['title']?.toString().trim() ?? 'Artist Radio';
  }

  String get _subtitle {
    final subtitle = radio['subtitle']?.toString().trim();
    if (subtitle != null && subtitle.isNotEmpty) return subtitle;
    final count = (radio['track_count'] as num?)?.toInt() ?? 0;
    return count > 0 ? '$count tracks from this orbit' : 'Songs from your taste';
  }

  Widget _circleArt(String? thumbnail, double size, {bool main = false}) {
    final borderColor =
        main ? Colors.black.withValues(alpha: 0.16) : Colors.white.withValues(alpha: 0.28);
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(color: borderColor, width: main ? 4 : 2),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: main ? 0.28 : 0.18),
            blurRadius: main ? 16 : 8,
            offset: const Offset(0, 6),
          ),
        ],
      ),
      child: ClipOval(
        child: thumbnail != null && thumbnail.isNotEmpty
            ? AppArtwork(
                thumbnail: thumbnail,
                width: size,
                height: size,
                radius: size / 2,
              )
            : ColoredBox(
                color: Colors.black.withValues(alpha: 0.18),
                child: Icon(
                  Icons.graphic_eq_rounded,
                  color: Colors.white.withValues(alpha: 0.82),
                  size: size * 0.42,
                ),
              ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final images = _collageImages;
    final primary = images.isNotEmpty ? images.first : null;
    final artworkLayers = images.length <= 1
        ? <Widget>[
            Positioned(
              left: 50,
              top: 45,
              child: _circleArt(primary, 90, main: true),
            ),
          ]
        : images.length == 2
            ? <Widget>[
                Positioned(left: 27, top: 54, child: _circleArt(images[0], 70)),
                Positioned(right: 27, top: 54, child: _circleArt(images[1], 70)),
              ]
            : <Widget>[
                Positioned(left: 18, top: 76, child: _circleArt(images[1], 54)),
                Positioned(right: 18, top: 72, child: _circleArt(images[2], 52)),
                Positioned(
                  left: 58,
                  top: 48,
                  child: _circleArt(primary, 82, main: true),
                ),
              ];
    return SizedBox(
      width: 190,
      child: InkWell(
        onTap: onOpen,
        borderRadius: BorderRadius.circular(24),
        child: Ink(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                _accent.withValues(alpha: 0.98),
                Color.lerp(_accent, Colors.black, 0.22) ?? _accent,
              ],
            ),
            border: Border.all(color: Colors.white.withValues(alpha: 0.10)),
            boxShadow: [
              BoxShadow(
                color: _accent.withValues(alpha: 0.20),
                blurRadius: 22,
                offset: const Offset(0, 12),
              ),
            ],
          ),
          child: Stack(
            children: [
              Positioned(
                right: -36,
                bottom: -26,
                child: Icon(
                  Icons.radio_rounded,
                  size: 142,
                  color: Colors.black.withValues(alpha: 0.08),
                ),
              ),
              Positioned(
                top: 14,
                left: 14,
                child: GestureDetector(
                  onTap: onLike,
                  child: Icon(
                    isLiked
                        ? Icons.favorite_rounded
                        : Icons.favorite_border_rounded,
                    color: Colors.black.withValues(alpha: 0.76),
                    size: 21,
                  ),
                ),
              ),
              Positioned(
                top: 16,
                right: 16,
                child: Text(
                  'RADIO',
                  style: TextStyle(
                    color: Colors.black.withValues(alpha: 0.78),
                    fontSize: 11,
                    fontWeight: FontWeight.w900,
                    letterSpacing: 1.8,
                  ),
                ),
              ),
              ...artworkLayers,
              Positioned(
                right: 14,
                bottom: 70,
                child: GestureDetector(
                  onTap: onPlay,
                  child: Container(
                    width: 44,
                    height: 44,
                    decoration: const BoxDecoration(
                      color: Colors.white,
                      shape: BoxShape.circle,
                    ),
                    child: const Icon(
                      Icons.play_arrow_rounded,
                      color: Colors.black,
                      size: 30,
                    ),
                  ),
                ),
              ),
              Positioned(
                left: 16,
                right: 16,
                bottom: 18,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.black,
                        fontSize: 18,
                        fontWeight: FontWeight.w900,
                        letterSpacing: -0.35,
                      ),
                    ),
                    const SizedBox(height: 5),
                    Text(
                      _subtitle,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Colors.black.withValues(alpha: 0.66),
                        fontSize: 10.5,
                        fontWeight: FontWeight.w700,
                        height: 1.2,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RankedTrackTile extends StatelessWidget {
  const _RankedTrackTile({
    required this.rank,
    required this.track,
    required this.onPlay,
    this.onPrime,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final int rank;
  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback? onPrime;
  final VoidCallback onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  @override
  Widget build(BuildContext context) {
    final subtitle = formatTrackSubtitle(track);
    return Row(
      children: [
        SizedBox(
          width: 24,
          child: Text(
            '$rank',
            style: const TextStyle(
              color: Colors.white,
              fontSize: 14,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
        InkWell(
          onTap: onPlay,
          onTapDown: onPrime == null ? null : (_) => onPrime!(),
          borderRadius: BorderRadius.circular(7),
          child: AppArtwork(
            thumbnail: track['thumbnail'],
            videoId: extractPlaybackSourceId(track),
            width: 48,
            height: 48,
            radius: 7,
          ),
        ),
        const SizedBox(width: 11),
        Expanded(
          child: InkWell(
            onTap: onPlay,
            onTapDown: onPrime == null ? null : (_) => onPrime!(),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  track['title']?.toString() ?? 'Unknown track',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 12.8,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  subtitle,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style:
                      const TextStyle(color: neatieMutedText, fontSize: 11.5),
                ),
              ],
            ),
          ),
        ),
        TrackMenuButton(
          track: track,
          onOpenDetails: onMenuDetails,
          onAddToPlaylist: onAddToPlaylist,
          onStartStation: onStartStation,
          buttonSize: 36,
          iconSize: 18,
        ),
      ],
    );
  }
}

class _TrackSlideIn extends StatefulWidget {
  const _TrackSlideIn({
    super.key,
    required this.child,
    required this.animate,
    required this.delay,
  });

  final Widget child;
  final bool animate;
  final Duration delay;

  @override
  State<_TrackSlideIn> createState() => _TrackSlideInState();
}

class _TrackSlideInState extends State<_TrackSlideIn>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final Animation<double> _opacity;
  late final Animation<Offset> _position;
  Timer? _startTimer;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 360),
      value: widget.animate ? 0 : 1,
    );
    final curve = CurvedAnimation(
      parent: _controller,
      curve: Curves.easeOutQuart,
    );
    _opacity = Tween<double>(begin: 0, end: 1).animate(curve);
    _position = Tween<Offset>(
      begin: const Offset(0, 0.14),
      end: Offset.zero,
    ).animate(curve);
    if (widget.animate) {
      _startTimer = Timer(widget.delay, () => _controller.forward());
    }
  }

  @override
  void dispose() {
    _startTimer?.cancel();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: _opacity,
      child: SlideTransition(
        position: _position,
        child: widget.child,
      ),
    );
  }
}

class NeatieLongTrackList extends StatefulWidget {
  const NeatieLongTrackList({
    super.key,
    required this.title,
    required this.tracks,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.onPrime,
    this.maxItems = 48,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
  final ValueChanged<Map<String, dynamic>>? onPrime;
  final int maxItems;
  final VoidCallback? onViewAll;

  @override
  State<NeatieLongTrackList> createState() => _NeatieLongTrackListState();
}

class _NeatieLongTrackListState extends State<NeatieLongTrackList> {
  final Set<String> _knownTrackKeys = <String>{};
  bool _initialized = false;

  @override
  Widget build(BuildContext context) {
    if (widget.tracks.isEmpty) return const SizedBox.shrink();
    final visible = widget.tracks.take(widget.maxItems).toList(growable: false);
    final visibleKeys = [
      for (final track in visible) recommendationRowItemKey('track', track),
    ];
    final enteringKeys = _initialized
        ? visibleKeys.where((key) => !_knownTrackKeys.contains(key)).toList()
        : const <String>[];
    final enteringOrder = <String, int>{
      for (var index = 0; index < enteringKeys.length; index++)
        enteringKeys[index]: index,
    };
    _knownTrackKeys.addAll(visibleKeys);
    _initialized = true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: widget.title, onViewAll: widget.onViewAll),
        Wrap(
          runSpacing: 13,
          children: [
            for (var index = 0; index < visible.length; index++)
              _TrackSlideIn(
                key: ValueKey<String>(
                  'track-slide:${visibleKeys[index]}',
                ),
                animate: enteringOrder.containsKey(visibleKeys[index]),
                delay: Duration(
                  milliseconds: (enteringOrder[visibleKeys[index]] ?? 0)
                          .clamp(0, 8)
                          .toInt() *
                      28,
                ),
                child: SizedBox(
                  width: MediaQuery.of(context).size.width >= 700
                      ? (MediaQuery.of(context).size.width - 64) / 2
                      : double.infinity,
                  child: _RankedTrackTile(
                    rank: index + 1,
                    track: visible[index],
                    onPlay: () => widget.onPlay(visible[index]),
                    onPrime: widget.onPrime == null
                        ? null
                        : () => widget.onPrime!(visible[index]),
                    onMenuDetails: () => widget.onMenuDetails(visible[index]),
                    onAddToPlaylist: widget.onAddToPlaylist,
                    onStartStation: widget.onStartStation,
                  ),
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class NeatieAlbumStrip extends StatelessWidget {
  const NeatieAlbumStrip({
    super.key,
    required this.title,
    required this.albums,
    required this.onOpen,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> albums;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (albums.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title, onViewAll: onViewAll),
        SizedBox(
          height: 186,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: albums.length,
            separatorBuilder: (_, __) => const SizedBox(width: 20),
            itemBuilder: (context, index) {
              final album = albums[index];
              final artist =
                  album['artist']?.toString().trim() ?? 'Unknown artist';
              return SizedBox(
                width: 124,
                child: InkWell(
                  onTap: () => onOpen(album),
                  borderRadius: BorderRadius.circular(6),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      AppArtwork(
                        thumbnail: album['thumbnail'],
                        width: 124,
                        height: 124,
                        radius: 6,
                        fit: BoxFit.cover,
                      ),
                      const SizedBox(height: 9),
                      Text(
                        album['title']?.toString() ?? 'Unknown Album',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 12.4,
                          fontWeight: FontWeight.w800,
                          height: 1.1,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        artist.isEmpty ? 'Unknown artist' : artist,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: neatieMutedText,
                          fontSize: 10.3,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class NeatieArtistStrip extends StatelessWidget {
  const NeatieArtistStrip({
    super.key,
    required this.artists,
    required this.onOpen,
    this.onViewAll,
  });

  final List<Map<String, dynamic>> artists;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (artists.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: 'Artists to explore', onViewAll: onViewAll),
        SizedBox(
          height: 96,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: artists.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final artist = artists[index];
              return SizedBox(
                width: 66,
                child: InkWell(
                  onTap: () => onOpen(artist),
                  borderRadius: BorderRadius.circular(4),
                  child: Column(
                    children: [
                      ArtistArtwork(
                        thumbnail: artist['thumbnail'],
                        width: 56,
                        height: 56,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        artist['name']?.toString() ?? 'Artist',
                        maxLines: 2,
                        textAlign: TextAlign.center,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 10,
                          fontWeight: FontWeight.w600,
                          height: 1.1,
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

List<Map<String, dynamic>> rowTracks(
  RecommendationFeedState feedState,
  Iterable<String> rowKinds,
) {
  final kinds = rowKinds.toSet();
  return feedState.rows
      .where((row) => kinds.contains(row.kind) || kinds.contains(row.id))
      .expand((row) => row.items)
      .where((item) => item.isNotEmpty)
      .map((item) => Map<String, dynamic>.from(item))
      .toList(growable: false);
}

List<Map<String, dynamic>> rowAlbums(
  RecommendationFeedState feedState,
  Iterable<String> rowKinds,
) {
  return rowTracks(feedState, rowKinds);
}

List<Map<String, dynamic>> rowArtists(
  RecommendationFeedState feedState,
  Iterable<String> rowKinds,
) {
  return rowTracks(feedState, rowKinds);
}
