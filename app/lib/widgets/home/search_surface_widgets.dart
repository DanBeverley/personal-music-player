import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider_queue.dart';
import '../../logic/suggest_provider.dart';
import '../../logic/track_metadata.dart';
import '../../navigation/player_navigation.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';
import '../playlist/add_to_playlist_dialog.dart';
import 'neatie_home_sections.dart';
import 'track_menu_button.dart';

String _searchArtistIdentityKey(Map<String, dynamic> artist) {
  final canonical =
      (artist['canonical_artist_id'] ?? artist['canonical_artist_key'] ?? '')
          .toString()
          .trim()
          .toLowerCase();
  final musicBrainz = (artist['musicbrainz_artist_id'] ??
          artist['artist_mbid'] ??
          artist['mb_artist_id'] ??
          '')
      .toString()
      .trim()
      .toLowerCase();
  if (musicBrainz.isNotEmpty) return 'mbid:$musicBrainz';
  if (canonical.startsWith('musicbrainz:artist:')) {
    return 'mbid:${canonical.substring('musicbrainz:artist:'.length)}';
  }
  final provider = (artist['provider_artist_id'] ??
          artist['browseId'] ??
          artist['artist_id'] ??
          artist['id'] ??
          '')
      .toString()
      .trim()
      .toLowerCase();
  if (provider.isNotEmpty &&
      !provider.startsWith('musicbrainz:artist:') &&
      !provider.startsWith('artist-name:') &&
      !provider.startsWith('derived:')) {
    return 'provider:$provider';
  }
  final name = (artist['normalized_name'] ?? artist['name'] ?? '')
      .toString()
      .trim()
      .toLowerCase();
  return 'name:$name';
}

String _searchAlbumIdentityKey(Map<String, dynamic> album) {
  return (album['canonical_album_identity'] ??
          album['id'] ??
          '${album['title'] ?? album['name'] ?? ''}|'
              '${album['artist'] ?? album['artist_name'] ?? ''}')
      .toString()
      .trim()
      .toLowerCase();
}

class NeatieSearchMasthead extends StatelessWidget {
  const NeatieSearchMasthead({
    super.key,
    this.logoAsset = 'assets/branding/neatie_3rd.png',
  });

  final String logoAsset;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: Row(
        children: [
          const SizedBox(width: 42),
          Expanded(
            child: Center(
              child: Image.asset(
                logoAsset,
                width: 46,
                height: 46,
                fit: BoxFit.contain,
                errorBuilder: (_, __, ___) => const Icon(
                  Icons.graphic_eq_rounded,
                  color: Colors.white,
                  size: 40,
                ),
              ),
            ),
          ),
          Container(
            width: 42,
            height: 42,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              border: Border.all(color: Colors.white70),
              color: Colors.white.withValues(alpha: 0.08),
            ),
            child: const Icon(
              Icons.person_rounded,
              color: Colors.white,
              size: 24,
            ),
          ),
        ],
      ),
    );
  }
}

