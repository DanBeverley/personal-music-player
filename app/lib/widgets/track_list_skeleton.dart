import 'package:flutter/material.dart';

class TrackListSkeleton extends StatefulWidget {
  final int count;
  final EdgeInsetsGeometry padding;
  final double radius;

  const TrackListSkeleton({
    super.key,
    this.count = 5,
    this.padding = EdgeInsets.zero,
    this.radius = 12,
  });

  @override
  State<TrackListSkeleton> createState() => _TrackListSkeletonState();
}

class _TrackListSkeletonState extends State<TrackListSkeleton>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1100),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        final alpha = 0.05 + (_controller.value * 0.08);
        final borderAlpha = 0.06 + (_controller.value * 0.04);
        return Padding(
          padding: widget.padding,
          child: Column(
            children: List<Widget>.generate(
              widget.count,
              (index) => _TrackListSkeletonTile(
                radius: widget.radius,
                fillAlpha: alpha + ((index % 2) * 0.01),
                borderAlpha: borderAlpha,
              ),
            ),
          ),
        );
      },
    );
  }
}

class _TrackListSkeletonTile extends StatelessWidget {
  final double radius;
  final double fillAlpha;
  final double borderAlpha;

  const _TrackListSkeletonTile({
    required this.radius,
    required this.fillAlpha,
    required this.borderAlpha,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 98,
      margin: const EdgeInsets.only(bottom: 14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: fillAlpha),
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(
          color: Colors.white.withValues(alpha: borderAlpha),
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            Container(
              width: 74,
              height: 74,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: fillAlpha + 0.05),
                borderRadius: BorderRadius.circular(radius - 2),
              ),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    height: 16,
                    width: double.infinity,
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: fillAlpha + 0.06),
                      borderRadius: BorderRadius.circular(6),
                    ),
                  ),
                  const SizedBox(height: 10),
                  FractionallySizedBox(
                    widthFactor: 0.58,
                    child: Container(
                      height: 12,
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: fillAlpha + 0.03),
                        borderRadius: BorderRadius.circular(6),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 14),
            Container(
              width: 38,
              height: 38,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: fillAlpha + 0.04),
                shape: BoxShape.circle,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
