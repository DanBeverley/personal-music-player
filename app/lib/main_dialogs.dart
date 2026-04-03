part of 'main.dart';

void showGlassDialog({required BuildContext context, required String title, required Widget content, required List<Widget> actions}) {
  showGeneralDialog(
    context: context,
    barrierDismissible: true,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    barrierColor: Colors.black54,
    transitionDuration: const Duration(milliseconds: 300),
    pageBuilder: (BuildContext buildContext, Animation<double> animation, Animation<double> secondaryAnimation) {
      return BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
        child: ScaleTransition(
          scale: CurvedAnimation(parent: animation, curve: Curves.easeOutBack),
          child: AlertDialog(
            backgroundColor: Colors.grey[900]?.withValues(alpha: 0.7),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(_radiusLarge),
              side: BorderSide(color: Colors.white.withValues(alpha: 0.1), width: 1),
            ),
            title: Text(title, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
            content: content,
            actions: actions,
          )
        )
      );
    }
  );
}

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
          child: _AddToPlaylistDialog(track: track),
        ),
      );
    },
  );
}

Color _playlistCoverColor(Playlist playlist) {
  final value = playlist.coverColorValue;
  if (value != null) {
    return Color(value);
  }
  return _playlistCoverPalette[playlist.name.hashCode.abs() %
      _playlistCoverPalette.length];
}

class PlaylistArtworkView extends StatelessWidget {
  final Playlist playlist;
  final double size;
  final double radius;
  final VoidCallback? onTap;

  const PlaylistArtworkView({
    super.key,
    required this.playlist,
    this.size = 56,
    this.radius = 14,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final coverPath = playlist.coverImagePath;
    final coverFile =
        coverPath == null || coverPath.isEmpty ? null : File(coverPath);
    final hasLocalImage = coverFile?.existsSync() ?? false;
    final artwork = Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(radius),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            _playlistCoverColor(playlist),
            _playlistCoverColor(playlist).withValues(alpha: 0.76),
          ],
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.2),
            blurRadius: 14,
            offset: const Offset(0, 8),
          ),
        ],
      ),
        child: ClipRRect(
          borderRadius: BorderRadius.circular(radius),
        child: hasLocalImage
            ? Image.file(
                coverFile!,
                fit: BoxFit.cover,
              )
            : Center(
                child: Icon(
                  Icons.library_music_rounded,
                  color: Colors.white.withValues(alpha: 0.76),
                  size: size * 0.38,
                ),
              ),
      ),
    );

    if (onTap == null) return artwork;
    return GestureDetector(onTap: onTap, child: artwork);
  }
}

void showPlaylistArtworkDialog({
  required BuildContext context,
  required Playlist playlist,
}) {
  showGeneralDialog(
    context: context,
    barrierDismissible: true,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    barrierColor: Colors.black54,
    transitionDuration: const Duration(milliseconds: 280),
    pageBuilder: (buildContext, animation, secondaryAnimation) {
      return BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
        child: ScaleTransition(
          scale: CurvedAnimation(parent: animation, curve: Curves.easeOutBack),
          child: _PlaylistArtworkDialog(playlist: playlist),
        ),
      );
    },
  );
}

class _PlaylistArtworkDialog extends ConsumerStatefulWidget {
  final Playlist playlist;

  const _PlaylistArtworkDialog({required this.playlist});

  @override
  ConsumerState<_PlaylistArtworkDialog> createState() =>
      _PlaylistArtworkDialogState();
}

class _PlaylistArtworkDialogState extends ConsumerState<_PlaylistArtworkDialog> {
  bool _isPicking = false;

  Future<void> _deleteOldCover(String? path) async {
    if (path == null || path.isEmpty) return;
    try {
      final oldFile = File(path);
      if (oldFile.existsSync()) {
        await oldFile.delete();
      }
    } catch (_) {
      // Ignore stale cover cleanup failures.
    }
  }

  Future<void> _pickImage() async {
    setState(() => _isPicking = true);
    try {
      final picker = ImagePicker();
      final picked = await picker.pickImage(
        source: ImageSource.gallery,
        imageQuality: 92,
        maxWidth: 1400,
      );
      if (picked == null || !mounted) return;

      final coverDir = await getScopedPlaylistCoversDirectory();

      final extension = picked.path.contains('.')
          ? picked.path.split('.').last
          : 'jpg';
      final copiedFile = await File(picked.path).copy(
        '${coverDir.path}/${widget.playlist.id}_${DateTime.now().millisecondsSinceEpoch}.$extension',
      );

      final oldPath = widget.playlist.coverImagePath;
      ref.read(playlistProvider.notifier).updatePlaylistArtwork(
            widget.playlist.id,
            coverImagePath: copiedFile.path,
          );
      unawaited(_deleteOldCover(oldPath));
      if (!mounted) return;
      Navigator.pop(context);
    } finally {
      if (mounted) {
        setState(() => _isPicking = false);
      }
    }
  }

