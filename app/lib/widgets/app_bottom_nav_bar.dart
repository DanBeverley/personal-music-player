import 'dart:ui';

import 'package:flutter/material.dart';

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
      child: ClipRRect(
        borderRadius: BorderRadius.circular(10),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 22, sigmaY: 22),
          child: Container(
            height: 58,
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(10),
              color: const Color(0xFF111111).withValues(alpha: 0.92),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.34),
                  blurRadius: 28,
                  offset: const Offset(0, 12),
                ),
              ],
            ),
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
              ],
            ),
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
    const activeColor = Color(0xFF7A8088);

    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: () => onSelected(index),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic,
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(8),
          gradient: isSelected
              ? LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [
                    activeColor.withValues(alpha: 0.20),
                    activeColor.withValues(alpha: 0.06),
                  ],
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
              width: 28,
              height: 28,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(6),
                color: isSelected
                    ? Colors.white.withValues(alpha: 0.10)
                    : Colors.transparent,
              ),
              child: Icon(
                isSelected ? selectedIcon : icon,
                color:
                    isSelected ? Colors.white : Colors.white.withValues(alpha: 0.62),
                size: 16,
              ),
            ),
            const SizedBox(height: 1),
            Text(
              label,
              style: TextStyle(
                color:
                    isSelected ? Colors.white : Colors.white.withValues(alpha: 0.52),
                fontSize: 9.5,
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
