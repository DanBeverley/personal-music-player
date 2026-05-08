import 'package:flutter/material.dart';

import '../../ui/app_theme_tokens.dart';

class AssistantLauncherCard extends StatelessWidget {
  const AssistantLauncherCard({
    super.key,
    required this.tooltip,
    required this.onTap,
  });

  final String tooltip;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(appRadiusLarge),
          onTap: onTap,
          child: Container(
            width: double.infinity,
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.03),
              borderRadius: BorderRadius.circular(appRadiusLarge),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: Row(
              children: [
                Container(
                  width: 42,
                  height: 42,
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.06),
                    borderRadius: BorderRadius.circular(appRadiusMedium),
                  ),
                  child: const Icon(
                    Icons.auto_awesome_rounded,
                    color: Colors.white,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Ask EBB',
                        style: TextStyle(
                          color: Colors.white,
                          fontSize: 15,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Describe a mood, artist, era, or playlist idea and get playable picks back.',
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.68),
                          fontSize: 12,
                          height: 1.4,
                        ),
                      ),
                    ],
                  ),
                ),
                Icon(
                  Icons.chevron_right_rounded,
                  color: Colors.white.withValues(alpha: 0.6),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class SearchContentSection extends StatelessWidget {
  const SearchContentSection({
    super.key,
    required this.songMatchLauncher,
    required this.showRecentSearchHistory,
    required this.recentSearchHistorySection,
    required this.featuredArtistCard,
    required this.artistMatchLoadingCard,
    required this.showSimilarArtists,
    required this.similarArtistsSection,
    required this.featuredAlbumSection,
    required this.relatedTracksSection,
    required this.trackListSection,
    required this.moreAlbumsSection,
  });

  final Widget songMatchLauncher;
  final bool showRecentSearchHistory;
  final Widget recentSearchHistorySection;
  final Widget? featuredArtistCard;
  final Widget? artistMatchLoadingCard;
  final bool showSimilarArtists;
  final Widget similarArtistsSection;
  final Widget? featuredAlbumSection;
  final Widget? relatedTracksSection;
  final Widget trackListSection;
  final Widget? moreAlbumsSection;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        songMatchLauncher,
        const SizedBox(height: 16),
        if (showRecentSearchHistory) ...[
          recentSearchHistorySection,
        ] else ...[
          const Text(
            'Search Results',
            style: TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
          const SizedBox(height: 16),
        ],
        if (featuredArtistCard != null) ...[
          featuredArtistCard!,
        ] else if (artistMatchLoadingCard != null) ...[
          artistMatchLoadingCard!,
        ],
        if (showSimilarArtists) ...[
          similarArtistsSection,
          const SizedBox(height: 8),
        ],
        if (featuredAlbumSection != null) ...[
          featuredAlbumSection!,
          const SizedBox(height: 10),
        ],
        if (relatedTracksSection != null) ...[
          relatedTracksSection!,
          const SizedBox(height: 18),
        ],
        trackListSection,
        if (moreAlbumsSection != null) ...[
          const SizedBox(height: 24),
          moreAlbumsSection!,
        ],
      ],
    );
  }
}

class HomeRecommendationsSection extends StatelessWidget {
  const HomeRecommendationsSection({
    super.key,
    this.lastPlayedLane,
    this.frequentlyListenedLane,
    required this.recommendationRows,
  });

  final Widget? lastPlayedLane;
  final Widget? frequentlyListenedLane;
  final Widget recommendationRows;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (lastPlayedLane != null) ...[
          lastPlayedLane!,
          const SizedBox(height: 22),
        ],
        if (frequentlyListenedLane != null) ...[
          frequentlyListenedLane!,
          const SizedBox(height: 22),
        ],
        recommendationRows,
      ],
    );
  }
}
