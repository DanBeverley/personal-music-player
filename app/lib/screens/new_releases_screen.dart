import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/audio_provider_queue.dart';
import '../logic/details_provider.dart';
import '../logic/new_releases_provider.dart';
import '../main_details.dart';
import '../ui/app_theme_tokens.dart';
import '../widgets/app_artwork.dart';

class NewReleasesScreen extends ConsumerWidget {
  const NewReleasesScreen({super.key});

  Future<void> _openRelease(
    BuildContext context,
    WidgetRef ref,
    NewReleaseEntry release,
  ) async {
    if (release.raw['entry_type'] == 'track') {
      await ref
          .read(playbackQueueProvider.notifier)
          .startDiscoverySession(release.raw);
      return;
    }
    ref.read(albumDetailsProvider.notifier).fetchAlbum(release.id);
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => AlbumDetailsScreen(
          albumId: release.id,
          fallbackAlbum: release.raw,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final snapshot = ref.watch(newReleasesProvider(false));
    Future<void> refresh() async {
      await ref.refresh(newReleasesProvider(true).future);
      ref.invalidate(newReleasesProvider(false));
    }

    return Scaffold(
      backgroundColor: neatieInk,
      appBar: AppBar(
        backgroundColor: neatieInk,
        foregroundColor: neatieActive,
        title: const Text('New releases'),
      ),
      body: RefreshIndicator(
        onRefresh: refresh,
        child: snapshot.when(
          loading: () => const _ReleaseMessage(
            title: 'Finding recent releases',
            loading: true,
          ),
          error: (_, __) => _ReleaseMessage(
            title: 'Releases could not load',
            onRetry: () => ref.invalidate(newReleasesProvider(false)),
          ),
          data: (value) {
            if (value.entries.isEmpty) {
              return _ReleaseMessage(
                title: 'No recent releases available',
                onRetry: refresh,
              );
            }
            return ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 112),
              children: [
                const Text('Recent releases',
                    style: TextStyle(
                        color: neatieActive,
                        fontSize: 26,
                        fontWeight: FontWeight.w800)),
                const SizedBox(height: 6),
                Text(
                    value.entries.any((entry) => entry.raw['views'] != null)
                        ? 'Ranked by available YouTube views. Counts are source-specific.'
                        : 'Recent tracks and albums. View counts are unavailable from these sources.',
                    style: TextStyle(color: neatieMutedText)),
                if (value.refreshStatus == 'failed' || value.status == 'stale')
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Text(
                      'Refresh failed. Showing saved releases'
                      '${value.generatedAt == null ? '' : ' from ${value.generatedAt!.toLocal().toString().split(' ').first}'}. Pull down to retry.',
                      style: const TextStyle(color: Colors.amber),
                    ),
                  )
                else if (value.refreshStatus == 'partial')
                  const Padding(
                    padding: EdgeInsets.only(top: 12),
                    child: Text('Recent releases updated. The older release window could not refresh yet.',
                        style: TextStyle(color: neatieMutedText)),
                  ),
                for (final kind in ['track', 'album']) ...[
                  Padding(
                    padding: const EdgeInsets.only(top: 28, bottom: 12),
                    child: Text(kind == 'track' ? 'Tracks' : 'Albums',
                        style: const TextStyle(
                            color: neatieActive,
                            fontSize: 22,
                            fontWeight: FontWeight.w800)),
                  ),
                  if (!value.entries.any(
                      (entry) => (entry.raw['entry_type'] ?? 'album') == kind))
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 18),
                      child: Text('No releases available in this section yet.',
                          style: TextStyle(color: neatieMutedText)),
                    ),
                  for (final entry in value.entries.where(
                      (entry) => (entry.raw['entry_type'] ?? 'album') == kind))
                    _ReleaseRow(
                        release: entry,
                        onTap: () => _openRelease(context, ref, entry)),
                ],
              ],
            );
          },
        ),
      ),
    );
  }
}

class _ReleaseRow extends StatelessWidget {
  const _ReleaseRow({required this.release, required this.onTap});
  final NewReleaseEntry release;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final views = release.raw['views']?.toString().trim() ?? '';
    final count = views.toLowerCase().contains('view') ? views : '$views views';
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(12),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 12),
        child: Row(
          children: [
            SizedBox(
                width: 34,
                child: Text('${release.raw['release_rank'] ?? '–'}',
                    style: const TextStyle(
                        color: neatieMutedText,
                        fontSize: 20,
                        fontWeight: FontWeight.w700))),
            AppArtwork(
                thumbnail: release.thumbnail,
                width: 64,
                height: 64,
                radius: 10),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(release.title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: neatieActive,
                          fontWeight: FontWeight.w700,
                          fontSize: 15)),
                  Text(release.artist,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: neatieMutedText)),
                  if (release.releaseDate.isNotEmpty)
                    Text(release.releaseDate,
                        style: const TextStyle(
                            color: neatieMutedText, fontSize: 12)),
                  const SizedBox(height: 4),
                  Text(views.isEmpty ? 'View count unavailable' : count,
                      style: const TextStyle(
                          color: neatieMutedText, fontSize: 12)),
                ],
              ),
            ),
            Icon(
                release.raw['entry_type'] == 'track'
                    ? Icons.play_circle_outline_rounded
                    : Icons.chevron_right_rounded,
                color: neatieActive),
          ],
        ),
      ),
    );
  }
}

class _ReleaseMessage extends StatelessWidget {
  const _ReleaseMessage(
      {required this.title, this.loading = false, this.onRetry});
  final String title;
  final bool loading;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) => ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(24),
        children: [
          SizedBox(height: MediaQuery.sizeOf(context).height * 0.2),
          Text(title,
              textAlign: TextAlign.center,
              style: const TextStyle(color: neatieActive, fontSize: 18)),
          const SizedBox(height: 18),
          if (loading) const Center(child: CircularProgressIndicator()),
          if (onRetry != null)
            Center(
                child:
                    TextButton(onPressed: onRetry, child: const Text('Retry'))),
        ],
      );
}