  void _applyColor(Color color) {
      final oldPath = widget.playlist.coverImagePath;
      ref.read(playlistProvider.notifier).updatePlaylistArtwork(
            widget.playlist.id,
            coverColorValue: color.toARGB32(),
            clearCoverImage: true,
          );
    unawaited(_deleteOldCover(oldPath));
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Material(
        color: Colors.transparent,
        child: Container(
          width: 360,
          margin: const EdgeInsets.symmetric(horizontal: 24),
          padding: const EdgeInsets.fromLTRB(24, 22, 24, 18),
          decoration: BoxDecoration(
            color: Colors.grey[900]?.withValues(alpha: 0.88),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Playlist Artwork',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 16),
              Center(
                child: PlaylistArtworkView(
                  playlist: widget.playlist,
                  size: 112,
                  radius: 24,
                ),
              ),
              const SizedBox(height: 18),
              Text(
                'Pick a color mood or bring your own image.',
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.56),
                  fontSize: 13,
                ),
              ),
              const SizedBox(height: 14),
              Wrap(
                spacing: 10,
                runSpacing: 10,
                children: _playlistCoverPalette.map((color) {
                  return GestureDetector(
                    onTap: () => _applyColor(color),
                    child: Container(
                      width: 34,
                      height: 34,
                      decoration: BoxDecoration(
                        color: color,
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.16),
                        ),
                      ),
                    ),
                  );
                }).toList(growable: false),
              ),
              const SizedBox(height: 18),
              SizedBox(
                width: double.infinity,
                child: TextButton.icon(
                  onPressed: _isPicking ? null : _pickImage,
                  icon: _isPicking
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.photo_library_outlined),
                  label: const Text('Choose Image'),
                  style: TextButton.styleFrom(
                    foregroundColor: Colors.white,
                    backgroundColor: Colors.white.withValues(alpha: 0.06),
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(18),
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text(
                    'Close',
                    style: TextStyle(color: Colors.white54),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _LyricsTabClipper extends CustomClipper<Path> {
  final bool pointLeft;

  const _LyricsTabClipper({required this.pointLeft});

  @override
  Path getClip(Size size) {
    final path = Path();
    const outerRadius = 12.0;
    const tipInset = 3.0;
    final midY = size.height / 2;
    if (pointLeft) {
      path.moveTo(size.width, outerRadius);
      path.quadraticBezierTo(size.width, 0, size.width - outerRadius, 0);
      path.lineTo(outerRadius + 6, 0);
      path.quadraticBezierTo(
        tipInset,
        midY * 0.32,
        tipInset,
        midY,
      );
      path.quadraticBezierTo(
        tipInset,
        size.height - (midY * 0.32),
        outerRadius + 6,
        size.height,
      );
      path.lineTo(size.width - outerRadius, size.height);
      path.quadraticBezierTo(
        size.width,
        size.height,
        size.width,
        size.height - outerRadius,
      );
    } else {
      path.moveTo(0, outerRadius);
      path.quadraticBezierTo(0, 0, outerRadius, 0);
      path.lineTo(size.width - outerRadius - 6, 0);
      path.quadraticBezierTo(
        size.width - tipInset,
        midY * 0.32,
        size.width - tipInset,
        midY,
      );
      path.quadraticBezierTo(
        size.width - tipInset,
        size.height - (midY * 0.32),
        size.width - outerRadius - 6,
        size.height,
      );
      path.lineTo(outerRadius, size.height);
      path.quadraticBezierTo(0, size.height, 0, size.height - outerRadius);
    }
    path.close();
    return path;
  }

  @override
  bool shouldReclip(covariant _LyricsTabClipper oldClipper) {
    return oldClipper.pointLeft != pointLeft;
  }
}

class _AddToPlaylistDialog extends ConsumerStatefulWidget {
  final Map<String, dynamic> track;

  const _AddToPlaylistDialog({required this.track});

  @override
  ConsumerState<_AddToPlaylistDialog> createState() =>
      _AddToPlaylistDialogState();
}

class _AddToPlaylistDialogState extends ConsumerState<_AddToPlaylistDialog> {
  final TextEditingController _playlistNameController =
      TextEditingController();

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
            borderRadius: BorderRadius.circular(_radiusLarge),
            border:
                Border.all(color: Colors.white.withValues(alpha: 0.08), width: 1),
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
                    borderSide: const BorderSide(color: _accentGrey),
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
                      backgroundColor: _surfaceGreyAlt,
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

