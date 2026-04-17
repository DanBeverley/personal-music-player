import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/playlist_provider.dart';
import '../../ui/app_theme_tokens.dart';

void showAddToPlaylistDialog({
  required BuildContext context,
  required Map<String, dynamic> track,
}) {
  showGeneralDialog(
    context: context,
    barrierDismissible: true,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    barrierColor: Colors.black54,
    transitionDuration: const Duration(milliseconds: 300),
    pageBuilder: (buildContext, animation, secondaryAnimation) {
      return BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
        child: ScaleTransition(
          scale: CurvedAnimation(parent: animation, curve: Curves.easeOutBack),
          child: AddToPlaylistDialog(track: track),
        ),
      );
    },
  );
}

class AddToPlaylistDialog extends ConsumerStatefulWidget {
  final Map<String, dynamic> track;

  const AddToPlaylistDialog({
    super.key,
    required this.track,
  });

  @override
  ConsumerState<AddToPlaylistDialog> createState() =>
      _AddToPlaylistDialogState();
}

class _AddToPlaylistDialogState extends ConsumerState<AddToPlaylistDialog> {
  final TextEditingController _playlistNameController = TextEditingController();

  @override
  void dispose() {
    _playlistNameController.dispose();
    super.dispose();
  }

  void _createPlaylistAndAddTrack() {
    final playlistName = _playlistNameController.text.trim();
    if (playlistName.isEmpty) return;
    final messenger = ScaffoldMessenger.of(context);

    final playlist =
        ref.read(playlistProvider.notifier).createPlaylist(playlistName);
    ref
        .read(playlistProvider.notifier)
        .addTrackToPlaylist(playlist.id, widget.track);

    Navigator.pop(context);
    messenger.showSnackBar(
      SnackBar(
        content: Text('Created $playlistName and added this track'),
        duration: const Duration(seconds: 1),
      ),
    );
  }

  void _addTrackToPlaylist(Playlist playlist) {
    final messenger = ScaffoldMessenger.of(context);
    ref
        .read(playlistProvider.notifier)
        .addTrackToPlaylist(playlist.id, widget.track);
    Navigator.pop(context);
    messenger.showSnackBar(
      SnackBar(
        content: Text('Added to ${playlist.name}'),
        duration: const Duration(seconds: 1),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final playlists = ref.watch(playlistProvider);
    final hasDraftName = _playlistNameController.text.trim().isNotEmpty;

    return Center(
      child: Material(
        color: Colors.transparent,
        child: Container(
          width: 360,
          margin: const EdgeInsets.symmetric(horizontal: 24),
          padding: const EdgeInsets.fromLTRB(24, 22, 24, 18),
          decoration: BoxDecoration(
            color: Colors.grey[900]?.withValues(alpha: 0.86),
            borderRadius: BorderRadius.circular(appRadiusLarge),
            border: Border.all(
              color: Colors.white.withValues(alpha: 0.08),
              width: 1,
            ),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.35),
                blurRadius: 30,
                offset: const Offset(0, 14),
              ),
            ],
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Add to Playlist',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 10),
              Text(
                playlists.isEmpty
                    ? 'No playlists yet. Create one here and this track will be added immediately.'
                    : 'Pick an existing playlist or create a new one right here.',
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.58),
                  fontSize: 13,
                  height: 1.35,
                ),
              ),
              if (playlists.isNotEmpty) ...[
                const SizedBox(height: 18),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 220),
                  child: ListView.separated(
                    shrinkWrap: true,
                    itemCount: playlists.length,
                    separatorBuilder: (_, __) => Divider(
                      color: Colors.white.withValues(alpha: 0.06),
                      height: 1,
                    ),
                    itemBuilder: (context, index) {
                      final playlist = playlists[index];
                      return ListTile(
                        contentPadding: EdgeInsets.zero,
                        title: Text(
                          playlist.name,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        subtitle: Text(
                          '${playlist.tracks.length} tracks',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.45),
                          ),
                        ),
                        trailing: Icon(
                          Icons.arrow_forward_ios_rounded,
                          color: Colors.white.withValues(alpha: 0.4),
                          size: 16,
                        ),
                        onTap: () => _addTrackToPlaylist(playlist),
                      );
                    },
                  ),
                ),
              ],
              const SizedBox(height: 18),
              TextField(
                controller: _playlistNameController,
                onChanged: (_) => setState(() {}),
                style: const TextStyle(color: Colors.white),
                decoration: InputDecoration(
                  hintText: 'New playlist name',
                  hintStyle: TextStyle(
                    color: Colors.white.withValues(alpha: 0.35),
                  ),
                  filled: true,
                  fillColor: Colors.white.withValues(alpha: 0.04),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(16),
                    borderSide: BorderSide(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(16),
                    borderSide: BorderSide(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(16),
                    borderSide: const BorderSide(color: appAccentGrey),
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  TextButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text(
                      'Cancel',
                      style: TextStyle(color: Colors.white54),
                    ),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: appSurfaceGreyAlt,
                      foregroundColor: Colors.white,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                    onPressed: hasDraftName ? _createPlaylistAndAddTrack : null,
                    child: const Text('Create & Add'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