class SearchHeaderBar extends StatelessWidget {
  const SearchHeaderBar({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.showBackButton,
    required this.onBack,
    required this.onClear,
    required this.onSubmitted,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final bool showBackButton;
  final VoidCallback onBack;
  final VoidCallback onClear;
  final ValueChanged<String> onSubmitted;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        if (showBackButton)
          IconButton(
            onPressed: onBack,
            icon: const Icon(Icons.arrow_back_rounded),
            color: Colors.white,
          ),
        Expanded(
          child: NeatieSurface(
            radius: neatieRadiusLarge,
            color: Colors.white.withValues(alpha: 0.045),
            padding: EdgeInsets.zero,
            child: Semantics(
              label: 'Search music',
              child: TextField(
                controller: controller,
                focusNode: focusNode,
                textInputAction: TextInputAction.search,
                style: const TextStyle(
                  color: Colors.white,
                  fontWeight: FontWeight.w600,
                ),
                decoration: InputDecoration(
                  prefixIcon: const Icon(
                    Icons.search_rounded,
                    color: neatieMutedText,
                    size: 24,
                  ),
                  hintText: 'What do you want to listen to?',
                  hintStyle: const TextStyle(
                    color: neatieDimText,
                    fontWeight: FontWeight.w500,
                  ),
                  filled: false,
                  isDense: true,
                  contentPadding: const EdgeInsets.symmetric(
                    vertical: 13,
                    horizontal: 14,
                  ),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(neatieRadiusLarge),
                    borderSide: BorderSide.none,
                  ),
                  suffixIcon: controller.text.trim().isNotEmpty
                      ? IconButton(
                          icon: const Icon(
                            Icons.close_rounded,
                            color: neatieMutedText,
                          ),
                          onPressed: onClear,
                        )
                      : null,
                ),
                onSubmitted: onSubmitted,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class SearchSuggestionPanel extends StatelessWidget {
  const SearchSuggestionPanel({
    super.key,
    required this.suggestions,
    required this.onSelectSuggestion,
    this.onPlaySuggestion,
    this.onPrimeSuggestion,
  });

  final List<SearchSuggestion> suggestions;
  final ValueChanged<String> onSelectSuggestion;
  final ValueChanged<SearchSuggestion>? onPlaySuggestion;
  final ValueChanged<SearchSuggestion>? onPrimeSuggestion;

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      margin: const EdgeInsets.only(bottom: 14),
      child: ListView.builder(
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        itemCount: suggestions.length,
        itemBuilder: (context, index) {
          final suggestion = suggestions[index];
          if (suggestion.isDirectPlayTrack) {
            final track = suggestion.track!;
            final title = (track['title'] ?? suggestion.text).toString();
            final artist = (track['artist'] ??
                    track['channel'] ??
                    track['author'] ??
                    'Recently played')
                .toString();
            final videoId = extractTrackId(track);
            return InkWell(
              onTap: () => onPlaySuggestion?.call(suggestion),
              onTapDown: onPrimeSuggestion == null
                  ? null
                  : (_) => onPrimeSuggestion!(suggestion),
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: track['thumbnail']?.toString(),
                      videoId: videoId,
                      width: 46,
                      height: 46,
                      radius: 10,
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontWeight: FontWeight.w800,
                            ),
                          ),
                          const SizedBox(height: 3),
                          Text(
                            artist,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white60,
                              fontSize: 13,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    const Icon(
                      Icons.play_arrow_rounded,
                      color: Colors.white,
                      size: 28,
                    ),
                  ],
                ),
              ),
            );
          }
          return InkWell(
            onTap: () => onSelectSuggestion(suggestion.text),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 13),
              child: Row(
                children: [
                  const Icon(Icons.search, color: Colors.white54),
                  const SizedBox(width: 16),
                  Expanded(
                    child: Text(
                      suggestion.text,
                      style: const TextStyle(color: Colors.white),
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

class NeatieSearchTabBar extends StatelessWidget {
  const NeatieSearchTabBar({
    super.key,
    required this.selected,
    required this.onSelected,
  });

  final String selected;
  final ValueChanged<String> onSelected;

  static const tabs = <String>[
    'Top',
    'Songs',
    'Albums',
    'Artists',
    'Playlists'
  ];

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 42,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        physics: const BouncingScrollPhysics(),
        itemCount: tabs.length,
        separatorBuilder: (_, __) => const SizedBox(width: 9),
        itemBuilder: (context, index) {
          final tab = tabs[index];
          final isSelected = tab == selected;
          return ChoiceChip(
            selected: isSelected,
            label: Text(tab),
            onSelected: (_) => onSelected(tab),
            showCheckmark: false,
            selectedColor: Colors.white,
            backgroundColor: Colors.white.withValues(alpha: 0.035),
            side: BorderSide(color: isSelected ? Colors.white : neatieStroke),
            labelStyle: TextStyle(
              color: isSelected ? Colors.black : neatieMutedText,
              fontSize: 13,
              fontWeight: FontWeight.w700,
            ),
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
          );
        },
      ),
    );
  }
}

class NeatieSearchLanding extends StatelessWidget {
  const NeatieSearchLanding({
    super.key,
    required this.songMatchLauncher,
    required this.recentSearchHistorySection,
    required this.showRecentSearchHistory,
    required this.onBrowseSelected,
  });

  final Widget songMatchLauncher;
  final Widget recentSearchHistorySection;
  final bool showRecentSearchHistory;
  final ValueChanged<String> onBrowseSelected;

  @override
  Widget build(BuildContext context) {
    const browse = <({String title, IconData icon, List<Color> colors})>[
      (
        title: 'New releases',
        icon: Icons.auto_awesome_rounded,
        colors: [Color(0xFF7E5AA8), Color(0xFF363047)]
      ),
      (
        title: 'Charts',
        icon: Icons.trending_up_rounded,
        colors: [Color(0xFF6140A0), Color(0xFFB04D70)]
      ),
      (
        title: 'Mood',
        icon: Icons.favorite_border_rounded,
        colors: [Color(0xFF527E7D), Color(0xFF273F43)]
      ),
      (
        title: 'Genres',
        icon: Icons.grid_view_rounded,
        colors: [Color(0xFF493C86), Color(0xFF7B4FB5)]
      ),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        songMatchLauncher,
        const SizedBox(height: 22),
        const Text(
          'Browse all',
          style: TextStyle(
            color: Colors.white,
            fontSize: 16,
            fontWeight: FontWeight.w800,
          ),
        ),
        const SizedBox(height: 12),
        GridView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: browse.length,
          gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            childAspectRatio: 2.8,
            crossAxisSpacing: 10,
            mainAxisSpacing: 10,
          ),
          itemBuilder: (context, index) {
            final item = browse[index];
            return InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: () => onBrowseSelected(item.title),
              child: Ink(
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(12),
                  gradient: LinearGradient(colors: item.colors),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(11),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          item.title,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 13,
                            fontWeight: FontWeight.w800,
                          ),
                        ),
                      ),
                      Icon(item.icon, color: Colors.white70, size: 21),
                    ],
                  ),
                ),
              ),
            );
          },
        ),
        if (showRecentSearchHistory) ...[
          const SizedBox(height: 24),
          recentSearchHistorySection,
        ],
      ],
    );
  }
}

