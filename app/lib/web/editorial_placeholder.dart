import 'package:flutter/material.dart';
import 'design_system.dart';

/// An explicit missing-cover treatment; it never substitutes another entity's art.
class EditorialPlaceholder extends StatelessWidget {
  const EditorialPlaceholder(
      {super.key, required this.label, required this.artist});
  final String label;
  final bool artist;
  @override
  Widget build(BuildContext context) {
    final tones = [
      const Color(0xff344b42),
      const Color(0xff3f414c),
      const Color(0xff4b3d32),
      const Color(0xff32434d)
    ];
    final seed = label.runes.fold<int>(0, (a, b) => a + b);
    return DecoratedBox(
        decoration: BoxDecoration(
            gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [tones[seed % tones.length], NeatieDesign.surface])),
        child: Stack(alignment: Alignment.center, children: [
          Positioned(
              right: -18,
              bottom: -18,
              child: Icon(Icons.album_outlined,
                  size: 130, color: Colors.white.withValues(alpha: .05))),
          Icon(artist ? Icons.person_outline_rounded : Icons.music_note_rounded,
              color: NeatieDesign.ink.withValues(alpha: .55), size: 32),
        ]));
  }
}
