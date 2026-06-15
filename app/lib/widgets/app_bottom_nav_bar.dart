import 'package:flutter/material.dart';

import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';

class AppBottomNavBar extends StatelessWidget {
  final int currentIndex;
  final ValueChanged<int> onSelected;

  const AppBottomNavBar({
    super.key,
    required this.currentIndex,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    final mediaQuery = MediaQuery.of(context);
    final bottomInset = mediaQuery.padding.bottom > mediaQuery.viewPadding.bottom
        ? mediaQuery.padding.bottom
        : mediaQuery.viewPadding.bottom;
    return Padding(
      padding: EdgeInsets.fromLTRB(
        18,
        0,
        18,
        bottomInset > 0 ? bottomInset : 6,
      ),
      child: NeatieSurface(
        radius: neatieRadiusMedium,
        color: neatieGlass,
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 4),
        child: SizedBox(
          height: 54,
          child: Row(
            children: [
              Expanded(
                child: _NavButton(
                  index: 0,
                  label: 'Home',
                  selectedIcon: Icons.home_rounded,
                  icon: Icons.home_outlined,
                  currentIndex: currentIndex,
                  onSelected: onSelected,
                ),
              ),
              Expanded(
                child: _NavButton(
                  index: 1,
                  label: 'Search',
                  selectedIcon: Icons.search_rounded,
                  icon: Icons.search_rounded,
                  currentIndex: currentIndex,
                  onSelected: onSelected,
                ),
              ),
              Expanded(
                child: _NavButton(
                  index: 2,
                  label: 'Library',
                  selectedIcon: Icons.library_music_rounded,
                  icon: Icons.library_music_outlined,
                  currentIndex: currentIndex,
                  onSelected: onSelected,
                ),
              ),
              Expanded(
                child: _NavButton(
                  index: 3,
                  label: 'Profile',
                  selectedIcon: Icons.person_rounded,
                  icon: Icons.person_outline_rounded,
                  currentIndex: currentIndex,
                  onSelected: onSelected,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _NavButton extends StatelessWidget {
  final int index;
  final int currentIndex;
  final String label;
  final IconData icon;
  final IconData selectedIcon;
  final ValueChanged<int> onSelected;

  const _NavButton({
    required this.index,
    required this.currentIndex,
    required this.label,
    required this.icon,
    required this.selectedIcon,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    final isSelected = currentIndex == index;

    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: () => onSelected(index),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic,
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(neatieRadiusSmall),
          color: isSelected
              ? Colors.white.withValues(alpha: 0.10)
              : Colors.transparent,
          border: isSelected
              ? Border.all(
                  color: neatieHairline,
                )
              : null,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            AnimatedContainer(
              duration: const Duration(milliseconds: 220),
              curve: Curves.easeOutCubic,
              width: 24,
              height: 24,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(5),
                color: isSelected
                    ? Colors.white.withValues(alpha: 0.14)
                    : Colors.transparent,
              ),
              child: Icon(
                isSelected ? selectedIcon : icon,
                color: isSelected ? neatieActive : neatieInactive,
                size: 14,
              ),
            ),
            const SizedBox(height: 1),
            Text(
              label,
              textScaler: TextScaler.noScaling,
              style: TextStyle(
                color: isSelected ? neatieActive : neatieDimText,
                fontSize: 8.5,
                fontWeight: isSelected ? FontWeight.w700 : FontWeight.w500,
                letterSpacing: 0.2,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