List<Map<String, dynamic>> _matchingRecentTracks(
  String query,
  List<Map<String, dynamic>> tracks,
) {
  final normalizedQuery = query.trim().toLowerCase();
  if (normalizedQuery.isEmpty || tracks.isEmpty) return const [];
  final seen = <String>{};
  final matches = <Map<String, dynamic>>[];
  for (final track in tracks) {
    final title = track['title']?.toString().toLowerCase() ?? '';
    final artist = (track['artist'] ?? track['channel'] ?? track['author'])
            ?.toString()
            .toLowerCase() ??
        '';
    if (!title.contains(normalizedQuery) && !artist.contains(normalizedQuery)) {
      continue;
    }
    final key = extractTrackId(track) ??
        '$title|$artist|${track['thumbnail']?.toString() ?? ''}';
    if (!seen.add(key)) continue;
    matches.add(track);
  }
  return matches;
}

class NeatieSearchResultsSection extends StatelessWidget {
  const NeatieSearchResultsSection({
    super.key,
    required this.query,
    required this.queryIntent,
    required this.topResult,
    required this.leadArtist,
    required this.containingAlbum,
    required this.selectedTab,
    required this.isLoading,
    required this.loadingSurfaces,
    required this.appendedItemKeys,
    required this.tracks,
    required this.artists,
    required this.albums,
    required this.recentlyPlayedTracks,
    required this.similarArtists,
    required this.artistTracks,
    required this.artistAlbums,
    required this.similarTracks,
    required this.playlists,
    required this.errorMessage,
    required this.onPlayTrack,
    required this.onPrimeTrack,
    required this.onOpenTrack,
    required this.onOpenArtist,
    required this.onOpenAlbum,
    required this.onOpenPlaylist,
  });

