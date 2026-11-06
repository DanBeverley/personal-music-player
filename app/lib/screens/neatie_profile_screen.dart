import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider.dart';
import '../logic/audio_provider_history.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/auth_provider.dart';
import '../logic/details_provider.dart';
import '../logic/playlist_provider.dart';
import '../logic/recommendation_preferences_provider.dart';
import '../main_details.dart';
import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';
import '../widgets/app_artwork.dart';

class NeatieProfileScreen extends ConsumerWidget {
  const NeatieProfileScreen({super.key});

  Future<void> _openArtist(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> artist,
  ) async {
    final resolvedArtist = await resolveArtistReference(ref.read, artist);
    final artistId = resolvedArtist?['id']?.toString().trim() ?? '';
    if (!context.mounted) return;
    if (resolvedArtist == null || artistId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Artist details are not available yet.')),
      );
      return;
    }
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: resolvedArtist,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final authState = ref.watch(authProvider);
    final playlists = ref.watch(playlistProvider);
    final lastPlayed = ref.watch(lastPlayedProvider);
    final displayName =
        authState.user?.userMetadata?['full_name']?.toString() ??
            authState.user?.userMetadata?['name']?.toString() ??
            authState.user?.email?.split('@').first ??
            'Listener';
    final avatarUrl =
        authState.user?.userMetadata?['avatar_url']?.toString() ??
            authState.user?.userMetadata?['picture']?.toString();
    final recentArtists = _recentArtists(lastPlayed);

