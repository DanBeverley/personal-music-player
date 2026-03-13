import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:palette_generator/palette_generator.dart';
import 'audio_provider.dart';

final dominantColorProvider = FutureProvider<Color>((ref) async {
  final playerState = ref.watch(audioPlayerProvider);
  if (playerState.thumbnail == null) {
    return const Color(0xFF1DB954); // Default Green
  }
  
  try {
    final imageProvider = NetworkImage(playerState.thumbnail!);
    final PaletteGenerator paletteGenerator = await PaletteGenerator.fromImageProvider(
      imageProvider,
      maximumColorCount: 20,
    );
    return paletteGenerator.dominantColor?.color ?? paletteGenerator.darkMutedColor?.color ?? const Color(0xFF1DB954);
  } catch (e) {
    return const Color(0xFF1DB954);
  }
});