  final String query;
  final String queryIntent;
  final Map<String, dynamic>? topResult;
  final Map<String, dynamic>? leadArtist;
  final Map<String, dynamic>? containingAlbum;
  final String selectedTab;
  final bool isLoading;
  final Set<String> loadingSurfaces;
  final Set<String> appendedItemKeys;
  final List<Map<String, dynamic>> tracks;
  final List<Map<String, dynamic>> artists;
  final List<Map<String, dynamic>> albums;
  final List<Map<String, dynamic>> recentlyPlayedTracks;
  final List<Map<String, dynamic>> similarArtists;
  final List<Map<String, dynamic>> artistTracks;
  final List<Map<String, dynamic>> artistAlbums;
  final List<Map<String, dynamic>> similarTracks;
  final List<Map<String, dynamic>> playlists;
  final String? errorMessage;
  final ValueChanged<Map<String, dynamic>> onPlayTrack;
  final ValueChanged<Map<String, dynamic>> onPrimeTrack;
  final ValueChanged<Map<String, dynamic>> onOpenTrack;
  final ValueChanged<Map<String, dynamic>> onOpenArtist;
  final ValueChanged<Map<String, dynamic>> onOpenAlbum;
  final ValueChanged<Map<String, dynamic>> onOpenPlaylist;