    return SafeArea(
      child: SingleChildScrollView(
        physics: const BouncingScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(20, 18, 20, 210),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SizedBox(height: 18),
            Center(
              child: Column(
                children: [
                  Container(
                    width: 104,
                    height: 104,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: neatieHairline),
                    ),
                    child: ClipOval(
                      child: avatarUrl != null && avatarUrl.isNotEmpty
                          ? Image.network(
                              avatarUrl,
                              fit: BoxFit.cover,
                              errorBuilder: (_, __, ___) =>
                                  const _ProfileFallbackAvatar(),
                            )
                          : const _ProfileFallbackAvatar(),
                    ),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    displayName,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 26,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    authState.user?.email ?? '@neatie',
                    style: const TextStyle(color: neatieMutedText),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 34),
            const _ProfileSectionHeader(title: 'Recently played artists'),
            if (recentArtists.isEmpty)
              const _ProfileEmptyCard(
                text: 'Play a few tracks and Neatie will shape this shelf.',
              )
            else
              SizedBox(
                height: 112,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  physics: const BouncingScrollPhysics(),
                  itemCount: recentArtists.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 15),
                  itemBuilder: (context, index) {
                    final artist = recentArtists[index];
                    return InkWell(
                      onTap: () => unawaited(_openArtist(context, ref, artist)),
                      borderRadius: BorderRadius.circular(999),
                      child: SizedBox(
                        width: 70,
                        child: Column(
                          children: [
                            AppArtwork(
                              thumbnail: artist['thumbnail'],
                              width: 64,
                              height: 64,
                              radius: 999,
                            ),
                            const SizedBox(height: 9),
                            Text(
                              artist['name']?.toString() ?? 'Artist',
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 11.5,
                                fontWeight: FontWeight.w600,
                                height: 1.1,
                              ),
                            ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
              ),
            const SizedBox(height: 24),
            const _ProfileSectionHeader(title: 'Recommendation source'),
            const _TastePreferencesCard(),
            const SizedBox(height: 24),
            const _ProfileSectionHeader(title: 'Public playlists'),
            if (playlists.isEmpty)
              const _ProfileEmptyCard(
                text: 'Create playlists from Library and they will appear here.',
              )
            else
              for (final playlist in playlists.take(4))
                NeatieSurface(
                  margin: const EdgeInsets.only(bottom: 12),
                  radius: neatieRadiusMedium,
                  color: Colors.white.withValues(alpha: 0.025),
                  child: Row(
                    children: [
                      AppArtwork(
                        thumbnail: playlist.tracks.isNotEmpty
                            ? playlist.tracks.first['thumbnail']
                            : null,
                        width: 50,
                        height: 50,
                        radius: 8,
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              playlist.name,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 16,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: 3),
                            Text(
                              '${playlist.tracks.length} songs',
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
            const SizedBox(height: 22),
            Center(
              child: OutlinedButton.icon(
                onPressed: authState.isBusy
                    ? null
                    : () async {
                        await ref
                            .read(audioPlayerProvider.notifier)
                            .stopPlayback();
                        ref.read(playbackQueueProvider.notifier).clearSession();
                        unawaited(ref.read(authProvider.notifier).signOut());
                      },
                icon: const Icon(Icons.logout_rounded),
                label: const Text('Sign out'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
                  side: const BorderSide(color: neatieHairline),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _TastePreferencesCard extends ConsumerStatefulWidget {
  const _TastePreferencesCard();

  @override
  ConsumerState<_TastePreferencesCard> createState() =>
      _TastePreferencesCardState();
}

class _TastePreferencesCardState
    extends ConsumerState<_TastePreferencesCard> {
  final _usernameController = TextEditingController();
  final _usernameFocus = FocusNode();

  @override
  void dispose() {
    _usernameController.dispose();
    _usernameFocus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(recommendationPreferencesProvider);
    if (!_usernameFocus.hasFocus &&
        _usernameController.text != state.listenBrainzUsername) {
      _usernameController.text = state.listenBrainzUsername;
    }
    final needsListenBrainz = state.tasteMode != 'neatie';
    final statusText = switch (state.listenBrainzStatus) {
      'ready' => 'ListenBrainz profile connected.',
      'no_recommendations' =>
        'This profile has no public recommendations; Neatie remains active.',
      'unavailable' =>
        'ListenBrainz could not be reached; Neatie remains active.',
      _ => needsListenBrainz
          ? 'Enter a public ListenBrainz username.'
          : 'Your local listening history leads recommendations.',
    };

    return NeatieSurface(
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.025),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          DropdownButtonFormField<String>(
            initialValue: state.tasteMode,
            dropdownColor: const Color(0xFF171717),
            decoration: const InputDecoration(labelText: 'Taste mode'),
            items: const [
              DropdownMenuItem(value: 'neatie', child: Text('Neatie')),
              DropdownMenuItem(value: 'blended', child: Text('Blended')),
              DropdownMenuItem(
                value: 'listenbrainz_first',
                child: Text('ListenBrainz-first'),
              ),
            ],
            onChanged: state.isLoading || state.isSaving
                ? null
                : (value) {
                    if (value != null) {
                      ref
                          .read(recommendationPreferencesProvider.notifier)
                          .setTasteMode(value);
                    }
                  },
          ),
          if (needsListenBrainz) ...[
            const SizedBox(height: 14),
            TextField(
              controller: _usernameController,
              focusNode: _usernameFocus,
              enabled: !state.isLoading && !state.isSaving,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: 'ListenBrainz username',
              ),
              onChanged: ref
                  .read(recommendationPreferencesProvider.notifier)
                  .setListenBrainzUsername,
            ),
          ],
          const SizedBox(height: 12),
          Text(
            statusText,
            style: const TextStyle(color: neatieMutedText, fontSize: 12.5),
          ),
          if (state.error.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              state.error,
              style: const TextStyle(color: Colors.redAccent, fontSize: 12.5),
            ),
          ],
          const SizedBox(height: 14),
          Align(
            alignment: Alignment.centerRight,
            child: FilledButton(
              onPressed: state.isLoading || state.isSaving
                  ? null
                  : () async {
                      FocusScope.of(context).unfocus();
                      final saved = await ref
                          .read(recommendationPreferencesProvider.notifier)
                          .save();
                      if (!context.mounted || !saved) return;
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(
                          content: Text('Recommendation settings saved.'),
                        ),
                      );
                    },
              child: Text(state.isSaving ? 'Saving…' : 'Save'),
            ),
          ),
        ],
      ),
    );
  }
}

class _ProfileFallbackAvatar extends StatelessWidget {
  const _ProfileFallbackAvatar();

  @override
  Widget build(BuildContext context) {
    return const ColoredBox(
      color: Color(0xFF111111),
      child: Icon(Icons.person_rounded, color: Colors.white, size: 40),
    );
  }
}

class _ProfileSectionHeader extends StatelessWidget {
  const _ProfileSectionHeader({required this.title});

  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
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

class _ProfileEmptyCard extends StatelessWidget {
  const _ProfileEmptyCard({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.025),
      child: Text(
        text,
        style: const TextStyle(
          color: neatieMutedText,
          fontSize: 13,
          height: 1.4,
        ),
      ),
    );
  }
}

List<Map<String, dynamic>> _recentArtists(List<Map<String, dynamic>> tracks) {
  final seen = <String>{};
  final artists = <Map<String, dynamic>>[];
  for (final track in tracks) {
    final name = (track['channel'] ?? track['artist'] ?? track['author'])
            ?.toString()
            .trim() ??
        '';
    if (name.isEmpty || !seen.add(name.toLowerCase())) continue;
    artists.add({
      'id': track['artist_id'] ?? track['channel_id'],
      'name': name,
      'thumbnail': track['thumbnail'],
      'resolution_status': 'recent_history',
    });
    if (artists.length >= 8) break;
  }
  return artists;
}
