import 'dart:async';

import 'package:flutter/material.dart';

import '../../logic/recommendation_feed_models.dart';
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
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
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

class _SquareTrackCard extends StatelessWidget {
  const _SquareTrackCard({
    required this.track,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  @override
  Widget build(BuildContext context) {
    final title = track['title']?.toString().trim();
    final artist = (track['channel'] ?? track['artist'] ?? track['author'])
            ?.toString()
            .trim() ??
        '';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        AspectRatio(
          aspectRatio: 1,
          child: InkWell(
            onTap: onPlay,
            borderRadius: BorderRadius.circular(11),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(11),
              child: AppArtwork(
                thumbnail: track['thumbnail'],
                videoId: (track['id'] ?? track['videoId'])?.toString(),
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
                artist,
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
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: 'Made for you', onViewAll: onViewAll),
        SizedBox(
          height: 88,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: items.length,
            separatorBuilder: (_, __) => const SizedBox(width: 12),
            itemBuilder: (context, index) {
              final item = items[index];
              return NeatieSurface(
                width: 188,
                radius: 14,
                color: Colors.white.withValues(alpha: 0.025),
                blur: false,
                padding: EdgeInsets.zero,
                child: InkWell(
                  onTap: () => onPlay(item),
                  borderRadius: BorderRadius.circular(14),
                  child: Row(
                    children: [
                      Container(
                        width: 68,
                        height: double.infinity,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.04),
                          borderRadius: const BorderRadius.horizontal(
                            left: Radius.circular(14),
                          ),
                        ),
                        child: item.thumbnail != null
                            ? AppArtwork(
                                thumbnail: item.thumbnail,
                                videoId: item.videoId,
                                width: 68,
                                height: 88,
                                radius: 14,
                              )
                            : const Icon(
                                Icons.graphic_eq_rounded,
                                color: Colors.white,
                                size: 34,
                              ),
                      ),
                      Expanded(
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              Text(
                                item.title,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 13.5,
                                  fontWeight: FontWeight.w800,
                                  height: 1.1,
                                ),
                              ),
                              const SizedBox(height: 6),
                              Text(
                                item.subtitle,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: neatieMutedText,
                                  fontSize: 10.8,
                                  height: 1.25,
                                ),
                              ),
                            ],
                          ),
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

class NeatieTrendingCompactRow extends StatelessWidget {
  const NeatieTrendingCompactRow({
    super.key,
    this.title = 'Trending now',
    required this.tracks,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    final visible = tracks.take(10).toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title),
        Wrap(
          runSpacing: 13,
          children: [
            for (var index = 0; index < visible.length; index++)
              SizedBox(
                width: MediaQuery.of(context).size.width >= 700
                    ? (MediaQuery.of(context).size.width - 64) / 2
                    : double.infinity,
                child: _RankedTrackTile(
                  rank: index + 1,
                  track: visible[index],
                  onPlay: () => onPlay(visible[index]),
                  onMenuDetails: () => onMenuDetails(visible[index]),
                  onAddToPlaylist: onAddToPlaylist,
                  onStartStation: onStartStation,
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class NeatieGenreTabsRow extends StatelessWidget {
  const NeatieGenreTabsRow({
    super.key,
    required this.title,
    required this.tabs,
    required this.selectedTabId,
    required this.onSelectedTab,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> tabs;
  final String selectedTabId;
  final ValueChanged<String> onSelectedTab;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
  final VoidCallback? onViewAll;

  List<Map<String, dynamic>> _tabTracks(Map<String, dynamic> tab) {
    return (tab['tracks'] as List<dynamic>? ?? const [])
        .whereType<Map>()
        .map((track) => Map<String, dynamic>.from(track))
        .toList(growable: false);
  }

  @override
  Widget build(BuildContext context) {
    if (tabs.isEmpty) return const SizedBox.shrink();
    final activeTab = tabs.firstWhere(
      (tab) => tab['id']?.toString() == selectedTabId,
      orElse: () => tabs.first,
    );
    final tracks = _tabTracks(activeTab).take(12).toList(growable: false);
    if (tracks.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title, onViewAll: onViewAll),
        SizedBox(
          height: 42,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: tabs.length,
            separatorBuilder: (_, __) => const SizedBox(width: 9),
            itemBuilder: (context, index) {
              final tab = tabs[index];
              final id = tab['id']?.toString() ?? '';
              final label = tab['label']?.toString().trim();
              final displayLabel =
                  label != null && label.isNotEmpty ? label : id;
              return NeatiePill(
                label: displayLabel,
                selected: id == activeTab['id']?.toString(),
                onTap: id.isEmpty ? null : () => onSelectedTab(id),
              );
            },
          ),
        ),
        const SizedBox(height: 8),
        Wrap(
          runSpacing: 13,
          children: [
            for (var index = 0; index < tracks.length; index++)
              SizedBox(
                width: MediaQuery.of(context).size.width >= 700
                    ? (MediaQuery.of(context).size.width - 64) / 2
                    : double.infinity,
                child: _RankedTrackTile(
                  rank: index + 1,
                  track: tracks[index],
                  onPlay: () => onPlay(tracks[index]),
                  onMenuDetails: () => onMenuDetails(tracks[index]),
                  onAddToPlaylist: onAddToPlaylist,
                  onStartStation: onStartStation,
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class _RankedTrackTile extends StatelessWidget {
  const _RankedTrackTile({
    required this.rank,
    required this.track,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
  });

  final int rank;
  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;

  @override
  Widget build(BuildContext context) {
    final artist = (track['channel'] ?? track['artist'] ?? track['author'])
            ?.toString()
            .trim() ??
        '';
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
          borderRadius: BorderRadius.circular(7),
          child: AppArtwork(
            thumbnail: track['thumbnail'],
            videoId: (track['id'] ?? track['videoId'])?.toString(),
            width: 48,
            height: 48,
            radius: 7,
          ),
        ),
        const SizedBox(width: 11),
        Expanded(
          child: InkWell(
            onTap: onPlay,
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
                  artist,
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

class NeatieLongTrackList extends StatelessWidget {
  const NeatieLongTrackList({
    super.key,
    required this.title,
    required this.tracks,
    required this.onPlay,
    required this.onMenuDetails,
    required this.onAddToPlaylist,
    required this.onStartStation,
    this.maxItems = 48,
    this.onViewAll,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onMenuDetails;
  final TrackActionCallback onAddToPlaylist;
  final TrackActionCallback onStartStation;
  final int maxItems;
  final VoidCallback? onViewAll;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    final visible = tracks.take(maxItems).toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        NeatieSectionTitle(title: title, onViewAll: onViewAll),
        Wrap(
          runSpacing: 13,
          children: [
            for (var index = 0; index < visible.length; index++)
              SizedBox(
                width: MediaQuery.of(context).size.width >= 700
                    ? (MediaQuery.of(context).size.width - 64) / 2
                    : double.infinity,
                child: _RankedTrackTile(
                  rank: index + 1,
                  track: visible[index],
                  onPlay: () => onPlay(visible[index]),
                  onMenuDetails: () => onMenuDetails(visible[index]),
                  onAddToPlaylist: onAddToPlaylist,
                  onStartStation: onStartStation,
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
          height: 214,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: albums.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final album = albums[index];
              final artist =
                  album['artist']?.toString().trim() ?? 'Unknown artist';
              final metadata = _albumMetadata(album);
              final reason = _albumRecommendationReason(album);
              return NeatieSurface(
                width: 244,
                radius: 18,
                color: Colors.transparent,
                blur: false,
                padding: EdgeInsets.zero,
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    Opacity(
                      opacity: 0.24,
                      child: AppArtwork(
                        thumbnail: album['thumbnail'],
                        width: 244,
                        height: 214,
                        radius: 18,
                        fit: BoxFit.cover,
                      ),
                    ),
                    DecoratedBox(
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(18),
                        gradient: LinearGradient(
                          begin: Alignment.topCenter,
                          end: Alignment.bottomCenter,
                          colors: [
                            Colors.transparent,
                            Colors.black.withValues(alpha: 0.90),
                          ],
                          stops: const [0.32, 0.76],
                        ),
                      ),
                    ),
                    InkWell(
                      onTap: () => onOpen(album),
                      borderRadius: BorderRadius.circular(18),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                      Stack(
                        children: [
                          AppArtwork(
                            thumbnail: album['thumbnail'],
                            width: 244,
                            height: 126,
                            radius: 18,
                          ),
                          Positioned.fill(
                            child: Container(
                              decoration: BoxDecoration(
                                borderRadius: const BorderRadius.vertical(
                                  top: Radius.circular(18),
                                ),
                                gradient: LinearGradient(
                                  begin: Alignment.topCenter,
                                  end: Alignment.bottomCenter,
                                  colors: [
                                    Colors.transparent,
                                    Colors.black.withValues(alpha: 0.58),
                                  ],
                                ),
                              ),
                            ),
                          ),
                          Positioned(
                            left: 12,
                            bottom: 10,
                            child: Text(
                              metadata.isEmpty
                                  ? 'EDITORIAL ALBUM PICK'
                                  : metadata.toUpperCase(),
                              style: const TextStyle(
                                color: Colors.white70,
                                fontSize: 9.5,
                                fontWeight: FontWeight.w800,
                                letterSpacing: 1.2,
                              ),
                            ),
                          ),
                        ],
                      ),
                      Expanded(
                        child: Padding(
                          padding: const EdgeInsets.fromLTRB(13, 10, 13, 11),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                album['title']?.toString() ?? 'Unknown Album',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 14,
                                  fontWeight: FontWeight.w800,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                artist.isEmpty ? 'Unknown artist' : artist,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: neatieMutedText,
                                  fontSize: 11.5,
                                ),
                              ),
                              if (reason.isNotEmpty) ...[
                                const Spacer(),
                                Row(
                                  children: [
                                    const Icon(
                                      Icons.auto_awesome_rounded,
                                      color: Colors.white54,
                                      size: 12,
                                    ),
                                    const SizedBox(width: 5),
                                    Expanded(
                                      child: Text(
                                        reason,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(
                                          color: Colors.white60,
                                          fontSize: 10.5,
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ],
                            ],
                          ),
                        ),
                      ),
                        ],
                      ),
                    ),
                  ],
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

String _albumMetadata(Map<String, dynamic> album) {
  final type = (album['album_type'] ?? album['type'] ?? album['kind'])
          ?.toString()
          .trim() ??
      '';
  final year = (album['year'] ?? album['release_date'])?.toString().trim() ?? '';
  final cleanYear = year.length >= 4 ? year.substring(0, 4) : year;
  return [
    if (type.isNotEmpty && type.toLowerCase() != 'album') type,
    if (cleanYear.isNotEmpty) cleanYear,
  ].join('  /  ');
}

String _albumRecommendationReason(Map<String, dynamic> album) {
  for (final key in const [
    'recommendation_reason',
    'reason',
    'recommendationReason',
    'subtitle',
  ]) {
    final value = album[key]?.toString().trim() ?? '';
    if (value.isNotEmpty) return value;
  }
  return '';
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
                  borderRadius: BorderRadius.circular(999),
                  child: Column(
                    children: [
                      AppArtwork(
                        thumbnail: artist['thumbnail'],
                        width: 56,
                        height: 56,
                        radius: 999,
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