  @override
  Widget build(BuildContext context) {
    final normalizedTab = selectedTab.toLowerCase();
    final leadArtistName =
        (leadArtist?['name'] ?? leadArtist?['artist'] ?? '').toString().trim();
    final artistAlbumTitle = leadArtistName.isEmpty
        ? 'Albums by this artist'
        : 'Albums by $leadArtistName';
    // Query albums and artist-discography albums have different contracts.
    // Do not erase the query row merely because the same release also appears
    // in the richer artist catalog.
    final regularAlbums = albums;
    final primaryTrackIds = tracks
        .map(extractTrackId)
        .whereType<String>()
        .where((id) => id.isNotEmpty)
        .toSet();
    final additionalArtistTracks = artistTracks
        .where((track) => !primaryTrackIds.contains(extractTrackId(track)))
        .toList(growable: false);
    final sections = <Widget>[];
    if (normalizedTab == 'top') {
      final backendTopType =
          topResult?['entity_type']?.toString().toLowerCase() ?? '';
      final backendTopItem = topResult?['item'];
      final backendTop = backendTopItem is Map
          ? Map<String, dynamic>.from(backendTopItem)
          : null;
      Widget? top;
      if (backendTopType == 'artist' && backendTop != null) {
        top = _TopArtistResult(
          artist: backendTop,
          onTap: () => onOpenArtist(backendTop),
        );
      } else if (backendTopType == 'track' && backendTop != null) {
        top = _TopTrackResult(
          track: backendTop,
          onPlay: () => onPlayTrack(backendTop),
          onPrime: () => onPrimeTrack(backendTop),
          onOpen: () => onOpenTrack(backendTop),
        );
      } else if (backendTopType == 'album' && backendTop != null) {
        top = _TaggedEntityResult(
          item: backendTop,
          tag: 'Album',
          onTap: () => onOpenAlbum(backendTop),
        );
      }
      if (top != null) sections.add(top);
      if (leadArtist != null &&
          !(backendTopType == 'artist' &&
              backendTop?['id']?.toString() == leadArtist?['id']?.toString())) {
        sections.add(
          _TaggedEntityResult(
            item: leadArtist!,
            tag: 'Artist',
            circular: true,
            onTap: () => onOpenArtist(leadArtist!),
          ),
        );
      }
      if (containingAlbum != null &&
          !(backendTopType == 'album' &&
              backendTop != null &&
              _searchAlbumIdentityKey(backendTop) ==
                  _searchAlbumIdentityKey(containingAlbum!))) {
        sections.add(
          _TaggedEntityResult(
            item: containingAlbum!,
            tag: 'Album',
            onTap: () => onOpenAlbum(containingAlbum!),
          ),
        );
      }
      final recentMatches = _matchingRecentTracks(
        query,
        recentlyPlayedTracks,
      );
      if (recentMatches.isNotEmpty) {
        sections.add(
          _SearchTrackSection(
            title: 'Recently played',
            tracks: recentMatches,
            onPlay: onPlayTrack,
            onPrime: onPrimeTrack,
            onOpen: onOpenTrack,
          ),
        );
      }
      if (tracks.isNotEmpty) {
        final topId = backendTopType == 'track'
            ? extractTrackId(backendTop ?? const {})
            : null;
        sections.add(_SearchTrackSection(
          title: 'Songs',
          tracks: tracks
              .where((track) => extractTrackId(track) != topId)
              .toList(growable: false),
          appendedItemKeys: appendedItemKeys,
          onPlay: onPlayTrack,
          onPrime: onPrimeTrack,
          onOpen: onOpenTrack,
        ));
      }
      if (artists.isNotEmpty) {
        sections.add(_SearchArtistSection(
            title: 'Artists',
            artists: artists,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenArtist));
      }
      if (artistAlbums.isNotEmpty) {
        sections.add(_SearchAlbumSection(
            title: artistAlbumTitle,
            albums: artistAlbums,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenAlbum));
      }
      if (regularAlbums.isNotEmpty) {
        sections.add(_SearchAlbumSection(
            albums: regularAlbums,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenAlbum));
      }
      if (additionalArtistTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(
          title: leadArtistName.isEmpty
              ? 'More from this artist'
              : 'More from $leadArtistName',
          tracks: additionalArtistTracks,
          onPlay: onPlayTrack,
          onPrime: onPrimeTrack,
          onOpen: onOpenTrack,
        ));
      }
      if (similarTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(
            title: 'Similar tracks',
            tracks: similarTracks,
            onPlay: onPlayTrack,
            onPrime: onPrimeTrack,
            onOpen: onOpenTrack));
      }
      if (playlists.isNotEmpty) {
        sections.add(_SearchPlaylistSection(
            playlists: playlists,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenPlaylist));
      }
      if (similarArtists.isNotEmpty) {
        sections.add(_SearchArtistSection(
            title: 'Related artists',
            artists: similarArtists,
            appendedItemKeys: appendedItemKeys,
            animateSection: true,
            onOpen: onOpenArtist));
      }
    } else if (normalizedTab == 'songs') {
      if (tracks.isNotEmpty) {
        sections.add(_SearchTrackSection(
            title: 'Songs',
            tracks: tracks,
            appendedItemKeys: appendedItemKeys,
            onPlay: onPlayTrack,
            onPrime: onPrimeTrack,
            onOpen: onOpenTrack));
      }
      if (similarTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(
            title: 'Similar tracks',
            tracks: similarTracks,
            onPlay: onPlayTrack,
            onPrime: onPrimeTrack,
            onOpen: onOpenTrack));
      }
    } else if (normalizedTab == 'albums') {
      if (artistAlbums.isNotEmpty) {
        sections.add(_SearchAlbumSection(
            title: artistAlbumTitle,
            albums: artistAlbums,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenAlbum));
      }
      if (regularAlbums.isNotEmpty) {
        sections.add(_SearchAlbumSection(
            albums: regularAlbums,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenAlbum));
      }
    } else if (normalizedTab == 'artists') {
      if (artists.isNotEmpty) {
        sections.add(_SearchArtistSection(
            title: 'Artists',
            artists: artists,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenArtist));
      }
      if (artistAlbums.isNotEmpty) {
        sections.add(_SearchAlbumSection(
            title: artistAlbumTitle,
            albums: artistAlbums,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenAlbum));
      }
      if (artistTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(
          title: leadArtistName.isEmpty
              ? 'Artist works'
              : 'Works by $leadArtistName',
          tracks: artistTracks,
          onPlay: onPlayTrack,
          onPrime: onPrimeTrack,
          onOpen: onOpenTrack,
        ));
      }
      if (similarArtists.isNotEmpty) {
        sections.add(_SearchArtistSection(
            title: 'Related artists',
            artists: similarArtists,
            appendedItemKeys: appendedItemKeys,
            animateSection: true,
            onOpen: onOpenArtist));
      }
    } else {
      if (playlists.isNotEmpty) {
        sections.add(_SearchPlaylistSection(
            playlists: playlists,
            appendedItemKeys: appendedItemKeys,
            onOpen: onOpenPlaylist));
      }
    }

    if (isLoading && sections.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 32),
        child: Center(child: CircularProgressIndicator(color: neatieActive)),
      );
    }
    if (sections.isEmpty) {
      return _EmptySearchSection(
        message: errorMessage?.trim().isNotEmpty == true
            ? errorMessage!.trim()
            : query.trim().isEmpty
                ? 'Search for a song, artist, or album.'
                : 'No results found.',
      );
    }
    final loadingSurface = switch (normalizedTab) {
      'artists' => 'artists',
      'albums' => 'albums',
      'playlists' => 'playlists',
      _ => 'tracks',
    };
    final showProgressiveLoading = normalizedTab == 'top'
        ? loadingSurfaces.isNotEmpty
        : loadingSurfaces.contains(loadingSurface);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        ...sections,
        if (showProgressiveLoading)
          const Padding(
            padding: EdgeInsets.only(top: 18, bottom: 8),
            child: Center(child: NeatieQuietPicksLoadingIndicator()),
          ),
      ],
    );
  }
}

