import 'dart:ui';

import 'package:flutter/material.dart';

import 'app_theme_tokens.dart';

class NeatieBackground extends StatelessWidget {
  final Widget child;

  const NeatieBackground({
    super.key,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: neatieInk,
        gradient: RadialGradient(
          center: Alignment(-0.72, -0.92),
          radius: 1.35,
          colors: [
            Color(0x221D1D1D),
            Color(0x00030303),
          ],
        ),
      ),
      child: Material(
        color: Colors.transparent,
        child: Stack(
          fit: StackFit.expand,
          children: [
            Positioned(
              top: -120,
              right: -90,
              width: 260,
              height: 260,
              child: Container(
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: Colors.white.withValues(alpha: 0.025),
                ),
              ),
            ),
            Positioned(
              left: -120,
              bottom: 70,
              width: 230,
              height: 230,
              child: Container(
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: Colors.white.withValues(alpha: 0.018),
                ),
              ),
            ),
            child,
          ],
        ),
      ),
    );
  }
}

class NeatieSurface extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry? padding;
  final EdgeInsetsGeometry? margin;
  final BoxConstraints? constraints;
  final double? width;
  final double? height;
  final double radius;
  final Color color;
  final bool blur;
  final bool bordered;
  final bool elevated;

  const NeatieSurface({
    super.key,
    required this.child,
    this.padding,
    this.margin,
    this.constraints,
    this.width,
    this.height,
    this.radius = neatieRadiusLarge,
    this.color = neatieGlass,
    this.blur = true,
    this.bordered = true,
    this.elevated = true,
  });

  @override
  Widget build(BuildContext context) {
    final content = Container(
      width: width,
      height: height,
      constraints: constraints,
      padding: padding,
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(radius),
        border: bordered ? Border.all(color: neatieStroke) : null,
        boxShadow: elevated
            ? [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.34),
                  blurRadius: 34,
                  offset: const Offset(0, 18),
                ),
              ]
            : const [],
      ),
      child: Material(
        color: Colors.transparent,
        child: child,
      ),
    );

    final surface = !blur
        ? content
        : ClipRRect(
            borderRadius: BorderRadius.circular(radius),
            child: BackdropFilter(
              filter: ImageFilter.blur(sigmaX: neatieBlur, sigmaY: neatieBlur),
              child: content,
            ),
          );
    if (margin == null) return surface;
    return Padding(padding: margin!, child: surface);
  }
}

class NeatieSectionHeader extends StatelessWidget {
  final String title;
  final VoidCallback? onViewAll;

  const NeatieSectionHeader({
    super.key,
    required this.title,
    this.onViewAll,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(
            title,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 22,
              fontWeight: FontWeight.w800,
              letterSpacing: -0.6,
            ),
          ),
        ),
        if (onViewAll != null)
          TextButton(
            onPressed: onViewAll,
            style: TextButton.styleFrom(
              foregroundColor: neatieMutedText,
              visualDensity: VisualDensity.compact,
            ),
            child: const Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('View all'),
                SizedBox(width: 2),
                Icon(Icons.chevron_right_rounded, size: 20),
              ],
            ),
          ),
      ],
    );
  }
}

class NeatiePill extends StatelessWidget {
  final String label;
  final bool selected;
  final IconData? icon;
  final VoidCallback? onTap;

  const NeatiePill({
    super.key,
    required this.label,
    this.selected = false,
    this.icon,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 8),
        decoration: BoxDecoration(
          color: selected ? neatieActive : Colors.white.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(
            color: selected ? neatieActive : neatieStroke,
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(
                icon,
                size: 12,
                color: selected ? Colors.black : neatieMutedText,
              ),
              const SizedBox(width: 5),
            ],
            Text(
              label,
              style: TextStyle(
                color: selected ? Colors.black : neatieMutedText,
                fontWeight: FontWeight.w700,
                fontSize: 12,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class NeatieSheetHandle extends StatelessWidget {
  const NeatieSheetHandle({super.key});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        margin: const EdgeInsets.only(top: 10, bottom: 10),
        width: 48,
        height: 4,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.24),
          borderRadius: BorderRadius.circular(999),
        ),
      ),
    );
  }
}

class NeatieBottomSheetSurface extends StatelessWidget {
  final Widget child;

  const NeatieBottomSheetSurface({
    super.key,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        child: NeatieSurface(
          radius: 28,
          color: neatieRaised,
          padding: EdgeInsets.zero,
          child: child,
        ),
      ),
    );
  }
}

class NeatieIconTile extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback? onTap;
  final Widget? trailing;

  const NeatieIconTile({
    super.key,
    required this.icon,
    required this.title,
    required this.subtitle,
    this.onTap,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.06),
                borderRadius: BorderRadius.circular(neatieRadiusMedium),
                border: Border.all(color: neatieHairline),
              ),
              child: Icon(icon, color: Colors.white),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      color: Colors.white,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      color: neatieMutedText,
                      fontSize: 12,
                      height: 1.35,
                    ),
                  ),
                ],
              ),
            ),
            trailing ??
                const Icon(
                  Icons.chevron_right_rounded,
                  color: neatieMutedText,
                ),
          ],
        ),
      ),
    );
  }
}
