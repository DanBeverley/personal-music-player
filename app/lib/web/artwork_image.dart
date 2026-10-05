import 'package:flutter/material.dart';

/// Tries supplied artwork alternatives before showing the empty-image icon.
class ArtworkImage extends StatelessWidget {
  const ArtworkImage(
      {super.key,
      required this.urls,
      required this.width,
      required this.height,
      this.icon = Icons.music_note,
      this.placeholder});

  final List<String> urls;
  final double width, height;
  final IconData icon;
  final Widget? placeholder;

  Widget _image(int index) {
    if (index >= urls.length) {
      return SizedBox(
          width: width,
          height: height,
          child: placeholder ?? Center(child: Icon(icon)));
    }
    return Image.network(urls[index],
        key: ValueKey(urls[index]),
        width: width,
        height: height,
        fit: BoxFit.cover,
        errorBuilder: (_, __, ___) => _image(index + 1));
  }

  @override
  Widget build(BuildContext context) => _image(0);
}
