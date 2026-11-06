import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../logic/charts_provider.dart';
import '../ui/app_theme_tokens.dart';

/// Shared native/web drawing of real provider rank history.
class RankHistoryPainter extends CustomPainter {
  RankHistoryPainter(this.history, this.progress);

  final List<ChartHistoryPoint> history;
  final double progress;

  @override
  void paint(Canvas canvas, Size size) {
    final grid = Paint()
      ..color = neatieHairline
      ..strokeWidth = 1;
    for (var index = 0; index < 4; index++) {
      final y = size.height * index / 3;
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }
    if (history.length < 2) return;
    final maxRank = math.max(
      2,
      history.fold<int>(1, (value, point) => math.max(value, point.rank)),
    );
    final points = <Offset>[];
    for (var pointIndex = 0; pointIndex < history.length; pointIndex++) {
      final x = size.width * pointIndex / (history.length - 1);
      final normalized =
          (history[pointIndex].rank - 1) / math.max(maxRank - 1, 1);
      final targetY = 4 + normalized * (size.height - 8);
      points.add(Offset(x, size.height + (targetY - size.height) * progress));
    }
    final path = Path()..moveTo(points.first.dx, points.first.dy);
    for (final point in points.skip(1)) {
      path.lineTo(point.dx, point.dy);
    }
    final fillPath = Path.from(path)
      ..lineTo(points.last.dx, size.height)
      ..lineTo(points.first.dx, size.height)
      ..close();
    canvas.drawPath(
      fillPath,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: <Color>[Color(0x557BDFF2), Color(0x057BDFF2)],
        ).createShader(Offset.zero & size),
    );
    canvas.drawPath(
      path,
      Paint()
        ..color = const Color(0xFF7BDFF2)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 3
        ..strokeCap = StrokeCap.round
        ..strokeJoin = StrokeJoin.round,
    );
    for (final point in points) {
      canvas.drawCircle(point, 3.5, Paint()..color = neatieActive);
      canvas.drawCircle(
        point,
        2,
        Paint()..color = const Color(0xFF7BDFF2),
      );
    }
  }

  @override
  bool shouldRepaint(covariant RankHistoryPainter oldDelegate) =>
      oldDelegate.progress != progress || oldDelegate.history != history;
}
