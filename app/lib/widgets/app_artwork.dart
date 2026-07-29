import 'dart:ui';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';

import '../logic/proxy_runtime.dart';
import '../ui/app_theme_tokens.dart';

final CacheManager _artworkCacheManager = CacheManager(
  Config(
    'neatieArtworkCacheV2',
    stalePeriod: const Duration(days: 90),
    maxNrOfCacheObjects: 1500,
  ),
);

String preferredArtworkUrl(String? thumbnail, {String? videoId}) {
  final cleanedThumbnail = thumbnail?.trim();
  if (cleanedThumbnail != null && cleanedThumbnail.isNotEmpty) {
    if (cleanedThumbnail.startsWith('/')) {
      return buildProxyUri(cleanedThumbnail).toString();
    }
    return cleanedThumbnail;
  }
  final cleanedVideoId = videoId?.trim();
  if (cleanedVideoId != null &&
      RegExp(r'^[A-Za-z0-9_-]{11}$').hasMatch(cleanedVideoId)) {
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

class ArtistArtwork extends StatelessWidget {
  const ArtistArtwork({
    super.key,
    required this.thumbnail,
    required this.width,
    required this.height,
    this.heroTag,
  });

  final String? thumbnail;
  final double width;
  final double height;
  final String? heroTag;

  @override
  Widget build(BuildContext context) {
    return AppArtwork(
      thumbnail: thumbnail,
      width: width,
      height: height,
      radius: 0,
      fit: BoxFit.contain,
      heroTag: heroTag,
      expandForeground: false,
      showBorder: false,
    );
  }
}

class AppArtwork extends StatelessWidget {
  final String? thumbnail;
  final String? videoId;
  final double width;
  final double height;
  final double radius;
  final BoxFit fit;
  final String? heroTag;
  final bool expandForeground;
  final bool showBorder;

  const AppArtwork({
    super.key,
    required this.thumbnail,
    this.videoId,
    required this.width,
    required this.height,
    this.radius = 16,
    this.fit = BoxFit.cover,
    this.heroTag,
    this.expandForeground = true,
    this.showBorder = true,
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
      expandForeground: expandForeground,
      showBorder: showBorder,
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
  final bool expandForeground;
  final bool showBorder;

  const _ArtworkFrame({
    required this.thumbnail,
    required this.videoId,
    required this.width,
    required this.height,
    required this.radius,
    required this.fit,
    required this.expandForeground,
    required this.showBorder,
  });

  @override
  Widget build(BuildContext context) {
    final imageUrl = preferredArtworkUrl(thumbnail, videoId: videoId);
    final borderRadius = BorderRadius.circular(radius);
    final isSquare = (width - height).abs() < 1;
    final foregroundScale = expandForeground ? (isSquare ? 1.16 : 1.08) : 1.0;

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
                : expandForeground
                    ? ImageFiltered(
                        imageFilter: ImageFilter.blur(sigmaX: 12, sigmaY: 12),
                        child: Transform.scale(
                          scale: 1.28,
                          child: _CachedArtworkImage(
                            imageUrl: imageUrl,
                            width: width,
                            height: height,
                            fit: BoxFit.cover,
                            opacity: 0.58,
                          ),
                        ),
                      )
                    : const SizedBox.shrink(),
            if (imageUrl.isNotEmpty)
              Transform.scale(
                scale: foregroundScale,
                child: _CachedArtworkImage(
                  imageUrl: imageUrl,
                  width: width,
                  height: height,
                  fit: fit,
                ),
              ),
            if (imageUrl.isNotEmpty)
              DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [
                      Colors.black.withValues(alpha: 0.02),
                      Colors.black.withValues(alpha: 0.08),
                    ],
                  ),
                ),
              ),
            DecoratedBox(
              decoration: BoxDecoration(
                borderRadius: borderRadius,
                border: showBorder ? Border.all(color: neatieHairline) : null,
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

class _CachedArtworkImage extends StatelessWidget {
  const _CachedArtworkImage({
    required this.imageUrl,
    required this.width,
    required this.height,
    required this.fit,
    this.opacity = 1,
  });

  final String imageUrl;
  final double width;
  final double height;
  final BoxFit fit;
  final double opacity;

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: opacity,
      child: CachedNetworkImage(
        cacheManager: _artworkCacheManager,
        cacheKey: imageUrl,
        imageUrl: imageUrl,
        fit: fit,
        memCacheWidth: _cacheDimension(width, multiplier: 2.2),
        memCacheHeight: _cacheDimension(height, multiplier: 2.2),
        fadeInDuration: const Duration(milliseconds: 120),
        useOldImageOnUrlChange: true,
        filterQuality: FilterQuality.low,
        placeholder: (context, url) => const _ArtworkFallback(),
        errorWidget: (context, url, error) => const _ArtworkFallback(),
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
