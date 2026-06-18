import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/audio_provider_queue.dart';
import '../../logic/track_metadata.dart';
import '../../navigation/player_navigation.dart';
import '../../ui/app_theme_tokens.dart';
import '../../ui/neatie_components.dart';
import '../app_artwork.dart';
import '../playlist/add_to_playlist_dialog.dart';
import 'track_menu_button.dart';

class NeatieSearchMasthead extends StatelessWidget {
  const NeatieSearchMasthead({
    super.key,
    this.logoAsset = 'assets/branding/neatie_intro_mark.png',
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
  });

  final List<String> suggestions;
  final ValueChanged<String> onSelectSuggestion;

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
          return InkWell(
            onTap: () => onSelectSuggestion(suggestion),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 13),
              child: Row(
                children: [
                  const Icon(Icons.search, color: Colors.white54),
                  const SizedBox(width: 16),
                  Expanded(
                    child: Text(
                      suggestion,
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

  static const tabs = <String>['Top', 'Songs', 'Albums', 'Artists', 'Playlists'];

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

class NeatieSearchResultsSection extends StatelessWidget {
  const NeatieSearchResultsSection({
    super.key,
    required this.query,
    required this.queryIntent,
    required this.selectedTab,
    required this.isLoading,
    required this.tracks,
    required this.artists,
    required this.albums,
    required this.similarArtists,
    required this.artistWorks,
    required this.similarTracks,
    required this.playlists,
    required this.errorMessage,
    required this.onPlayTrack,
    required this.onOpenTrack,
    required this.onOpenArtist,
    required this.onOpenAlbum,
    required this.onOpenPlaylist,
  });

  final String query;
  final String queryIntent;
  final String selectedTab;
  final bool isLoading;
  final List<Map<String, dynamic>> tracks;
  final List<Map<String, dynamic>> artists;
  final List<Map<String, dynamic>> albums;
  final List<Map<String, dynamic>> similarArtists;
  final List<Map<String, dynamic>> artistWorks;
  final List<Map<String, dynamic>> similarTracks;
  final List<Map<String, dynamic>> playlists;
  final String? errorMessage;
  final ValueChanged<Map<String, dynamic>> onPlayTrack;
  final ValueChanged<Map<String, dynamic>> onOpenTrack;
  final ValueChanged<Map<String, dynamic>> onOpenArtist;
  final ValueChanged<Map<String, dynamic>> onOpenAlbum;
  final ValueChanged<Map<String, dynamic>> onOpenPlaylist;

  @override
  Widget build(BuildContext context) {
    final normalizedTab = selectedTab.toLowerCase();
    final normalizedIntent = queryIntent.trim().toLowerCase();
    final preferTrackTop = normalizedIntent == 'track' ||
        (normalizedIntent == 'exact' && tracks.isNotEmpty);
    final sections = <Widget>[];
    if (normalizedTab == 'top') {
      final top = preferTrackTop && tracks.isNotEmpty
          ? _TopTrackResult(
              track: tracks.first,
              onPlay: () => onPlayTrack(tracks.first),
              onOpen: () => onOpenTrack(tracks.first),
            )
          : artists.isNotEmpty
              ? _TopArtistResult(
                  artist: artists.first,
                  onTap: () => onOpenArtist(artists.first),
                )
              : tracks.isNotEmpty
                  ? _TopTrackResult(
                      track: tracks.first,
                      onPlay: () => onPlayTrack(tracks.first),
                      onOpen: () => onOpenTrack(tracks.first),
                    )
                  : null;
      if (top != null) sections.add(top);
      if (tracks.isNotEmpty) {
        sections.add(_SearchTrackSection(title: 'Songs', tracks: tracks.take(5).toList(growable: false), onPlay: onPlayTrack, onOpen: onOpenTrack));
      }
      if (artists.isNotEmpty) {
        sections.add(_SearchArtistSection(title: 'Artists', artists: artists.take(5).toList(growable: false), onOpen: onOpenArtist));
      }
      if (similarArtists.isNotEmpty) {
        sections.add(_SearchArtistSection(title: 'Similar artists', artists: similarArtists.take(5).toList(growable: false), onOpen: onOpenArtist));
      }
      if (albums.isNotEmpty) {
        sections.add(_SearchAlbumSection(albums: albums.take(8).toList(growable: false), onOpen: onOpenAlbum));
      }
      if (artistWorks.isNotEmpty) {
        sections.add(_ArtistWorksSection(works: artistWorks.take(6).toList(growable: false), onPlayTrack: onPlayTrack, onOpenTrack: onOpenTrack, onOpenAlbum: onOpenAlbum));
      }
      if (similarTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(title: 'Similar tracks', tracks: similarTracks.take(5).toList(growable: false), onPlay: onPlayTrack, onOpen: onOpenTrack));
      }
      if (playlists.isNotEmpty) {
        sections.add(_SearchPlaylistSection(playlists: playlists.take(4).toList(growable: false), onOpen: onOpenPlaylist));
      }
    } else if (normalizedTab == 'songs') {
      if (tracks.isNotEmpty) {
        sections.add(_SearchTrackSection(title: 'Songs', tracks: tracks, onPlay: onPlayTrack, onOpen: onOpenTrack));
      }
      if (similarTracks.isNotEmpty) {
        sections.add(_SearchTrackSection(title: 'Similar tracks', tracks: similarTracks, onPlay: onPlayTrack, onOpen: onOpenTrack));
      }
    } else if (normalizedTab == 'albums') {
      if (albums.isNotEmpty) {
        sections.add(_SearchAlbumSection(albums: albums, onOpen: onOpenAlbum));
      }
    } else if (normalizedTab == 'artists') {
      if (artists.isNotEmpty) {
        sections.add(_SearchArtistSection(title: 'Artists', artists: artists, onOpen: onOpenArtist));
      }
      if (similarArtists.isNotEmpty) {
        sections.add(_SearchArtistSection(title: 'Similar artists', artists: similarArtists, onOpen: onOpenArtist));
      }
      if (artistWorks.isNotEmpty) {
        sections.add(_ArtistWorksSection(works: artistWorks, onPlayTrack: onPlayTrack, onOpenTrack: onOpenTrack, onOpenAlbum: onOpenAlbum));
      }
    } else {
      if (playlists.isNotEmpty) {
        sections.add(_SearchPlaylistSection(playlists: playlists, onOpen: onOpenPlaylist));
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
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: sections,
    );
  }
}

class _SearchPlaylistSection extends StatelessWidget {
  const _SearchPlaylistSection({
    required this.playlists,
    required this.onOpen,
  });

  final List<Map<String, dynamic>> playlists;
  final ValueChanged<Map<String, dynamic>> onOpen;

  @override
  Widget build(BuildContext context) {
    if (playlists.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SearchSectionTitle('Playlists'),
        ...playlists.map((playlist) {
          final count = playlist['track_count'] as int? ?? 0;
          return InkWell(
            borderRadius: BorderRadius.circular(10),
            onTap: () => onOpen(playlist),
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 7),
              child: Row(
                children: [
                  AppArtwork(
                    thumbnail: playlist['thumbnail'],
                    width: 54,
                    height: 54,
                    radius: 8,
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
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
                          '$count ${count == 1 ? 'track' : 'tracks'}',
                          style: const TextStyle(color: neatieMutedText),
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
          );
        }),
      ],
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
                AppArtwork(
                  thumbnail: artist['thumbnail'],
                  width: 58,
                  height: 58,
                  radius: 999,
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
    required this.onOpen,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SearchSectionTitle('Top result'),
        _SearchTrackTile(track: track, onPlay: onPlay, onOpen: onOpen),
      ],
    );
  }
}

class _SearchTrackSection extends StatelessWidget {
  const _SearchTrackSection({
    required this.title,
    required this.tracks,
    required this.onPlay,
    required this.onOpen,
  });

  final String title;
  final List<Map<String, dynamic>> tracks;
  final ValueChanged<Map<String, dynamic>> onPlay;
  final ValueChanged<Map<String, dynamic>> onOpen;

  @override
  Widget build(BuildContext context) {
    if (tracks.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        for (final track in tracks)
          _SearchTrackTile(
            track: track,
            onPlay: () => onPlay(track),
            onOpen: () => onOpen(track),
          ),
      ],
    );
  }
}

class _SearchTrackTile extends ConsumerWidget {
  const _SearchTrackTile({
    required this.track,
    required this.onPlay,
    required this.onOpen,
  });

  final Map<String, dynamic> track;
  final VoidCallback onPlay;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final artist = (track['channel'] ?? track['artist'] ?? track['author'] ?? '').toString();
    return InkWell(
      onTap: onPlay,
      borderRadius: BorderRadius.circular(10),
      child: Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Row(
          children: [
            AppArtwork(
              thumbnail: track['thumbnail'],
              videoId: (track['id'] ?? track['videoId'])?.toString(),
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
                    artist,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: neatieMutedText, fontSize: 12),
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
  });

  final String title;
  final List<Map<String, dynamic>> artists;
  final ValueChanged<Map<String, dynamic>> onOpen;

  @override
  Widget build(BuildContext context) {
    if (artists.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        SizedBox(
          height: 86,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: artists.length,
            separatorBuilder: (_, __) => const SizedBox(width: 18),
            itemBuilder: (context, index) {
              final artist = artists[index];
              return SizedBox(
                width: 120,
                child: InkWell(
                  onTap: () => onOpen(artist),
                  child: Row(
                    children: [
                      AppArtwork(
                        thumbnail: artist['thumbnail'],
                        width: 58,
                        height: 58,
                        radius: 999,
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          artist['name']?.toString() ?? 'Artist',
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700),
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _ArtistWorksSection extends StatelessWidget {
  const _ArtistWorksSection({
    required this.works,
    required this.onPlayTrack,
    required this.onOpenTrack,
    required this.onOpenAlbum,
  });

  final List<Map<String, dynamic>> works;
  final ValueChanged<Map<String, dynamic>> onPlayTrack;
  final ValueChanged<Map<String, dynamic>> onOpenTrack;
  final ValueChanged<Map<String, dynamic>> onOpenAlbum;

  bool _isAlbum(Map<String, dynamic> work) {
    final type = (work['entity_type'] ?? work['type'] ?? work['kind'])
        ?.toString()
        .toLowerCase();
    final hasTrackShape = work['videoId'] != null ||
        work['video_id'] != null ||
        work['channel'] != null ||
        work['author'] != null ||
        work['duration'] != null;
    return type == 'album' ||
        work['browseId'] != null ||
        work['album_id'] != null ||
        (!hasTrackShape && work['artist'] != null && work['title'] != null);
  }

  @override
  Widget build(BuildContext context) {
    if (works.isEmpty) return const SizedBox.shrink();
    final albums = works.where(_isAlbum).toList(growable: false);
    final tracks = works.where((work) => !_isAlbum(work)).toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (tracks.isNotEmpty)
          _SearchTrackSection(
            title: 'Artist works',
            tracks: tracks,
            onPlay: onPlayTrack,
            onOpen: onOpenTrack,
          ),
        if (albums.isNotEmpty)
          _SearchAlbumSection(
            title: tracks.isEmpty ? 'Artist works' : 'Albums by these artists',
            albums: albums,
            onOpen: onOpenAlbum,
          ),
      ],
    );
  }
}

class _SearchAlbumSection extends StatelessWidget {
  const _SearchAlbumSection({
    this.title = 'Albums',
    required this.albums,
    required this.onOpen,
  });

  final String title;
  final List<Map<String, dynamic>> albums;
  final ValueChanged<Map<String, dynamic>> onOpen;

  @override
  Widget build(BuildContext context) {
    if (albums.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SearchSectionTitle(title),
        for (final album in albums)
          InkWell(
            onTap: () => onOpen(album),
            borderRadius: BorderRadius.circular(12),
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 7),
              child: Row(
                children: [
                  AppArtwork(
                    thumbnail: album['thumbnail'],
                    width: 64,
                    height: 64,
                    radius: 9,
                  ),
                  const SizedBox(width: 13),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
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
                        if ((album['year'] ?? album['release_date'])
                                ?.toString()
                                .trim()
                                .isNotEmpty ==
                            true) ...[
                          const SizedBox(height: 3),
                          Text(
                            (album['year'] ?? album['release_date']).toString(),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white38,
                              fontSize: 11,
                            ),
                          ),
                        ],
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
