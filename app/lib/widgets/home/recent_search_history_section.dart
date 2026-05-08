import 'package:flutter/material.dart';

import '../../ui/app_theme_tokens.dart';

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
        Text(
          'Recent searches',
          style: TextStyle(
            color: Colors.white.withValues(alpha: 0.78),
            fontSize: 14,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.4,
          ),
        ),
        const SizedBox(height: 12),
        ...queries.map((query) {
          return Container(
            margin: const EdgeInsets.only(bottom: 10),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.03),
              borderRadius: BorderRadius.circular(appRadiusMedium),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: ListTile(
              leading: const Icon(
                Icons.history_rounded,
                color: Colors.white70,
              ),
              title: Text(
                query,
                style: const TextStyle(color: Colors.white),
              ),
              trailing: Icon(
                Icons.north_west_rounded,
                color: Colors.white.withValues(alpha: 0.46),
              ),
              onTap: () => onSelectQuery(query),
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