class _SearchAppendAnimation extends StatelessWidget {
  const _SearchAppendAnimation({
    super.key,
    required this.animate,
    required this.order,
    required this.child,
  });

  final bool animate;
  final int order;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final mediaQuery = MediaQuery.maybeOf(context);
    final reduceMotion = mediaQuery?.disableAnimations == true ||
        mediaQuery?.accessibleNavigation == true;
    if (!animate || reduceMotion) return child;
    final delayMs = (order % 6) * 28;
    final totalMs = 260 + delayMs;
    return TweenAnimationBuilder<double>(
      tween: Tween<double>(begin: 0, end: 1),
      duration: Duration(milliseconds: totalMs),
      curve: Interval(
        delayMs / totalMs,
        1,
        curve: Curves.easeOutCubic,
      ),
      builder: (context, value, animatedChild) => Opacity(
        opacity: value,
        child: Transform.translate(
          offset: Offset(0, 14 * (1 - value)),
          child: animatedChild,
        ),
      ),
      child: child,
    );
  }
}

class _SearchPlaylistSection extends StatelessWidget {
  const _SearchPlaylistSection({
    required this.playlists,
    required this.onOpen,
    this.appendedItemKeys = const {},
  });

  final List<Map<String, dynamic>> playlists;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final Set<String> appendedItemKeys;

  @override
  Widget build(BuildContext context) {
    final visiblePlaylists = playlists
        .where((playlist) => preferredArtworkUrl(
              playlist['thumbnail']?.toString(),
            ).isNotEmpty)
        .toList(growable: false);
    if (visiblePlaylists.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SearchSectionTitle('Playlists'),
        GridView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: 2,
              mainAxisSpacing: 18,
              crossAxisSpacing: 14,
              childAspectRatio: 0.78,
            ),
            itemCount: visiblePlaylists.length,
            itemBuilder: (context, index) {
              final playlist = visiblePlaylists[index];
              final count = (playlist['track_count'] as num?)?.toInt() ?? 0;
              final key = (playlist['id'] ?? playlist['name'] ?? '').toString();
              return _SearchAppendAnimation(
                key: ValueKey('playlist:$key'),
                animate: appendedItemKeys.contains('playlists:$key'),
                order: index,
                child: InkWell(
                  borderRadius: BorderRadius.circular(10),
                  onTap: () => onOpen(playlist),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 7),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        AppArtwork(
                          thumbnail: playlist['thumbnail'],
                          width: double.infinity,
                          height: 138,
                          radius: 10,
                        ),
                        const SizedBox(height: 9),
                        Text(
                          playlist['name']?.toString() ?? 'Playlist',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 3),
                        Text(
                          [
                            playlist['author']?.toString().trim(),
                            if (count > 0)
                              '$count ${count == 1 ? 'track' : 'tracks'}',
                          ]
                              .where((value) => value?.isNotEmpty == true)
                              .join(' • '),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: neatieMutedText),
                        ),
                      ],
                    ),
                  ),
                ),
              );
            }),
      ],
    );
  }
}

class _TaggedEntityResult extends StatelessWidget {
  const _TaggedEntityResult({
    required this.item,
    required this.tag,
    required this.onTap,
    this.circular = false,
  });

  final Map<String, dynamic> item;
  final String tag;
  final VoidCallback onTap;
  final bool circular;

