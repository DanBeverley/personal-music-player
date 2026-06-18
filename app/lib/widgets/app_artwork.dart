import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import '../ui/app_theme_tokens.dart';

String preferredArtworkUrl(String? thumbnail, {String? videoId}) {
  final cleanedThumbnail = thumbnail?.trim();
  if (cleanedThumbnail != null && cleanedThumbnail.isNotEmpty) {
    return cleanedThumbnail;
  }
  final cleanedVideoId = videoId?.trim();
  if (cleanedVideoId != null && cleanedVideoId.isNotEmpty) {
    return 'https://i.ytimg.com/vi/$cleanedVideoId/hqdefault.jpg';
  }
  return '';
}

int? _cacheDimension(double value, {double multiplier = 1}) {
  if (!value.isFinite || value <= 0) return null;
  final scaled = value * multiplier;
  if (!scaled.isFinite || scaled <= 0) return null;
  return scaled.round();
}

class AppArtwork extends StatelessWidget {
  final String? thumbnail;
  final String? videoId;
  final double width;
  final double height;
  final double radius;
  final BoxFit fit;
  final String? heroTag;

  const AppArtwork({
    super.key,
    required this.thumbnail,
    this.videoId,
    required this.width,
    required this.height,
    this.radius = 16,
    this.fit = BoxFit.cover,
    this.heroTag,
  });

  @override
  Widget build(BuildContext context) {
    final image = _ArtworkFrame(
      thumbnail: thumbnail,
      videoId: videoId,
      width: width,
      height: height,
      radius: radius,
      fit: fit,
    );

    if (heroTag == null) return image;
    return Hero(tag: heroTag!, child: image);
  }
}

class _ArtworkFrame extends StatelessWidget {
  final String? thumbnail;
  final String? videoId;
  final double width;
  final double height;
  final double radius;
  final BoxFit fit;

  const _ArtworkFrame({
    required this.thumbnail,
    required this.videoId,
    required this.width,
    required this.height,
    required this.radius,
    required this.fit,
  });

  @override
  Widget build(BuildContext context) {
    final imageUrl = preferredArtworkUrl(thumbnail, videoId: videoId);
    final borderRadius = BorderRadius.circular(radius);

    return ClipRRect(
      borderRadius: borderRadius,
      child: Container(
        width: width,
        height: height,
        decoration: BoxDecoration(
          borderRadius: borderRadius,
          color: const Color(0xFF161616),
          gradient: const LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFF1A1A1A), Color(0xFF0D0D0D)],
          ),
        ),
        child: Stack(
          fit: StackFit.expand,
          children: [
            imageUrl.isEmpty
                ? const _ArtworkFallback()
                : CachedNetworkImage(
                    imageUrl: imageUrl,
                    fit: fit,
                    memCacheWidth: _cacheDimension(width, multiplier: 2.2),
                    memCacheHeight: _cacheDimension(height, multiplier: 2.2),
                    maxWidthDiskCache: _cacheDimension(width, multiplier: 3),
                    maxHeightDiskCache: _cacheDimension(height, multiplier: 3),
                    fadeInDuration: const Duration(milliseconds: 120),
                    useOldImageOnUrlChange: true,
                    filterQuality: FilterQuality.low,
                    placeholder: (context, url) => const _ArtworkFallback(),
                    errorWidget: (context, url, error) =>
                        const _ArtworkFallback(),
                  ),
            DecoratedBox(
              decoration: BoxDecoration(
                borderRadius: borderRadius,
                border: Border.all(color: neatieHairline),
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [
                    Colors.white.withValues(alpha: 0.04),
                    Colors.black.withValues(alpha: 0.10),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ArtworkFallback extends StatelessWidget {
  const _ArtworkFallback();

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF202020), Color(0xFF0E0E0E)],
        ),
      ),
      child: Center(
        child: Icon(
          Icons.graphic_eq_rounded,
          color: Colors.white.withValues(alpha: 0.24),
          size: 36,
        ),
      ),
    );
  }
}
