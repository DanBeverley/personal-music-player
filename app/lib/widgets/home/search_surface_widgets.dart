import 'package:flutter/material.dart';

import '../../ui/app_theme_tokens.dart';

class SearchHeaderBar extends StatelessWidget {
  const SearchHeaderBar({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.showBackButton,
    required this.onBack,
    required this.onClear,
    required this.onSubmitted,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final bool showBackButton;
  final VoidCallback onBack;
  final VoidCallback onClear;
  final ValueChanged<String> onSubmitted;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        if (showBackButton)
          IconButton(
            onPressed: onBack,
            icon: const Icon(Icons.arrow_back_rounded),
            color: Colors.white,
          ),
        Expanded(
          child: Semantics(
            label: 'Search music',
            child: TextField(
              controller: controller,
              focusNode: focusNode,
              textInputAction: TextInputAction.search,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: 'Search tracks, albums, artists',
                hintStyle: TextStyle(
                  color: Colors.white.withValues(alpha: 0.34),
                ),
                filled: true,
                fillColor: Colors.white.withValues(alpha: 0.05),
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(
                  vertical: 12,
                  horizontal: 14,
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(appRadiusMedium),
                  borderSide: BorderSide.none,
                ),
                suffixIcon: controller.text.trim().isNotEmpty
                    ? IconButton(
                        icon: const Icon(
                          Icons.close_rounded,
                          color: Colors.white70,
                        ),
                        onPressed: onClear,
                      )
                    : null,
              ),
              onSubmitted: onSubmitted,
            ),
          ),
        ),
      ],
    );
  }
}

class SearchSuggestionPanel extends StatelessWidget {
  const SearchSuggestionPanel({
    super.key,
    required this.suggestions,
    required this.onSelectSuggestion,
  });

  final List<String> suggestions;
  final ValueChanged<String> onSelectSuggestion;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.035),
        borderRadius: BorderRadius.circular(appRadiusMedium),
        border: Border.all(
          color: Colors.white.withValues(alpha: 0.08),
        ),
      ),
      child: ListView.builder(
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        itemCount: suggestions.length,
        itemBuilder: (context, index) {
          final suggestion = suggestions[index];
          return ListTile(
            leading: const Icon(Icons.search, color: Colors.white54),
            title: Text(
              suggestion,
              style: const TextStyle(color: Colors.white),
            ),
            onTap: () => onSelectSuggestion(suggestion),
          );
        },
      ),
    );
  }
}
