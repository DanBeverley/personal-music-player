import 'package:flutter/material.dart';

import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';

class RecentSearchHistorySection extends StatelessWidget {
  const RecentSearchHistorySection({
    super.key,
    required this.queries,
    required this.isLoading,
    required this.onSelectQuery,
  });

  final List<String> queries;
  final bool isLoading;
  final ValueChanged<String> onSelectQuery;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Recent searches',
          style: TextStyle(
            color: Colors.white,
            fontSize: 20,
            fontWeight: FontWeight.w800,
            letterSpacing: -0.4,
          ),
        ),
        const SizedBox(height: 12),
        ...queries.map((query) {
          return NeatieSurface(
            margin: const EdgeInsets.only(bottom: 10),
            radius: neatieRadiusMedium,
            color: Colors.white.withValues(alpha: 0.035),
            blur: false,
            padding: EdgeInsets.zero,
            child: InkWell(
              borderRadius: BorderRadius.circular(neatieRadiusMedium),
              onTap: () => onSelectQuery(query),
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
        }),
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
