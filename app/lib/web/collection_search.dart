import 'package:flutter/material.dart';

final _apostrophes = RegExp(r"['’]");
final _separators = RegExp(r'[\s_-]+');

/// Search loaded collection metadata, preserving each original queue position.
List<int> matchingTrackIndices(
    List<Map<String, dynamic>> tracks, String query) {
  String normalize(String value) => value
      .toLowerCase()
      .replaceAll(_apostrophes, '')
      .replaceAll(_separators, ' ')
      .trim();
  final words =
      normalize(query).split(' ').where((word) => word.isNotEmpty).toList();
  return [
    for (var i = 0; i < tracks.length; i++)
      if (words.isEmpty ||
          (() {
            final track = tracks[i];
            final text = normalize([
              for (final key in [
                'title',
                'name',
                'artist',
                'channel',
                'author',
                'album',
                'album_title'
              ])
                if (track[key] != null) '${track[key]}',
              for (final artist in track['artists'] is List
                  ? track['artists'] as List
                  : const [])
                artist is Map ? '${artist['name'] ?? ''}' : '$artist',
            ].join(' '));
            final compact = text.replaceAll(' ', '');
            return words
                .every((word) => text.contains(word) || compact.contains(word));
          })())
        i
  ];
}

class CollectionSearchField extends StatelessWidget {
  const CollectionSearchField(
      {super.key,
      required this.controller,
      required this.label,
      required this.onChanged});
  final TextEditingController controller;
  final String label;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) => TextField(
        controller: controller,
        onChanged: onChanged,
        maxLength: 200,
        textInputAction: TextInputAction.search,
        decoration: InputDecoration(
          labelText: label,
          counterText: '',
          prefixIcon: const Icon(Icons.search),
          suffixIcon: controller.text.isEmpty
              ? null
              : IconButton(
                  tooltip: 'Clear collection search',
                  icon: const Icon(Icons.close),
                  onPressed: () {
                    controller.clear();
                    onChanged('');
                  }),
        ),
      );
}