  @override
  Widget build(BuildContext context) {
    final title = (item['name'] ?? item['title'] ?? tag).toString();
    final subtitle = (item['artist'] ?? item['description'] ?? '').toString();
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 7),
        child: Row(
          children: [
            if (circular)
              ArtistArtwork(
                thumbnail: item['thumbnail'],
                width: 58,
                height: 58,
              )
            else
              AppArtwork(
                thumbnail: item['thumbnail'],
                width: 58,
                height: 58,
                radius: 8,
              ),
            const SizedBox(width: 13),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  if (subtitle.trim().isNotEmpty) ...[
                    const SizedBox(height: 3),
                    Text(
                      subtitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: neatieMutedText),
                    ),
                  ],
                ],
              ),
            ),
            DecoratedBox(
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(5),
              ),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
                child: Text(
                  tag,
                  style: const TextStyle(
                    color: Colors.white70,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SearchSectionTitle extends StatelessWidget {
  const _SearchSectionTitle(this.title);

  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 18, bottom: 10),
      child: Text(
        title,
        style: const TextStyle(
          color: Colors.white,
          fontSize: 18,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }
}

class _TopArtistResult extends StatelessWidget {
  const _TopArtistResult({required this.artist, required this.onTap});

  final Map<String, dynamic> artist;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SearchSectionTitle('Top result'),
        InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(
              children: [
                ArtistArtwork(
                  thumbnail: artist['thumbnail'],
                  width: 58,
                  height: 58,
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        artist['name']?.toString() ?? 'Artist',
                        style: const TextStyle(
                          color: Colors.white,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                      const SizedBox(height: 3),
                      const Text(
                        'Artist',
                        style: TextStyle(color: neatieMutedText),
                      ),
                    ],
                  ),
                ),
                const Icon(
                  Icons.chevron_right_rounded,
                  color: neatieMutedText,
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _TopTrackResult extends StatelessWidget {
  const _TopTrackResult({
    required this.track,
    required this.onPlay,
    required this.onPrime,
    required this.onOpen,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onPrime;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SearchSectionTitle('Top result'),
        _SearchTrackTile(
          track: track,
          onPlay: onPlay,
          onPrime: onPrime,
          onOpen: onOpen,
        ),
      ],
    );
  }
}

class _SearchTrackSection extends StatelessWidget {
  const _SearchTrackSection({
    required this.title,
    required this.tracks,
    required this.onPlay,
    required this.onPrime,
    required this.onOpen,
    this.appendedItemKeys = const {},
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onPrime;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final Set<String> appendedItemKeys;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        for (final entry in tracks.indexed)
          _SearchAppendAnimation(
            key: ValueKey(
              'track:${extractTrackId(entry.$2) ?? entry.$2['title'] ?? entry.$1}',
            ),
            animate: appendedItemKeys.contains(
              'tracks:${extractTrackId(entry.$2) ?? ''}',
            ),
            order: entry.$1,
            child: _SearchTrackTile(
              track: entry.$2,
              onPlay: () => onPlay(entry.$2),
              onPrime: () => onPrime(entry.$2),
              onOpen: () => onOpen(entry.$2),
            ),
          ),
      ],
    );
  }
}

class _SearchTrackTile extends ConsumerWidget {
  const _SearchTrackTile({
    required this.track,
    required this.onPlay,
    required this.onPrime,
    required this.onOpen,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onPrime;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final subtitle = formatTrackSubtitle(track, includeDuration: true);
    return InkWell(
      onTap: onPlay,
      onTapDown: (_) => onPrime(),
      borderRadius: BorderRadius.circular(10),
      child: Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Row(
          children: [
            AppArtwork(
              thumbnail: track['thumbnail'],
              videoId: extractPlaybackSourceId(track),
              width: 52,
              height: 52,
              radius: 7,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    track['title']?.toString() ?? 'Unknown',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style:
                        const TextStyle(color: neatieMutedText, fontSize: 12),
                  ),
                ],
              ),
            ),
            TrackMenuButton(
              track: track,
              onOpenDetails: onOpen,
              onAddToPlaylist: (selectedTrack) async {
                if (!context.mounted) return;
                showAddToPlaylistDialog(
                  context: context,
                  track: Map<String, dynamic>.from(selectedTrack),
                );
              },
              onStartStation: (selectedTrack) async {
                final trackId = extractTrackId(selectedTrack);
                if (trackId == null || trackId.isEmpty) return;
                await ref
                    .read(playbackQueueProvider.notifier)
                    .startRadioSession(selectedTrack);
                if (!context.mounted) return;
                unawaited(openFullPlayer(context));
              },
              buttonSize: 36,
              iconSize: 18,
            ),
          ],
        ),
      ),
    );
  }
}

