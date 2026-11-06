import 'package:flutter/material.dart';

import '../../logic/cloud_search_queries.dart';
import '../../logic/track_metadata.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';

class RecentSearchHistorySection extends StatelessWidget {
  const RecentSearchHistorySection({
    super.key,
    required this.queries,
    required this.picks,
    required this.isLoading,
    required this.onSelectQuery,
    required this.onPlayPick,
    required this.onRemovePick,
  });

  final List<String> queries;
  final List<RecentSearchPick> picks;
  final bool isLoading;
  final ValueChanged<String> onSelectQuery;
  final ValueChanged<RecentSearchPick> onPlayPick;
  final ValueChanged<RecentSearchPick> onRemovePick;

  @override
  Widget build(BuildContext context) {
    final entries = <({DateTime at, Widget child})>[
      for (final pick in picks)
        (
          at: pick.selectedAt,
          child: _RecentSearchPickTile(
            pick: pick,
            onTap: () => onPlayPick(pick),
            onRemove: () => onRemovePick(pick),
          ),
        ),
      for (final query in queries.where((query) => !picks.any(
            (pick) => pick.query.toLowerCase() == query.toLowerCase(),
          )))
        (
          at: recentCloudSearchQueryTimestamp(query),
          child: _RecentQueryTile(
            query: query,
            onTap: () => onSelectQuery(query),
          ),
        ),
    ]..sort((a, b) => b.at.compareTo(a.at));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Recently searched',
          style: TextStyle(
            color: Colors.white,
            fontSize: 20,
            fontWeight: FontWeight.w800,
            letterSpacing: -0.4,
          ),
        ),
        const SizedBox(height: 12),
        ...entries.map((entry) => entry.child),
        if (isLoading) ...[
          const SizedBox(height: 6),
          const Center(
            child: SizedBox(
              width: 20,
              height: 20,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
          ),
        ],
      ],
    );
  }
}

class _RecentQueryTile extends StatelessWidget {
  const _RecentQueryTile({required this.query, required this.onTap});

  final String query;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
            margin: const EdgeInsets.only(bottom: 10),
            radius: neatieRadiusMedium,
            color: Colors.white.withValues(alpha: 0.035),
            blur: false,
            padding: EdgeInsets.zero,
            child: InkWell(
              borderRadius: BorderRadius.circular(neatieRadiusMedium),
              onTap: onTap,
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 14,
                ),
                child: Row(
                  children: [
                    const Icon(Icons.history_rounded, color: neatieMutedText),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Text(
                        query,
                        style: const TextStyle(color: Colors.white),
                      ),
                    ),
                    const Icon(
                      Icons.north_west_rounded,
                      color: neatieDimText,
                    ),
                  ],
                ),
              ),
            ),
          );
  }
}

class _RecentSearchPickTile extends StatelessWidget {
  const _RecentSearchPickTile({
    required this.pick,
    required this.onTap,
    required this.onRemove,
  });

  final RecentSearchPick pick;
  final VoidCallback onTap;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final track = pick.track;
    final trackId = extractTrackId(track);
    return NeatieSurface(
      margin: const EdgeInsets.only(bottom: 12),
      radius: neatieRadiusMedium,
      color: Colors.transparent,
      blur: false,
      padding: EdgeInsets.zero,
      child: InkWell(
        borderRadius: BorderRadius.circular(neatieRadiusMedium),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
          child: Row(
            children: [
              AppArtwork(
                thumbnail: track['thumbnail']?.toString(),
                videoId: trackId,
                width: 58,
                height: 58,
                radius: 11,
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      pick.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 15,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      pick.artist.isNotEmpty ? pick.artist : pick.query,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: neatieMutedText,
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ],
                ),
              ),
              IconButton(
                onPressed: onRemove,
                icon: const Icon(Icons.close_rounded),
                color: Colors.white,
                tooltip: 'Remove recent search',
              ),
            ],
          ),
        ),
      ),
    );
  }
}