class _SearchArtistSection extends StatelessWidget {
  const _SearchArtistSection({
    required this.title,
    required this.artists,
    required this.onOpen,
    this.appendedItemKeys = const {},
    this.animateSection = false,
  });

  final String title;
  final List<Map<String, dynamic>> artists;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final Set<String> appendedItemKeys;
  final bool animateSection;

  @override
  Widget build(BuildContext context) {
    if (artists.isEmpty) return const SizedBox.shrink();
    final allItemsWereAppended = artists.every(
      (artist) => appendedItemKeys.contains(
        'artists:${_searchArtistIdentityKey(artist)}',
      ),
    );
    final section = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        GridView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            mainAxisSpacing: 18,
            crossAxisSpacing: 16,
            childAspectRatio: 0.88,
          ),
          itemCount: artists.length,
          itemBuilder: (context, index) {
            final artist = artists[index];
            final key = _searchArtistIdentityKey(artist);
            return _SearchAppendAnimation(
              key: ValueKey('artist:$key'),
              animate: appendedItemKeys.contains('artists:$key'),
              order: index,
              child: InkWell(
                onTap: () => onOpen(artist),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    ArtistArtwork(
                      thumbnail: artist['thumbnail'],
                      width: 138,
                      height: 138,
                    ),
                    const SizedBox(height: 8),
                    Text(
                      artist['name']?.toString() ?? 'Artist',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: Colors.white, fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 2),
                    const Text('Artist',
                        style: TextStyle(color: neatieMutedText, fontSize: 12)),
                  ],
                ),
              ),
            );
          },
        ),
      ],
    );
    return _SearchAppendAnimation(
      key: ValueKey('artist-section:$title'),
      animate: animateSection && allItemsWereAppended,
      order: 0,
      child: section,
    );
  }
}

class _SearchAlbumSection extends StatelessWidget {
  const _SearchAlbumSection({
    this.title = 'Albums',
    required this.albums,
    required this.onOpen,
    this.appendedItemKeys = const {},
  });

  final String title;
  final List<Map<String, dynamic>> albums;
  final ValueChanged<Map<String, dynamic>> onOpen;
  final Set<String> appendedItemKeys;

  @override
  Widget build(BuildContext context) {
    final visibleAlbums = albums
        .where((album) => preferredArtworkUrl(
              album['thumbnail']?.toString(),
            ).isNotEmpty)
        .toList(growable: false);
    if (visibleAlbums.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        GridView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: 2,
              mainAxisSpacing: 18,
              crossAxisSpacing: 14,
              childAspectRatio: 0.72,
            ),
            itemCount: visibleAlbums.length,
            itemBuilder: (context, index) {
              final album = visibleAlbums[index];
              final key = _searchAlbumIdentityKey(album);
              return _SearchAppendAnimation(
                key: ValueKey('album:$key'),
                animate: appendedItemKeys.contains('albums:$key'),
                order: index,
                child: InkWell(
                  onTap: () => onOpen(album),
                  borderRadius: BorderRadius.circular(12),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 7),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        AppArtwork(
                          thumbnail: album['thumbnail'],
                          width: double.infinity,
                          height: 150,
                          radius: 9,
                        ),
                        const SizedBox(height: 8),
                        Text(
                          album['title']?.toString() ?? 'Album',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          album['artist']?.toString() ?? '',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: neatieMutedText,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              );
            }),
      ],
    );
  }
}

class _EmptySearchSection extends StatelessWidget {
  const _EmptySearchSection({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 28),
      child: Text(
        message,
        style: const TextStyle(color: neatieMutedText, fontSize: 14),
      ),
    );
  }
}
