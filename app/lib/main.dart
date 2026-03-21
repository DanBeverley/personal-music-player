import 'dart:io';
import 'dart:ui';
import 'dart:convert';
import 'dart:async';
import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:permission_handler/permission_handler.dart';
import 'logic/audio_provider.dart';
import 'logic/auth_provider.dart';
import 'logic/download_provider.dart';
import 'logic/playlist_provider.dart';
import 'widgets/app_artwork.dart';
import 'widgets/app_bottom_nav_bar.dart';
import 'widgets/download_hud.dart';

const _accentGrey = Color(0xFFB7BBC4);
const _surfaceGrey = Color(0xFF141414);
const _surfaceGreyAlt = Color(0xFF1B1B1B);
const _voidBlack = Color(0xFF070707);
const double _radiusLarge = 12;
const double _radiusMedium = 10;
const List<Color> _playlistCoverPalette = <Color>[
  Color(0xFF525866),
  Color(0xFF4A5260),
  Color(0xFF3F4755),
  Color(0xFF5B4D4A),
  Color(0xFF46515C),
  Color(0xFF5C5664),
];
const List<String> _quipOpeners = <String>[
  'Today\'s sonic forecast',
  'Current music goblin memo',
  'Breaking genre news',
  'The headphone council reports',
  'Fresh dispatch from the aux cord tribunal',
];
const List<String> _quipMiddles = <String>[
  'your queue is one impulsive click away from becoming a legal issue',
  'someone out there is still defending their 47-minute bagpipe ambient cut',
  'the algorithm briefly achieved sentience and immediately asked for one more chorus',
  'your taste is giving “I know a shortcut” right before getting lost in a concept album',
  'a bassline just kicked the door open and demanded better transitions',
];
const List<String> _quipClosers = <String>[
  'Proceed with swagger.',
  'Use responsibly near the skip button.',
  'No moderators were consulted.',
  'Respectfully: play it too loud.',
  'The aux has never been safer.',
];

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

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initSupabase();
  await initAudioService();
  runApp(const ProviderScope(child: AuralisApp()));
}

class AuralisApp extends ConsumerStatefulWidget {
  const AuralisApp({super.key});

  @override
  ConsumerState<AuralisApp> createState() => _AuralisAppState();
}

class _AuralisAppState extends ConsumerState<AuralisApp>
    with WidgetsBindingObserver {
  Timer? _introFadeTimer;
  Timer? _introDismissTimer;
  bool _showIntro = true;
  bool _introFading = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(_requestNotificationPermissionIfNeeded());
    _introFadeTimer = Timer(const Duration(milliseconds: 950), () {
      if (mounted) {
        setState(() => _introFading = true);
      }
    });
    _introDismissTimer = Timer(const Duration(milliseconds: 1550), () {
      if (mounted) {
        setState(() => _showIntro = false);
      }
    });
  }

  Future<void> _requestNotificationPermissionIfNeeded() async {
    if (!Platform.isAndroid) return;
    try {
      final status = await Permission.notification.status;
      if (status.isDenied || status.isRestricted) {
        await Permission.notification.request();
      }
    } catch (_) {
      // Media notification permission stays best-effort.
    }
  }

  @override
  void dispose() {
    _introFadeTimer?.cancel();
    _introDismissTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.detached) {
      unawaited(ref.read(audioPlayerProvider.notifier).stopPlayback());
      ref.read(playbackQueueProvider.notifier).clearSession();
    }
  }

  @override
  Widget build(BuildContext context) {
    final authState = ref.watch(authProvider);
    final home = authState.isConfigured && !authState.isAuthenticated
        ? const AuthGateScreen()
        : const MainLayout();

    return MaterialApp(
      title: 'EBB',
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: _voidBlack,
        primaryColor: _accentGrey,
        colorScheme: const ColorScheme.dark(
          primary: _accentGrey,
          secondary: _accentGrey,
          surface: _surfaceGrey,
          onSurface: Colors.white,
        ),
        textTheme: GoogleFonts.spaceGroteskTextTheme(Theme.of(context).textTheme)
            .apply(bodyColor: Colors.white, displayColor: Colors.white),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
        ),
      ),
      home: Stack(
        children: [
          home,
          if (_showIntro)
            Positioned.fill(
              child: IgnorePointer(
                child: AnimatedOpacity(
                  opacity: _introFading ? 0 : 1,
                  duration: const Duration(milliseconds: 520),
                  curve: Curves.easeOutCubic,
                  child: AnimatedScale(
                    scale: _introFading ? 1.06 : 0.92,
                    duration: const Duration(milliseconds: 700),
                    curve: Curves.easeOutCubic,
                    child: Container(
                      color: Colors.black,
                      child: Center(
                        child: Image.asset(
                          'assets/branding/ebb_intro_mark.png',
                          width: 140,
                          height: 140,
                          fit: BoxFit.contain,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class AuthGateScreen extends ConsumerStatefulWidget {
  const AuthGateScreen({super.key});

  @override
  ConsumerState<AuthGateScreen> createState() => _AuthGateScreenState();
}

class _AuthGateScreenState extends ConsumerState<AuthGateScreen> {
  final TextEditingController _emailController = TextEditingController();

  @override
  void dispose() {
    _emailController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final authState = ref.watch(authProvider);
    final authNotifier = ref.read(authProvider.notifier);

    return Scaffold(
      backgroundColor: _voidBlack,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Container(
                padding: const EdgeInsets.fromLTRB(24, 28, 24, 24),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.03),
                  borderRadius: BorderRadius.circular(_radiusLarge),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.08),
                  ),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'EBB',
                      style: GoogleFonts.spaceGrotesk(
                        fontSize: 34,
                        fontWeight: FontWeight.w700,
                        color: Colors.white,
                        letterSpacing: -1.1,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      'Sign in to keep playlists, listening memory, and offline ownership scoped to your account.',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.68),
                        fontSize: 14,
                        height: 1.45,
                      ),
                    ),
                    const SizedBox(height: 24),
                    if (authState.error != null && authState.error!.isNotEmpty)
                      Container(
                        width: double.infinity,
                        margin: const EdgeInsets.only(bottom: 16),
                        padding: const EdgeInsets.all(14),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.04),
                          borderRadius: BorderRadius.circular(_radiusMedium),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: 0.08),
                          ),
                        ),
                        child: Text(
                          authState.error!,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.78),
                            fontSize: 13,
                            height: 1.4,
                          ),
                        ),
                      ),
                    SizedBox(
                      width: double.infinity,
                      child: ElevatedButton.icon(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: _surfaceGreyAlt,
                          foregroundColor: Colors.white,
                          padding: const EdgeInsets.symmetric(vertical: 16),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(_radiusMedium),
                          ),
                        ),
                        onPressed: authState.isBusy
                            ? null
                            : () => authNotifier.signInWithGoogle(),
                        icon: const Icon(Icons.login_rounded),
                        label: const Text('Continue With Google'),
                      ),
                    ),
                    const SizedBox(height: 12),
                    SizedBox(
                      width: double.infinity,
                      child: OutlinedButton.icon(
                        style: OutlinedButton.styleFrom(
                          foregroundColor: Colors.white,
                          side: BorderSide(
                            color: Colors.white.withValues(alpha: 0.14),
                          ),
                          padding: const EdgeInsets.symmetric(vertical: 16),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(_radiusMedium),
                          ),
                        ),
                        onPressed: authState.isBusy
                            ? null
                            : () => authNotifier.signInWithGitHub(),
                        icon: const Icon(Icons.code_rounded),
                        label: const Text('Continue With GitHub'),
                      ),
                    ),
                    const SizedBox(height: 20),
                    TextField(
                      controller: _emailController,
                      keyboardType: TextInputType.emailAddress,
                      style: const TextStyle(color: Colors.white),
                      decoration: InputDecoration(
                        hintText: 'Email for magic link',
                        hintStyle: TextStyle(
                          color: Colors.white.withValues(alpha: 0.36),
                        ),
                        filled: true,
                        fillColor: Colors.white.withValues(alpha: 0.04),
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(_radiusMedium),
                          borderSide: BorderSide.none,
                        ),
                      ),
                    ),
                    const SizedBox(height: 12),
                    SizedBox(
                      width: double.infinity,
                      child: TextButton(
                        onPressed: authState.isBusy
                            ? null
                            : () {
                                final email = _emailController.text.trim();
                                if (email.isEmpty) return;
                                authNotifier.sendMagicLink(email);
                              },
                        child: const Text(
                          'Send Magic Link',
                          style: TextStyle(color: Colors.white),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class MainLayout extends ConsumerStatefulWidget {
  const MainLayout({super.key});

  @override
  ConsumerState<MainLayout> createState() => _MainLayoutState();
}

class _MainLayoutState extends ConsumerState<MainLayout> {
  int _currentIndex = 0;
  final GlobalKey<_HomeScreenState> _legacyHomeKey =
      GlobalKey<_HomeScreenState>();

  Widget _buildBottomArea(bool hasActiveTrack) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        AnimatedSwitcher(
          duration: const Duration(milliseconds: 240),
          switchInCurve: Curves.easeOutCubic,
          switchOutCurve: Curves.easeInCubic,
          transitionBuilder: (child, animation) {
            return FadeTransition(
              opacity: animation,
              child: SlideTransition(
                position: animation.drive(
                  Tween<Offset>(
                    begin: const Offset(0, 0.18),
                    end: Offset.zero,
                  ),
                ),
                child: child,
              ),
            );
          },
          child: hasActiveTrack
              ? const Padding(
                  key: ValueKey('mini-player'),
                  padding: EdgeInsets.fromLTRB(18, 0, 18, 8),
                  child: MiniPlayer(),
                )
              : const SizedBox.shrink(key: ValueKey('mini-player-hidden')),
        ),
        AppBottomNavBar(
          currentIndex: _currentIndex,
          onSelected: (index) => setState(() => _currentIndex = index),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final hasActiveTrack = ref.watch(
      audioPlayerProvider.select(
        (state) => state.currentTrackName != 'No track loaded',
      ),
    );
    final pages = <Widget>[
      HomeScreen(key: _legacyHomeKey),
      const LibraryScreen(),
    ];

    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) {
        if (didPop) return;
        final handledByHome =
            (_legacyHomeKey.currentState?.handleSystemBack() ?? false);
        if (_currentIndex == 0 && handledByHome) {
          return;
        }
        if (_currentIndex != 0) {
          setState(() => _currentIndex = 0);
          return;
        }
        unawaited(ref.read(audioPlayerProvider.notifier).stopPlayback());
        ref.read(playbackQueueProvider.notifier).clearSession();
        SystemNavigator.pop();
      },
      child: Scaffold(
        backgroundColor: _voidBlack,
        body: Stack(
          children: [
            const Positioned.fill(child: ColoredBox(color: _voidBlack)),
            FadeIndexedStack(
              index: _currentIndex,
              children: pages,
            ),
            Positioned(
              right: 24,
              bottom: hasActiveTrack ? 148 : 24,
              child: const DownloadHud(),
            ),
          ],
        ),
        bottomNavigationBar: _buildBottomArea(hasActiveTrack),
      ),
    );
  }
}

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  final TextEditingController _urlController = TextEditingController();
  final FocusNode _searchFocusNode = FocusNode();
  bool _isSearching = false;
  Timer? _suggestDebounce;
  final Set<String> _prewarmedTrackIds = <String>{};
  String _lastPrimeSignature = '';
  late final String _heroQuip;

  @override
  void initState() {
    super.initState();
    _heroQuip = _buildHeroQuip();
    _urlController.addListener(() {
      final text = _urlController.text.trim();
      if (text.isNotEmpty && !_isSearching) {
        _suggestDebounce?.cancel();
        _suggestDebounce = Timer(const Duration(milliseconds: 280), () {
          if (mounted) {
            ref.read(suggestProvider.notifier).fetchSuggestions(text);
          }
        });
      } else if (text.isEmpty) {
        _suggestDebounce?.cancel();
        ref.read(suggestProvider.notifier).clear();
        ref.read(searchProvider.notifier).clear();
        ref.read(albumSearchProvider.notifier).clear();
        setState(() => _isSearching = false);
      }
    });
  }

  @override
  void dispose() {
    _suggestDebounce?.cancel();
    _urlController.dispose();
    _searchFocusNode.dispose();
    super.dispose();
  }

  Future<void> _performSearch(WidgetRef ref, [String? query]) async {
    final q = query ?? _urlController.text.trim();
    if (q.isEmpty) {
      ref.read(searchProvider.notifier).clear();
      ref.read(albumSearchProvider.notifier).clear();
      setState(() => _isSearching = false);
      return;
    }
    _urlController.text = q;
    _searchFocusNode.unfocus();
    setState(() => _isSearching = true);
    ref.read(suggestProvider.notifier).clear();
    await Future.wait([
      ref.read(searchProvider.notifier).search(q),
      ref.read(albumSearchProvider.notifier).search(q),
    ]);
  }

  void _clearSearch() {
    _urlController.clear();
    _searchFocusNode.unfocus();
    ref.read(suggestProvider.notifier).clear();
    ref.read(searchProvider.notifier).clear();
    ref.read(albumSearchProvider.notifier).clear();
    setState(() => _isSearching = false);
  }

  Future<void> _refreshContent() async {
    if (_isSearching) {
      await _performSearch(ref);
      return;
    }
    final seed = await HistoryManager.getRecommendationSeed();
    await ref.read(recommendationProvider.notifier).loadRecommendations(seed, true);
  }

  bool handleSystemBack() {
    if (hasActiveSearch) {
      _clearSearch();
      return true;
    }
    return false;
  }

  bool get hasActiveSearch =>
      _isSearching || _urlController.text.trim().isNotEmpty;

  String _buildHeroQuip() {
    final seed = DateTime.now().microsecondsSinceEpoch ^
        (_urlController.hashCode << 3) ^
        DateTime.now().second;
    final rng = math.Random(seed);
    return '${_quipOpeners[rng.nextInt(_quipOpeners.length)]}: '
        '${_quipMiddles[rng.nextInt(_quipMiddles.length)]}. '
        '${_quipClosers[rng.nextInt(_quipClosers.length)]}';
  }

  void _primeLikelyTracks(List<dynamic> tracks) {
    final idsToWarm = <String>[];
    for (final track in tracks.take(16)) {
      final id = (track['id'] ?? track['videoId'])?.toString();
      if (id == null || id.isEmpty) continue;
      if (_prewarmedTrackIds.add(id)) {
        idsToWarm.add(id);
      }
    }
    if (idsToWarm.isEmpty) return;

    final signature = idsToWarm.join('|');
    if (signature == _lastPrimeSignature) return;
    _lastPrimeSignature = signature;

    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(idsToWarm));
  }

  void _warmTrack(String? videoId) {
    if (videoId == null || videoId.isEmpty) return;
    _prewarmedTrackIds.add(videoId);
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(videoId));
  }

  Future<void> _triggerStream(dynamic track) async {
    unawaited(ref.read(playbackQueueProvider.notifier).startRadioSession(track));
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
  }

  Future<void> _openAlbum(Map<String, dynamic> album) async {
    final albumId = album['id']?.toString();
    if (albumId == null || albumId.isEmpty) return;
    unawaited(ref.read(albumDetailsProvider.notifier).fetchAlbum(albumId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => AlbumDetailsScreen(
          albumId: albumId,
          fallbackAlbum: album,
        ),
      ),
    );
  }

  Widget _buildAlbumList(
    List<Map<String, dynamic>> albums,
    bool isLoading,
  ) {
    if (albums.isEmpty && isLoading) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: CircularProgressIndicator(color: _accentGrey),
        ),
      );
    }
    if (albums.isEmpty) {
      return const SizedBox.shrink();
    }

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: albums.length,
      itemBuilder: (context, index) {
        final album = albums[index];
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(
              color: Colors.white.withValues(alpha: 0.06),
              width: 1,
            ),
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(_radiusLarge),
              onTap: () => _openAlbum(album),
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: album['thumbnail'],
                      width: 78,
                      height: 78,
                      radius: 20,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 10,
                              vertical: 5,
                            ),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.06),
                              borderRadius: BorderRadius.circular(999),
                              border: Border.all(
                                color: Colors.white.withValues(alpha: 0.08),
                              ),
                            ),
                            child: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(
                                  Icons.album_outlined,
                                  size: 12,
                                  color: Colors.white.withValues(alpha: 0.72),
                                ),
                                const SizedBox(width: 6),
                                Text(
                                  'Album',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.74),
                                    fontSize: 11,
                                    fontWeight: FontWeight.w700,
                                    letterSpacing: 0.4,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            album['title'] ?? 'Unknown Album',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            album['artist'] ?? 'Unknown Artist',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.62),
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            [
                              if ((album['year'] ?? '').toString().isNotEmpty)
                                album['year'].toString(),
                              if ((album['track_count'] ?? 0) > 0)
                                '${album['track_count']} tracks',
                            ].join(' • '),
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.42),
                              fontSize: 12,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ],
                      ),
                    ),
                    Icon(
                      Icons.chevron_right_rounded,
                      color: Colors.white.withValues(alpha: 0.52),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  List<Map<String, dynamic>> _deriveAlbumsFromTracks(List<dynamic> tracks) {
    final seen = <String>{};
    final albums = <Map<String, dynamic>>[];

    for (final rawTrack in tracks) {
      if (rawTrack is! Map) continue;

      final albumTitle =
          (rawTrack['album_title'] ?? rawTrack['album'])?.toString().trim();
      if (albumTitle == null || albumTitle.isEmpty) continue;

      final albumId = rawTrack['album_id']?.toString().trim();
      final artist =
          (rawTrack['channel'] ?? rawTrack['author'] ?? '').toString().trim();
      final key = albumId != null && albumId.isNotEmpty
          ? 'id:$albumId'
          : 'title:${albumTitle.toLowerCase()}|artist:${artist.toLowerCase()}';
      if (!seen.add(key)) continue;

      albums.add({
        'id': albumId,
        'title': albumTitle,
        'artist': artist,
        'thumbnail': rawTrack['thumbnail'],
        'year': '',
        'track_count': 0,
      });
    }

    return albums;
  }

  Widget _buildTrackList(
    List<dynamic> tracks,
    bool isLoading,
    String emptyMessage, {
    bool showTrailingLoader = false,
  }) {
    if (isLoading && tracks.isEmpty) {
      return const Center(
          child: CircularProgressIndicator(color: _accentGrey));
    }
    if (tracks.isEmpty) {
      return Center(
        child:
            Text(emptyMessage, style: const TextStyle(color: Colors.white54)),
      );
    }
    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: tracks.length + ((isLoading || showTrailingLoader) ? 1 : 0),
      itemBuilder: (context, index) {
        if (index >= tracks.length) {
          return const Center(
              child: Padding(
                  padding: EdgeInsets.all(16.0),
                  child: CircularProgressIndicator(color: _accentGrey)));
        }
        final t = tracks[index];
        final videoId = (t['id'] ?? t['videoId'])?.toString();
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.06), width: 1),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.2),
                blurRadius: 18,
                offset: const Offset(0, 10),
              )
            ]
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onTapDown: (_) => _warmTrack(videoId),
              onTap: () => _triggerStream(t),
              child: Padding(
                padding: const EdgeInsets.all(12.0),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: t['thumbnail'],
                      videoId: videoId,
                      width: 74,
                      height: 74,
                      radius: 18,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: GestureDetector(
                        onTap: () {
                          if (videoId == null || videoId.isEmpty) return;
                          _warmTrack(videoId);
                          ref.read(trackDetailsProvider.notifier).fetchDetails(
                                videoId,
                              );
                          Navigator.push(
                              context,
                              MaterialPageRoute(
                                  builder: (_) => TrackDetailsScreen(track: t)));
                        },
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(t['title'] ?? 'Unknown',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
                            const SizedBox(height: 4),
                            Text(t['channel'] ?? '',
                                maxLines: 1, style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
                          ],
                        ),
                      ),
                    ),
                    Consumer(
                      builder: (context, ref, child) {
                        final task = videoId == null
                            ? null
                            : ref.watch(downloadTaskProvider(videoId));
                        final isActive = task?.phase == DownloadPhase.active;
                        return IconButton(
                          icon: Container(
                            padding: const EdgeInsets.all(8),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.05),
                              shape: BoxShape.circle,
                            ),
                            child: isActive
                                ? SizedBox(
                                    width: 20,
                                    height: 20,
                                    child: CircularProgressIndicator(
                                      value: task!.progress > 0 ? task.progress : null,
                                      strokeWidth: 2,
                                      color: _accentGrey,
                                    ),
                                  )
                                : Icon(
                                    task?.phase == DownloadPhase.complete
                                        ? Icons.check_rounded
                                        : Icons.download_rounded,
                                    color: Colors.white70,
                                    size: 20,
                                  ),
                          ),
                          onPressed: videoId == null
                              ? null
                              : () {
                                  ref
                                      .read(downloadCenterProvider.notifier)
                                      .downloadTrack(t);
                                },
                        );
                      },
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final searchState = ref.watch(searchProvider);
    final isSearchLoading = ref.watch(searchProvider.notifier).isLoading;
    final albumSearchState = ref.watch(albumSearchProvider);
    final isAlbumSearchLoading =
        ref.watch(albumSearchProvider.notifier).isLoading;
    final fallbackAlbums =
        _isSearching ? _deriveAlbumsFromTracks(searchState) : const <Map<String, dynamic>>[];
    final displayAlbums =
        albumSearchState.isNotEmpty ? albumSearchState : fallbackAlbums;
    final featuredAlbum =
        _isSearching && displayAlbums.isNotEmpty ? displayAlbums.first : null;
    final extraAlbums = featuredAlbum == null
        ? displayAlbums
        : displayAlbums.skip(1).toList(growable: false);

    final recState = ref.watch(recommendationProvider);
    final isRecLoading = ref.watch(recommendationProvider.notifier).isLoading;
    final isRecPaginating =
        ref.watch(recommendationProvider.notifier).isPaginating;
    final suggestState = ref.watch(suggestProvider);
    final visibleTracks = _isSearching ? searchState : recState;

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primeLikelyTracks(visibleTracks);
    });

    return SafeArea(
      child: RefreshIndicator(
        color: _accentGrey,
        backgroundColor: _surfaceGreyAlt,
        onRefresh: _refreshContent,
        child: NotificationListener<ScrollNotification>(
          onNotification: (ScrollNotification scrollInfo) {
            if (!_isSearching &&
                !isRecLoading &&
                scrollInfo.metrics.pixels >=
                    scrollInfo.metrics.maxScrollExtent - 200) {
              if (recState.isNotEmpty) {
                final lastTrackId = recState.last['id'];
                ref.read(recommendationProvider.notifier).loadMore(lastTrackId);
              }
            }
            return false;
          },
          child: SingleChildScrollView(
            physics: const BouncingScrollPhysics(
              parent: AlwaysScrollableScrollPhysics(),
            ),
            padding: const EdgeInsets.all(16.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
            Semantics(
              label: _heroQuip,
              child: TextField(
                controller: _urlController,
                focusNode: _searchFocusNode,
                textInputAction: TextInputAction.search,
                style: const TextStyle(color: Colors.white),
                decoration: InputDecoration(
                  hintText: '',
                  filled: true,
                  fillColor: _surfaceGrey,
                  isDense: true,
                  contentPadding: const EdgeInsets.symmetric(vertical: 10),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(_radiusLarge),
                    borderSide: BorderSide.none,
                  ),
                  prefixIcon: _isSearching || _urlController.text.isNotEmpty
                      ? IconButton(
                          icon: const Icon(Icons.arrow_back, color: Colors.white),
                          onPressed: _clearSearch,
                        )
                      : const Icon(Icons.search, color: Colors.white54),
                  prefixIconConstraints: const BoxConstraints(
                    minWidth: 40,
                    minHeight: 40,
                  ),
                ),
                onSubmitted: (_) => _performSearch(ref),
              ),
            ),

            // Suggestions Dropdown
            if (suggestState.isNotEmpty && !_isSearching)
              Container(
                margin: const EdgeInsets.only(top: 4),
                decoration: BoxDecoration(
                  color: Colors.grey[850],
                  borderRadius: BorderRadius.circular(8),
                ),
                child: ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: suggestState.length,
                  itemBuilder: (context, i) {
                    return ListTile(
                      leading: const Icon(Icons.search, color: Colors.white54),
                      title: Text(suggestState[i],
                          style: const TextStyle(color: Colors.white)),
                      onTap: () => _performSearch(ref, suggestState[i]),
                    );
                  },
                ),
              ),

            const SizedBox(height: 20),

            // Content Area (Search vs Recommendations)
            if (_isSearching) ...[
              const Text('Search Results',
                  style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: Colors.white)),
              const SizedBox(height: 16),
              if (featuredAlbum != null) ...[
                _buildAlbumList([featuredAlbum], false),
                const SizedBox(height: 10),
              ],
              _buildTrackList(
                  searchState, isSearchLoading, "No results found."),
              if (extraAlbums.isNotEmpty || isAlbumSearchLoading) ...[
                const SizedBox(height: 24),
                const Text(
                  'More albums',
                  style: TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.bold,
                    color: Colors.white,
                  ),
                ),
                const SizedBox(height: 16),
                _buildAlbumList(extraAlbums, isAlbumSearchLoading),
              ],
            ] else ...[
              const Text('Quiet picks',
                  style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: Colors.white)),
              const SizedBox(height: 16),
              _buildTrackList(
                recState,
                isRecLoading,
                "Loading recommendations...",
                showTrailingLoader: isRecPaginating,
              ),
            ],
                const SizedBox(height: 28),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _LibraryStatChip extends StatelessWidget {
  final String label;

  const _LibraryStatChip({required this.label});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: Colors.white.withValues(alpha: 0.8),
          fontSize: 11,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.35,
        ),
      ),
    );
  }
}

class LibraryScreen extends ConsumerWidget {
  const LibraryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final authState = ref.watch(authProvider);
    final libraryAsync = ref.watch(libraryProvider);
    final playlists = ref.watch(playlistProvider);
    final offlineTracks = libraryAsync.valueOrNull ?? const <Map<String, dynamic>>[];
    final displayName =
        authState.user?.userMetadata?['full_name']?.toString() ??
            authState.user?.userMetadata?['name']?.toString() ??
            authState.user?.email?.split('@').first ??
            'Listener';
    final avatarUrl = authState.user?.userMetadata?['avatar_url']?.toString() ??
        authState.user?.userMetadata?['picture']?.toString();
    final providerName =
        authState.user?.appMetadata['provider']?.toString() ?? 'Supabase';

    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (authState.isConfigured) ...[
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(18),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.03),
                  borderRadius: BorderRadius.circular(_radiusLarge),
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.08),
                  ),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.06),
                        borderRadius: BorderRadius.circular(_radiusMedium),
                      ),
                      alignment: Alignment.center,
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(_radiusMedium),
                        child: avatarUrl != null && avatarUrl.isNotEmpty
                            ? Image.network(
                                avatarUrl,
                                fit: BoxFit.cover,
                                errorBuilder: (_, __, ___) => const Icon(
                                  Icons.person_outline_rounded,
                                  color: Colors.white,
                                ),
                              )
                            : const Icon(
                                Icons.person_outline_rounded,
                                color: Colors.white,
                              ),
                      ),
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            displayName,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            authState.user?.email ?? 'Signed in',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.56),
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(height: 10),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: [
                              _LibraryStatChip(label: providerName.toUpperCase()),
                              _LibraryStatChip(label: '${playlists.length} playlists'),
                              _LibraryStatChip(label: '${offlineTracks.length} offline'),
                            ],
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    TextButton(
                      onPressed: authState.isBusy
                          ? null
                          : () async {
                              await ref
                                  .read(audioPlayerProvider.notifier)
                                  .stopPlayback();
                              ref
                                  .read(playbackQueueProvider.notifier)
                                  .clearSession();
                              await ref.read(authProvider.notifier).signOut();
                            },
                      child: const Text(
                        'Sign Out',
                        style: TextStyle(color: Colors.white),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 24),
            ],
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Playlists', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
                TextButton.icon(
                  onPressed: () {
                    final ctrl = TextEditingController();
                    showGlassDialog(
                      context: context,
                      title: 'New Playlist',
                      content: TextField(
                        controller: ctrl,
                        style: const TextStyle(color: Colors.white),
                        decoration: const InputDecoration(hintText: 'Playlist Name', hintStyle: TextStyle(color: Colors.white54)),
                      ),
                      actions: [
                        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                        TextButton(onPressed: () {
                          if (ctrl.text.trim().isNotEmpty) {
                            ref.read(playlistProvider.notifier).createPlaylist(ctrl.text.trim());
                          }
                          Navigator.pop(context);
                        }, child: const Text('Create', style: TextStyle(color: Colors.white)))
                      ]
                    );
                  }, 
                  icon: const Icon(Icons.add, color: Colors.white), 
                  label: const Text('Create', style: TextStyle(color: Colors.white))
                )
              ],
            ),
            const SizedBox(height: 16),
            if (playlists.isEmpty)
              const Text('No playlists yet. Create one to get started!', style: TextStyle(color: Colors.white54, fontSize: 16))
            else
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: playlists.length,
                itemBuilder: (context, index) {
                  final p = playlists[index];
                  return Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.03),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                      boxShadow: [
                        BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))
                      ]
                    ),
                    child: ListTile(
                      onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => PlaylistDetailScreen(playlistId: p.id))),
                      leading: PlaylistArtworkView(playlist: p),
                      title: Text(p.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
                      subtitle: Text('${p.tracks.length} tracks', style: const TextStyle(color: Colors.white54)),
                      trailing: PopupMenuButton<String>(
                        icon: const Icon(Icons.more_vert, color: Colors.white70),
                        color: Colors.grey[900],
                        onSelected: (val) {
                          if (val == 'delete') {
                            ref.read(playlistProvider.notifier).deletePlaylist(p.id);
                          } else if (val == 'artwork') {
                            showPlaylistArtworkDialog(
                              context: context,
                              playlist: p,
                            );
                          } else if (val == 'rename') {
                            final ctrl = TextEditingController(text: p.name);
                            showGlassDialog(
                              context: context,
                              title: 'Rename Playlist',
                              content: TextField(
                                controller: ctrl,
                                style: const TextStyle(color: Colors.white),
                              ),
                              actions: [
                                TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                                TextButton(onPressed: () {
                                  if (ctrl.text.trim().isNotEmpty) {
                                    ref.read(playlistProvider.notifier).renamePlaylist(p.id, ctrl.text.trim());
                                  }
                                  Navigator.pop(context);
                                }, child: const Text('Save', style: TextStyle(color: Colors.white)))
                              ]
                            );
                          }
                        },
                        itemBuilder: (BuildContext context) => [
                          const PopupMenuItem(value: 'artwork', child: Text('Artwork', style: TextStyle(color: Colors.white))),
                          const PopupMenuItem(value: 'rename', child: Text('Rename', style: TextStyle(color: Colors.white))),
                          const PopupMenuItem(value: 'delete', child: Text('Delete', style: TextStyle(color: Colors.red))),
                        ],
                      ),
                    ),
                  );
                },
              ),
            
            const SizedBox(height: 32),
            const Text('Offline tracks', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
            const SizedBox(height: 16),
            
            libraryAsync.when(
              data: (files) {
                if (files.isEmpty) {
                  return const Text('Your Offline Library is empty.', style: TextStyle(color: Colors.white54, fontSize: 16));
                }
                return ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: files.length,
                  itemBuilder: (context, index) {
                    final track = files[index];
                    final name = track['title'] ?? 'Unknown';
                    final path = track['local_path'];
                    final videoId =
                        (track['video_id'] ?? track['id'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(16),
                        border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                        boxShadow: [BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))]
                      ),
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(16),
                          onTap: () async {
                            final loaded = await ref
                                .read(playbackQueueProvider.notifier)
                                .startLocalSession(
                                  track: track,
                                  path: path,
                                );
                            if (!context.mounted) return;
                            if (!loaded) {
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(
                                  content: Text(
                                      'This downloaded track could not be loaded for playback.'),
                                  duration: Duration(seconds: 2),
                                ),
                              );
                              return;
                            }
                            Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                          },
                          child: Padding(
                            padding: const EdgeInsets.all(12.0),
                            child: Row(
                              children: [
                                AppArtwork(
                                  thumbnail: track['thumbnail'],
                                  videoId: videoId,
                                  width: 70,
                                  height: 70,
                                  radius: 16,
                                ),
                                const SizedBox(width: 16),
                                Expanded(
                                  child: Column(
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      Text(name, maxLines: 1, overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
                                      const SizedBox(height: 4),
                                      Text(track['author'] ?? track['artist'] ?? '', maxLines: 1, style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
                                    ],
                                  ),
                                ),
                                Column(
                                  crossAxisAlignment: CrossAxisAlignment.end,
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  children: [
                                    IconButton(
                                      icon: Container(
                                         padding: const EdgeInsets.all(6),
                                         decoration: BoxDecoration(color: Colors.redAccent.withValues(alpha: 0.1), shape: BoxShape.circle),
                                         child: const Icon(Icons.delete_outline_rounded, color: Colors.redAccent, size: 20)
                                      ),
                                      onPressed: () {
                                        try {
                                          File(path).deleteSync();
                                          final jsonPath = path.replaceAll('.mp3', '.json');
                                          if (File(jsonPath).existsSync()) File(jsonPath).deleteSync();
                                          unawaited(removeCloudLibraryTrack(videoId));
                                          ref.invalidate(libraryProvider);
                                        } catch (e) {
                                          // Ignored mapping
                                        }
                                      },
                                    ),
                                    if (track['filesize'] != null && track['filesize'] > 0)
                                      Padding(
                                        padding: const EdgeInsets.only(right: 8.0),
                                        child: Text('${(track['filesize'] / 1024 / 1024).toStringAsFixed(1)} MB',
                                          style: TextStyle(color: Colors.white.withValues(alpha: 0.3), fontSize: 10, fontWeight: FontWeight.w600),
                                        ),
                                      ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    );
                  },
                );
              },
              loading: () => const Center(child: CircularProgressIndicator(color: Colors.white)),
              error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
            ),
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}

class PlaylistDetailScreen extends ConsumerStatefulWidget {
  final String playlistId;
  const PlaylistDetailScreen({super.key, required this.playlistId});
  @override
  ConsumerState<PlaylistDetailScreen> createState() => _PlaylistDetailScreenState();
}

class _PlaylistDetailScreenState extends ConsumerState<PlaylistDetailScreen> {
  final _searchCtrl = TextEditingController();
  List<dynamic> _searchResults = [];
  bool _isSearching = false;
  final Set<String> _primedPlaylistIds = <String>{};
  int _searchRequestVersion = 0;

  void _primePlaylistTracks(Iterable<dynamic> tracks, {int limit = 8}) {
    final idsToWarm = <String>[];
    for (final track in tracks.take(limit)) {
      final videoId = (track['id'] ?? track['videoId'])?.toString();
      if (videoId == null || videoId.isEmpty) continue;
      if (_primedPlaylistIds.add(videoId)) {
        idsToWarm.add(videoId);
      }
    }
    if (idsToWarm.isEmpty) return;
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(idsToWarm));
  }

  Future<void> _search(String q) async {
    if (q.isEmpty) return;
    final requestVersion = ++_searchRequestVersion;
    setState(() => _isSearching = true);
    try {
      final res = await http.post(
        buildProxyUri('/search'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({"query": q}),
      );
      if (requestVersion != _searchRequestVersion) return;
      if (res.statusCode == 200) {
        final results = jsonDecode(res.body)['results'] ?? [];
        setState(() => _searchResults = results);
        _primePlaylistTracks(results, limit: 8);
      } else {
        setState(() => _searchResults = []);
      }
    } catch (e) {
      // Ignored
    }
    if (requestVersion != _searchRequestVersion || !mounted) return;
    setState(() => _isSearching = false);
  }

  @override
  Widget build(BuildContext context) {
    final playlists = ref.watch(playlistProvider);
    final playlist = playlists.firstWhere((p) => p.id == widget.playlistId, orElse: () => Playlist(id: '', name: 'Deleted Playlist', tracks: []));
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primePlaylistTracks(playlist.tracks);
    });

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(backgroundColor: Colors.transparent, title: Text(playlist.name, style: const TextStyle(color: Colors.white))),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.03),
                borderRadius: BorderRadius.circular(_radiusLarge),
                border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
              ),
              child: Row(
                children: [
                  PlaylistArtworkView(
                    playlist: playlist,
                    size: 96,
                    radius: 22,
                    onTap: () => showPlaylistArtworkDialog(
                      context: context,
                      playlist: playlist,
                    ),
                  ),
                  const SizedBox(width: 18),
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
                            fontSize: 24,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          '${playlist.tracks.length} tracks'
                          '${playlist.tracks.any(isTrackHidden) ? ' • ${playlist.tracks.where(isTrackHidden).length} skipped' : ''}',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.56),
                            fontSize: 13,
                          ),
                        ),
                        const SizedBox(height: 14),
                        TextButton.icon(
                          onPressed: () => showPlaylistArtworkDialog(
                            context: context,
                            playlist: playlist,
                          ),
                          icon: const Icon(Icons.photo_outlined, size: 18),
                          label: const Text('Edit artwork'),
                          style: TextButton.styleFrom(
                            foregroundColor: Colors.white,
                            backgroundColor: Colors.white.withValues(alpha: 0.05),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 14,
                              vertical: 12,
                            ),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(18),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _searchCtrl,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: "Let's find something for your playlist",
                hintStyle: const TextStyle(color: Colors.white54),
                filled: true,
                fillColor: _surfaceGrey,
                suffixIcon: IconButton(icon: const Icon(Icons.search, color: Colors.white), onPressed: () => _search(_searchCtrl.text)),
              ),
              onSubmitted: _search,
            ),
            const SizedBox(height: 16),
            if (_isSearching) const Center(child: CircularProgressIndicator(color: Colors.white)),
            if (_searchResults.isNotEmpty) ...[
              const Text('Search Results', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              ListView.builder(
                 shrinkWrap: true,
                 physics: const NeverScrollableScrollPhysics(),
                 itemCount: _searchResults.length,
                 itemBuilder: (context, i) {
                   final t = _searchResults[i];
                   final videoId = (t['id'] ?? t['videoId'])?.toString();
                   return ListTile(
                     leading: AppArtwork(
                       thumbnail: t['thumbnail'],
                       videoId: videoId,
                       width: 50,
                       height: 50,
                       radius: 14,
                     ),
                     title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                     subtitle: Text(t['channel'] ?? '', style: const TextStyle(color: Colors.white54)),
                     trailing: IconButton(
                       icon: const Icon(Icons.add_circle_outline, color: Colors.white),
                       onPressed: () {
                         ref.read(playlistProvider.notifier).addTrackToPlaylist(widget.playlistId, t);
                         ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added to ${playlist.name}'), duration: const Duration(seconds: 1)));
                       }
                     )
                   );
                 }
              ),
              const Divider(color: Colors.white24, height: 32),
            ],
            
            const Text('Playlist Tracks', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            if (playlist.tracks.isEmpty) const Text('No tracks added yet.', style: TextStyle(color: Colors.white54)),
            ListView.builder(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemCount: playlist.tracks.length,
              itemBuilder: (context, i) {
                 final t = playlist.tracks[i];
                 final videoId = (t['id'] ?? t['videoId'])?.toString();
                 return ListTile(
                    onTap: () {
                        unawaited(ref.read(playbackQueueProvider.notifier).startPlaylistSession(
                          playlistId: widget.playlistId,
                          playlistName: playlist.name,
                          tracks: playlist.tracks,
                          currentTrack: t,
                        ));
                        Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                    },
                    leading: AppArtwork(
                      thumbnail: t['thumbnail'],
                      videoId: videoId,
                      width: 50,
                      height: 50,
                      radius: 14,
                    ),
                    title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                    subtitle: Text(t['channel'] ?? t['author'] ?? '', style: const TextStyle(color: Colors.white54)),
                    trailing: PopupMenuButton<String>(
                      icon: const Icon(Icons.more_vert, color: Colors.white70),
                      color: Colors.grey[900],
                      onSelected: (val) {
                        if (val == 'remove') {
                          ref.read(playlistProvider.notifier).removeTrackFromPlaylist(widget.playlistId, t['id'] ?? t['videoId']);
                        }
                      },
                      itemBuilder: (_) => [const PopupMenuItem(value: 'remove', child: Text('Remove from Playlist', style: TextStyle(color: Colors.red)))]
                    )
                 );
              }
            )
          ]
        )
      )
    );
  }
}

class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final videoId = playerState.videoId;

    return GestureDetector(
      onTap: () {
        Navigator.of(context)
            .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
      },
      child: ClipRRect(
        borderRadius: BorderRadius.circular(_radiusMedium),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 15, sigmaY: 15),
          child: Container(
            height: 78,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(_radiusLarge),
              border: Border.all(color: Colors.white.withValues(alpha: 0.08), width: 1),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.25),
                  blurRadius: 24,
                  offset: const Offset(0, 12),
                )
              ]
            ),
            child: Column(
              children: [
                Expanded(
                  child: Row(
                    children: [
                      const SizedBox(width: 8),
                      AppArtwork(
                        thumbnail: playerState.thumbnail,
                        videoId: videoId,
                        width: 54,
                        height: 54,
                        radius: _radiusMedium,
                        heroTag: 'album_art_${playerState.currentTrackName}',
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Text(
                              playerState.currentTrackName,
                              style: const TextStyle(
                                  color: Colors.white, fontWeight: FontWeight.w700, fontSize: 15),
                              overflow: TextOverflow.ellipsis,
                            ),
                            const SizedBox(height: 2),
                            if (playerState.duration > 0 || playerState.artist != null)
                              Text(
                                  playerState.artist ?? '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')} / ${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                                  style: TextStyle(
                                      color: Colors.white.withValues(alpha: 0.6), fontSize: 12, fontWeight: FontWeight.w500)),
                          ],
                        ),
                      ),
                      // Play/Pause Button
                      GestureDetector(
                        onTap: () {
                          unawaited(
                            playerState.isPlaying
                                ? audioNotifier.pause()
                                : audioNotifier.play(),
                          );
                        },
                        child: AnimatedContainer(
                          duration: const Duration(milliseconds: 220),
                          margin: const EdgeInsets.only(right: 12),
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                             color: playerState.isPlaying
                                 ? Colors.white.withValues(alpha: 0.16)
                                 : Colors.white.withValues(alpha: 0.08),
                             shape: BoxShape.circle,
                          ),
                          child: playerState.isDownloading
                              ? const SizedBox(
                                  width: 28,
                                  height: 28,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                    color: Colors.white,
                                  ),
                                )
                              : Icon(
                                  playerState.isPlaying
                                      ? Icons.pause_rounded
                                      : Icons.play_arrow_rounded,
                                  color: Colors.white,
                                  size: 28,
                                ),
                        ),
                      ),
                    ],
                  ),
                ),
                // Progress Bar
                if (playerState.duration > 0)
                  ClipRRect(
                    borderRadius: const BorderRadius.only(
                      bottomLeft: Radius.circular(_radiusMedium),
                      bottomRight: Radius.circular(_radiusMedium),
                    ),
                    child: LinearProgressIndicator(
                      value: (playerState.currentPosition / playerState.duration)
                          .clamp(0.0, 1.0),
                      backgroundColor: Colors.transparent,
                      color: _accentGrey,
                      minHeight: 2,
                    ),
                  )
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class FullPlayerScreen extends ConsumerStatefulWidget {
  const FullPlayerScreen({super.key});

  @override
  ConsumerState<FullPlayerScreen> createState() => _FullPlayerScreenState();
}

class _FullPlayerScreenState extends ConsumerState<FullPlayerScreen> {
  static const double _queueInitialSize = 0.72;
  static const double _queueMinSize = 0.44;
  static const double _queueExpandedSize = 0.92;

  double? _dragValue;
  late final PageController _pageController;
  late final DraggableScrollableController _queueSheetController;
  late final TextEditingController _queueSearchController;
  final Map<int, GlobalKey> _lyricsLineKeys = <int, GlobalKey>{};
  final Set<String> _removingQueueTrackIds = <String>{};
  Timer? _queueSearchDebounce;
  List<Map<String, dynamic>> _queueSearchResults = const [];
  bool _isQueueSearchLoading = false;
  bool _isQueueSheetOpen = false;
  int _queueSearchRequestVersion = 0;
  String? _lastLyricsVideoId;
  StateSetter? _queueSheetStateSetter;
  int _activePage = 0;
  int _lastSyncedLyricIndex = -1;

  @override
  void initState() {
    super.initState();
    _pageController = PageController(initialPage: 0);
    _queueSheetController = DraggableScrollableController();
    _queueSearchController = TextEditingController()
      ..addListener(_onQueueSearchChanged);
  }

  @override
  void dispose() {
    _queueSearchDebounce?.cancel();
    _queueSearchController
      ..removeListener(_onQueueSearchChanged)
      ..dispose();
    _pageController.dispose();
    _queueSheetController.dispose();
    super.dispose();
  }

  void _openLyricsPanel() {
    if (!_pageController.hasClients) return;
    _lastSyncedLyricIndex = -1;
    _dismissQueueSheetIfOpen();
    _pageController.nextPage(
      duration: const Duration(milliseconds: 340),
      curve: Curves.easeOutCubic,
    );
  }

  void _closeLyricsPanel() {
    if (!_pageController.hasClients) return;
    _pageController.previousPage(
      duration: const Duration(milliseconds: 320),
      curve: Curves.easeOutCubic,
    );
  }

  void _dismissQueueSheetIfOpen() {
    if (!_isQueueSheetOpen) return;
    Navigator.of(context).maybePop();
  }

  void _refreshQueueSheet() {
    _queueSheetStateSetter?.call(() {});
  }

  void _openQueueSheet() {
    if (_isQueueSheetOpen || !mounted) return;
    _clearQueueSearch();
    setState(() => _isQueueSheetOpen = true);
    unawaited(
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Colors.transparent,
        barrierColor: Colors.black.withValues(alpha: 0.28),
        builder: (sheetContext) {
          return StatefulBuilder(
            builder: (context, setSheetState) {
              _queueSheetStateSetter = setSheetState;
              return Consumer(
                builder: (context, ref, _) {
                  final queueState = ref.watch(playbackQueueProvider);
                  final queueNotifier = ref.read(playbackQueueProvider.notifier);
                  return DraggableScrollableSheet(
                    controller: _queueSheetController,
                    initialChildSize: _queueInitialSize,
                    minChildSize: _queueMinSize,
                    maxChildSize: _queueExpandedSize,
                    expand: false,
                    snap: true,
                    snapSizes: const [_queueMinSize, _queueExpandedSize],
                    builder: (context, scrollController) => _buildQueueSheet(
                      context,
                      queueState,
                      queueNotifier,
                      scrollController,
                      onDismiss: () => Navigator.of(sheetContext).pop(),
                    ),
                  );
                },
              );
            },
          );
        },
      ).whenComplete(() {
        _clearQueueSearch();
        _queueSheetStateSetter = null;
        if (mounted) {
          setState(() => _isQueueSheetOpen = false);
        }
      }),
    );
  }

  void _onQueueSearchChanged() {
    final query = _queueSearchController.text.trim();
    _queueSearchDebounce?.cancel();
    if (query.isEmpty) {
      if (_queueSearchResults.isNotEmpty || _isQueueSearchLoading) {
        setState(() {
          _queueSearchResults = const [];
          _isQueueSearchLoading = false;
        });
        _refreshQueueSheet();
      }
      return;
    }

    _queueSearchDebounce = Timer(const Duration(milliseconds: 260), () {
      unawaited(_searchQueueTracks(query));
    });
  }

  Future<void> _searchQueueTracks(String query) async {
    final requestVersion = ++_queueSearchRequestVersion;
    if (mounted) {
      setState(() => _isQueueSearchLoading = true);
      _refreshQueueSheet();
    }
    try {
      final res = await appHttpClient
          .post(
            buildProxyUri('/search'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({"query": query, "limit": 8}),
          )
          .timeout(const Duration(seconds: 10));
      if (!mounted || requestVersion != _queueSearchRequestVersion) return;

      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
        final results = (payload['results'] as List<dynamic>? ?? const []);
        setState(() {
          _queueSearchResults = results
              .map((entry) => Map<String, dynamic>.from(entry as Map))
              .toList(growable: false);
        });
        _refreshQueueSheet();
      } else {
        setState(() => _queueSearchResults = const []);
        _refreshQueueSheet();
      }
    } catch (_) {
      if (!mounted || requestVersion != _queueSearchRequestVersion) return;
      setState(() => _queueSearchResults = const []);
      _refreshQueueSheet();
    } finally {
      if (mounted && requestVersion == _queueSearchRequestVersion) {
        setState(() => _isQueueSearchLoading = false);
        _refreshQueueSheet();
      }
    }
  }

  void _clearQueueSearch() {
    _queueSearchDebounce?.cancel();
    _queueSearchRequestVersion++;
    if (_queueSearchController.text.isNotEmpty) {
      _queueSearchController.clear();
    }
    if (_queueSearchResults.isNotEmpty || _isQueueSearchLoading) {
      setState(() {
        _queueSearchResults = const [];
        _isQueueSearchLoading = false;
      });
      _refreshQueueSheet();
    }
  }

  String _formatSleepTimerLabel(int remainingSeconds) {
    if (remainingSeconds <= 0) return 'Off';
    final hours = remainingSeconds ~/ 3600;
    final minutes = (remainingSeconds % 3600) ~/ 60;
    if (hours > 0) {
      return minutes > 0 ? '${hours}h ${minutes}m' : '${hours}h';
    }
    return '${minutes}m';
  }

  Future<void> _showSleepTimerSheet(
    BuildContext context,
    AudioPlayerNotifier audioNotifier,
    PlayerState playerState,
  ) async {
    final initialMinutes = playerState.sleepTimerRemainingSeconds > 0
        ? (playerState.sleepTimerRemainingSeconds / 60).clamp(5, 180).round()
        : 30;

    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: _surfaceGreyAlt,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(_radiusLarge)),
      ),
      builder: (sheetContext) {
        double sliderMinutes = initialMinutes.toDouble();
        return StatefulBuilder(
          builder: (context, setSheetState) {
            final formattedValue = _formatSleepTimerLabel(
              Duration(minutes: sliderMinutes.round()).inSeconds,
            );
            return SafeArea(
              top: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Sleep timer',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      playerState.sleepTimerRemainingSeconds > 0
                          ? 'Currently ends in ${_formatSleepTimerLabel(playerState.sleepTimerRemainingSeconds)}.'
                          : 'Stop playback after a set time.',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.58),
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 24),
                    Row(
                      children: [
                        Text(
                          'Off',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.48),
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const Spacer(),
                        Text(
                          formattedValue,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: _accentGrey,
                        inactiveTrackColor:
                            Colors.white.withValues(alpha: 0.12),
                        thumbColor: Colors.white,
                        overlayColor: Colors.white.withValues(alpha: 0.08),
                        trackHeight: 3,
                      ),
                      child: Slider(
                        min: 5,
                        max: 180,
                        divisions: 35,
                        value: sliderMinutes,
                        onChanged: (value) {
                          setSheetState(() => sliderMinutes = value);
                        },
                      ),
                    ),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: TextButton(
                            onPressed: () {
                              audioNotifier.setSleepTimer(null);
                              Navigator.of(sheetContext).pop();
                            },
                            child: const Text(
                              'Turn Off',
                              style: TextStyle(color: Colors.white70),
                            ),
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: TextButton(
                            onPressed: () {
                              audioNotifier.setSleepTimer(
                                Duration(minutes: sliderMinutes.round()),
                              );
                              Navigator.of(sheetContext).pop();
                            },
                            child: const Text(
                              'Set Timer',
                              style: TextStyle(color: Colors.white),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            );
          },
        );
      },
    );
  }

  bool _isTrackInQueue(PlaybackQueueState queueState, String? trackId) {
    if (trackId == null || trackId.isEmpty) return false;
    return queueState.queue.any((track) => extractTrackId(track) == trackId);
  }

  Future<void> _animateQueueRemoval(
    String trackId,
    VoidCallback onRemove,
  ) async {
    if (_removingQueueTrackIds.contains(trackId)) return;
    setState(() => _removingQueueTrackIds.add(trackId));
    _refreshQueueSheet();
    await Future.delayed(const Duration(milliseconds: 240));
    if (!mounted) return;
    onRemove();
    setState(() => _removingQueueTrackIds.remove(trackId));
    _refreshQueueSheet();
  }

  void _ensureLyricsLoaded(String? videoId) {
    if (_lastLyricsVideoId == videoId) return;
    _lastLyricsVideoId = videoId;
    _lyricsLineKeys.clear();
    _lastSyncedLyricIndex = -1;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (videoId == null || videoId.isEmpty) {
        ref.read(lyricsProvider.notifier).clear();
        return;
      }
      unawaited(ref.read(lyricsProvider.notifier).fetchLyrics(videoId));
    });
  }

  int _activeLyricsIndex(
    TrackLyricsState lyricsState,
    PlayerState playerState,
  ) {
    if (!lyricsState.hasLyrics || lyricsState.lines.isEmpty) return -1;

    final currentMs = playerState.currentPositionMs;
    if (lyricsState.hasTimestamps) {
      for (var i = 0; i < lyricsState.lines.length; i++) {
        final line = lyricsState.lines[i];
        final start = line.startTimeMs ?? 0;
        final end = line.endTimeMs ?? (start + 2500);
        if (currentMs >= start && currentMs < end) {
          return i;
        }
      }
      for (var i = lyricsState.lines.length - 1; i >= 0; i--) {
        final line = lyricsState.lines[i];
        if (line.startTimeMs != null && currentMs >= line.startTimeMs!) {
          return i;
        }
      }
      return -1;
    }

    if (playerState.duration <= 0) return 0;
    final progress =
        (playerState.currentPosition / playerState.duration).clamp(0.0, 0.999);
    return (progress * lyricsState.lines.length)
        .floor()
        .clamp(0, lyricsState.lines.length - 1);
  }

  GlobalKey _lyricKeyFor(int index) =>
      _lyricsLineKeys.putIfAbsent(index, GlobalKey.new);

  void _syncLyricsScroll(int activeIndex) {
    if (_activePage != 1 || activeIndex < 0) return;
    if (_lastSyncedLyricIndex == activeIndex) return;
    _lastSyncedLyricIndex = activeIndex;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final targetContext = _lyricKeyFor(activeIndex).currentContext;
      if (targetContext == null) return;
      final renderObject = targetContext.findRenderObject();
      if (renderObject is! RenderObject || !renderObject.attached) return;
      if (Scrollable.maybeOf(targetContext) == null) return;
      try {
        Scrollable.ensureVisible(
          targetContext,
          duration: const Duration(milliseconds: 420),
          curve: Curves.easeOutCubic,
          alignment: 0.28,
        );
      } catch (_) {
        // The line may have been detached by a fast page transition.
      }
    });
  }

  int _resolvedQueueIndex(PlaybackQueueState queueState) {
    if (queueState.queue.isEmpty) return -1;
    final activeTrackId = queueState.currentTrackId;
    if (activeTrackId != null && activeTrackId.isNotEmpty) {
      final matchingIndex = queueState.queue.indexWhere(
        (track) => extractTrackId(track) == activeTrackId,
      );
      if (matchingIndex >= 0) {
        return matchingIndex;
      }
    }
    return queueState.currentIndex.clamp(0, queueState.queue.length - 1).toInt();
  }

  Map<String, dynamic>? _currentQueueTrack(PlaybackQueueState queueState) {
    final resolvedIndex = _resolvedQueueIndex(queueState);
    if (resolvedIndex < 0 || resolvedIndex >= queueState.queue.length) {
      return null;
    }
    return queueState.queue[resolvedIndex];
  }

  Map<String, dynamic>? _nextUpTrack(PlaybackQueueState queueState) {
    final resolvedIndex = _resolvedQueueIndex(queueState);
    for (var i = resolvedIndex + 1; i < queueState.queue.length; i++) {
      final track = queueState.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      return track;
    }
    return queueState.recommendations.isNotEmpty
        ? queueState.recommendations.first
        : null;
  }

  Widget _buildPanelTab({
    required IconData icon,
    required VoidCallback onTap,
    required bool pointLeft,
  }) {
    return GestureDetector(
      onTap: onTap,
        child: ClipPath(
          clipper: _LyricsTabClipper(pointLeft: pointLeft),
          child: Container(
            width: 28,
            height: 74,
            decoration: BoxDecoration(
              color: _surfaceGreyAlt.withValues(alpha: 0.96),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.3),
                blurRadius: 18,
                offset: const Offset(0, 8),
              ),
            ],
          ),
          child: Center(
            child: Icon(
              icon,
              color: Colors.white.withValues(alpha: 0.82),
              size: 14,
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildQueueTile({
    required BuildContext context,
    required Map<String, dynamic> track,
    required bool isActive,
    required VoidCallback onTap,
    VoidCallback? onRemove,
    VoidCallback? onAddToPlaylist,
    String? trailingLabel,
    bool isDimmed = false,
    bool isRemoving = false,
    Widget? trailing,
  }) {
    final videoId = extractTrackId(track);
    final tile = Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: isActive
            ? Colors.white.withValues(alpha: 0.08)
            : Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(_radiusLarge),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
      ),
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 220),
        opacity: isDimmed ? 0.34 : 1,
        child: ListTile(
          contentPadding:
              const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
          onTap: onTap,
          leading: AppArtwork(
            thumbnail: track['thumbnail'],
            videoId: videoId,
            width: 58,
            height: 58,
            radius: _radiusMedium,
          ),
          title: Text(
            track['title'] ?? 'Unknown Track',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: isDimmed
                  ? Colors.white.withValues(alpha: 0.48)
                  : Colors.white,
              fontWeight: isActive ? FontWeight.w700 : FontWeight.w600,
            ),
          ),
          subtitle: Text(
            track['channel'] ?? track['author'] ?? '',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: Colors.white.withValues(alpha: isDimmed ? 0.3 : 0.54),
              fontSize: 12,
            ),
          ),
          trailing: trailing ??
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (trailingLabel != null) ...[
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 6,
                      ),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.08),
                        borderRadius: BorderRadius.circular(999),
                      ),
                      child: Text(
                        trailingLabel,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.8),
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    const SizedBox(width: 6),
                  ],
                  if (onAddToPlaylist != null)
                    IconButton(
                      onPressed: onAddToPlaylist,
                      icon: Icon(
                        Icons.playlist_add_rounded,
                        color: Colors.white.withValues(alpha: 0.72),
                      ),
                    ),
                  if (onRemove != null)
                    IconButton(
                      onPressed: onRemove,
                      icon: Icon(
                        Icons.remove_circle_outline_rounded,
                        color: Colors.white.withValues(alpha: 0.62),
                      ),
                    ),
                ],
              ),
        ),
      ),
    );

    return TweenAnimationBuilder<double>(
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeInOutCubic,
      tween: Tween<double>(begin: 1, end: isRemoving ? 0 : 1),
      builder: (context, value, child) {
        return ClipRect(
          child: Align(
            heightFactor: value.clamp(0, 1),
            child: Opacity(
              opacity: value.clamp(0, 1),
              child: Transform.translate(
                offset: Offset((1 - value) * -30, 0),
                child: child,
              ),
            ),
          ),
        );
      },
      child: tile,
    );
  }

  Widget _buildQueueSheet(
    BuildContext context,
    PlaybackQueueState queueState,
    PlaybackQueueNotifier queueNotifier,
    ScrollController scrollController,
    {required VoidCallback onDismiss}
  ) {
    final queueTitle = queueState.mode == PlaybackQueueMode.playlist
        ? (queueState.playlistName?.isNotEmpty == true
            ? queueState.playlistName!
            : 'Queue')
        : 'Queue';
    final header = GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onDismiss,
      child: Container(
        padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.96),
          borderRadius: const BorderRadius.vertical(
            top: Radius.circular(_radiusLarge),
          ),
          border: Border(
            bottom: BorderSide(color: Colors.white.withValues(alpha: 0.06)),
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 42,
              height: 4,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.16),
                borderRadius: BorderRadius.circular(999),
              ),
            ),
            const SizedBox(height: 14),
            Row(
              children: [
                Text(
                  queueTitle,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );

    final resolvedCurrentIndex = _resolvedQueueIndex(queueState);
    final currentTrack = _currentQueueTrack(queueState);
    final earlierQueue = resolvedCurrentIndex > 0
        ? queueState.queue
            .take(resolvedCurrentIndex)
            .where((track) {
              final trackId = extractTrackId(track);
              if (trackId == null || trackId == queueState.currentTrackId) {
                return false;
              }
              return !queueState.playedTrackIds.contains(trackId);
            })
            .toList(growable: false)
        : const <Map<String, dynamic>>[];
    final playedQueue = queueState.queue
        .where((track) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId == queueState.currentTrackId) {
            return false;
          }
          return queueState.playedTrackIds.contains(trackId);
        })
        .toList(growable: false);
    final upcomingQueue = resolvedCurrentIndex >= 0 &&
            resolvedCurrentIndex + 1 < queueState.queue.length
        ? queueState.queue.sublist(resolvedCurrentIndex + 1)
        : const <Map<String, dynamic>>[];
    final queueChildren = <Widget>[];
    final searchQuery = _queueSearchController.text.trim();
    queueChildren.add(
      Container(
        margin: const EdgeInsets.only(bottom: 16),
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 12),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.03),
          borderRadius: BorderRadius.circular(_radiusLarge),
          border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _queueSearchController,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: '',
                hintStyle: TextStyle(
                  color: Colors.white.withValues(alpha: 0.34),
                ),
                filled: true,
                fillColor: Colors.white.withValues(alpha: 0.04),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 12,
                ),
                prefixIcon: Icon(
                  Icons.search_rounded,
                  color: Colors.white.withValues(alpha: 0.48),
                ),
                suffixIcon: _queueSearchController.text.trim().isEmpty
                    ? (_isQueueSearchLoading
                        ? const Padding(
                            padding: EdgeInsets.all(14),
                            child: SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: _accentGrey,
                              ),
                            ),
                          )
                        : null)
                    : IconButton(
                        onPressed: _clearQueueSearch,
                        icon: Icon(
                          Icons.close_rounded,
                          color: Colors.white.withValues(alpha: 0.5),
                        ),
                      ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(_radiusMedium),
                  borderSide: BorderSide.none,
                ),
              ),
            ),
            if (searchQuery.isNotEmpty ||
                _isQueueSearchLoading ||
                _queueSearchResults.isNotEmpty) ...[
              const SizedBox(height: 12),
              if (_queueSearchResults.isEmpty && _isQueueSearchLoading)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: Center(
                    child: CircularProgressIndicator(color: _accentGrey),
                  ),
                )
              else if (_queueSearchResults.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    'No songs found for "$searchQuery".',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.46),
                      fontSize: 13,
                    ),
                  ),
                )
              else
                ..._queueSearchResults.take(6).map((track) {
                  final videoId = extractTrackId(track);
                  final queueIndex = videoId == null
                      ? -1
                      : queueState.queue.indexWhere(
                          (entry) => extractTrackId(entry) == videoId,
                        );
                  final isQueued = _isTrackInQueue(queueState, videoId);
                  final isCurrent =
                      videoId != null && videoId == queueState.currentTrackId;
                  return _buildQueueTile(
                    context: context,
                    track: track,
                    isActive: isCurrent,
                    onTap: () {
                      if (isQueued) {
                        unawaited(queueNotifier.playQueueIndex(queueIndex));
                        return;
                      }
                      unawaited(queueNotifier.enqueueTrack(track));
                    },
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (isQueued)
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 10,
                              vertical: 6,
                            ),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.08),
                              borderRadius: BorderRadius.circular(999),
                            ),
                            child: Text(
                              isCurrent ? 'Playing' : 'In Queue',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.8),
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          )
                        else
                          IconButton(
                            onPressed: () =>
                                unawaited(queueNotifier.enqueueTrack(track)),
                            icon: Icon(
                              Icons.add_circle_outline_rounded,
                              color: Colors.white.withValues(alpha: 0.74),
                            ),
                          ),
                      ],
                    ),
                  );
                }),
            ],
          ],
        ),
      ),
    );

    if (earlierQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Queued earlier',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.54),
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ),
          ),
        ),
      );
      for (final track in earlierQueue) {
        final videoId = extractTrackId(track);
        final queueIndex = videoId == null
            ? -1
            : queueState.queue.indexWhere(
                (entry) => extractTrackId(entry) == videoId,
              );
        queueChildren.add(
          _buildQueueTile(
            context: context,
            track: track,
            isActive: false,
            onTap: queueIndex >= 0
                ? () => unawaited(queueNotifier.playQueueIndex(queueIndex))
                : () {},
          ),
        );
      }
    }

    if (playedQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Played',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.54),
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ),
          ),
        ),
      );
      for (final track in playedQueue) {
        final videoId = extractTrackId(track);
        final queueIndex = videoId == null
            ? -1
            : queueState.queue.indexWhere(
                (entry) => extractTrackId(entry) == videoId,
              );
        queueChildren.add(
          _buildQueueTile(
            context: context,
            track: track,
            isActive: false,
            isDimmed: true,
            onTap: queueIndex >= 0
                ? () => unawaited(queueNotifier.playQueueIndex(queueIndex))
                : () {},
          ),
        );
      }
    }

    if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: _buildQueueTile(
            context: context,
            track: currentTrack,
            isActive: true,
            onTap: () => unawaited(queueNotifier.playQueueIndex(resolvedCurrentIndex)),
            trailingLabel: 'Playing',
          ),
        ),
      );
    }

    if (upcomingQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Up next',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.72),
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.45,
            ),
          ),
        ),
      );
    }

    if (upcomingQueue.isNotEmpty) {
      queueChildren.add(
        ReorderableListView.builder(
          shrinkWrap: true,
          buildDefaultDragHandles: false,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: upcomingQueue.length,
          onReorder: queueNotifier.reorderUpcomingQueue,
          proxyDecorator: (child, index, animation) {
            return AnimatedBuilder(
              animation: animation,
              builder: (context, _) {
                final elevation =
                    Tween<double>(begin: 0, end: 16).transform(animation.value);
                return Material(
                  color: Colors.transparent,
                  elevation: elevation,
                  child: child,
                );
              },
            );
          },
          itemBuilder: (context, index) {
            final track = upcomingQueue[index];
            final videoId = extractTrackId(track);
            final isHidden = isTrackHidden(track);
            final isRemoving =
                videoId != null && _removingQueueTrackIds.contains(videoId);
            final actualQueueIndex = resolvedCurrentIndex + 1 + index;
            return ReorderableDelayedDragStartListener(
              key: ValueKey(
                'queue-upcoming-${videoId ?? '$actualQueueIndex-${track['title']}'}',
              ),
              index: index,
              child: _buildQueueTile(
                context: context,
                track: track,
                isActive: false,
                isDimmed: isHidden,
                isRemoving: isRemoving,
                onTap: () =>
                    unawaited(queueNotifier.playQueueIndex(actualQueueIndex)),
                trailing: videoId != null
                    ? Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          IconButton(
                            onPressed: () {
                              if (queueState.mode == PlaybackQueueMode.playlist &&
                                  queueState.playlistId != null) {
                                ref.read(playlistProvider.notifier).setTrackHidden(
                                      queueState.playlistId!,
                                      videoId,
                                      !isHidden,
                                    );
                              }
                              queueNotifier.setQueueTrackHidden(
                                videoId,
                                !isHidden,
                              );
                            },
                            icon: AnimatedRotation(
                              duration: const Duration(milliseconds: 220),
                              turns: isHidden ? 0.08 : 0,
                              child: Icon(
                                isHidden
                                    ? Icons.visibility_off_rounded
                                    : Icons.visibility_rounded,
                                color: Colors.white.withValues(
                                  alpha: isHidden ? 0.38 : 0.7,
                                ),
                              ),
                            ),
                          ),
                          if (queueState.mode == PlaybackQueueMode.radio)
                            IconButton(
                              onPressed: () => showAddToPlaylistDialog(
                                context: context,
                                track: track,
                              ),
                              icon: Icon(
                                Icons.playlist_add_rounded,
                                color: Colors.white.withValues(alpha: 0.72),
                              ),
                            ),
                          IconButton(
                            onPressed: () {
                              unawaited(
                                _animateQueueRemoval(videoId, () {
                                  if (queueState.mode ==
                                          PlaybackQueueMode.playlist &&
                                      queueState.playlistId != null) {
                                    ref
                                        .read(playlistProvider.notifier)
                                        .removeTrackFromPlaylist(
                                          queueState.playlistId!,
                                          videoId,
                                        );
                                    queueNotifier.removePlaylistTrack(videoId);
                                    return;
                                  }
                                  queueNotifier.removeFromQueue(videoId);
                                }),
                              );
                            },
                            icon: Icon(
                              queueState.mode == PlaybackQueueMode.playlist
                                  ? Icons.delete_outline_rounded
                                  : Icons.remove_circle_outline_rounded,
                              color: Colors.white.withValues(alpha: 0.58),
                            ),
                          ),
                        ],
                      )
                    : null,
              ),
            );
          },
        ),
      );
    } else if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 4, 6, 18),
          child: Text(
            queueState.mode == PlaybackQueueMode.radio
                ? 'This queue will refill after the current song finishes.'
                : 'No upcoming songs are queued after the current track yet.',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.46),
              fontSize: 12.5,
              height: 1.5,
            ),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.radio && queueState.isLoadingQueue) {
      queueChildren.add(
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: Center(
            child: CircularProgressIndicator(color: _accentGrey),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.playlist &&
        (queueState.recommendations.isNotEmpty ||
            queueState.isLoadingRecommendations)) {
      queueChildren.add(
        const Padding(
          padding: EdgeInsets.fromLTRB(6, 18, 6, 12),
          child: Text(
            'Based on this song',
            style: TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
        ),
      );
      if (queueState.recommendations.isEmpty &&
          queueState.isLoadingRecommendations) {
        queueChildren.add(
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: _accentGrey),
            ),
          ),
        );
      } else {
        for (final track in queueState.recommendations) {
          queueChildren.add(
            _buildQueueTile(
              context: context,
              track: track,
              isActive: false,
              onTap: () => unawaited(queueNotifier.startRadioSession(track)),
              onAddToPlaylist: () => showAddToPlaylistDialog(
                context: context,
                track: track,
              ),
            ),
          );
        }
      }
      if (queueState.isLoadingRecommendations) {
        queueChildren.add(
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: _accentGrey),
            ),
          ),
        );
      }
    }

    return Container(
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.92),
        borderRadius: const BorderRadius.vertical(
          top: Radius.circular(_radiusLarge),
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.38),
            blurRadius: 28,
            offset: const Offset(0, -8),
          ),
        ],
      ),
      child: Column(
        children: [
          header,
          Expanded(
            child: queueState.mode == PlaybackQueueMode.none
                ? const Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 28),
                      child: Text(
                        'Start a song from search, recommendations, or a playlist and the live queue will appear here.',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white54,
                          fontSize: 15,
                          height: 1.45,
                        ),
                      ),
                    ),
                  )
                : NotificationListener<ScrollNotification>(
                    onNotification: (notification) {
                      if (notification.metrics.pixels >=
                          notification.metrics.maxScrollExtent - 180) {
                        unawaited(queueNotifier.loadMore());
                      }
                      return false;
                    },
                    child: ListView(
                      controller: scrollController,
                      physics: const BouncingScrollPhysics(
                        parent: AlwaysScrollableScrollPhysics(),
                      ),
                      padding: EdgeInsets.fromLTRB(
                        16,
                        8,
                        16,
                        math.max(
                          140,
                          MediaQuery.of(context).padding.bottom + 120,
                        ).toDouble(),
                      ),
                      children: queueChildren,
                    ),
                  ),
          ),
        ],
      ),
    );
  }

  Widget _buildLyricsPanel(
    BuildContext context,
    PlayerState playerState,
    TrackLyricsState lyricsState,
  ) {
    final activeIndex = _activeLyricsIndex(lyricsState, playerState);
    _syncLyricsScroll(activeIndex);

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 94, 18, 28),
        child: Container(
          decoration: BoxDecoration(
            color: const Color(0xFF101216).withValues(alpha: 0.94),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.34),
                blurRadius: 30,
                offset: const Offset(0, 18),
              ),
            ],
          ),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 26, 24, 18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
            Row(
              children: [
                Icon(
                  Icons.lyrics_rounded,
                  color: Colors.white.withValues(alpha: 0.72),
                  size: 18,
                ),
                const SizedBox(width: 10),
                Text(
                  'Lyrics',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.92),
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const Spacer(),
                if (lyricsState.source != null && lyricsState.source!.isNotEmpty)
                  Text(
                    lyricsState.source!,
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.34),
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 16),
            Text(
              playerState.currentTrackName,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.95),
                fontSize: 22,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              playerState.artist ?? 'Artist Unknown',
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.52),
                fontSize: 14,
                fontWeight: FontWeight.w500,
              ),
            ),
            const SizedBox(height: 24),
            Expanded(
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 240),
                switchInCurve: Curves.easeOutCubic,
                switchOutCurve: Curves.easeInCubic,
                child: () {
                  if (lyricsState.isLoading) {
                    return const Center(
                      key: ValueKey('lyrics-loading'),
                      child: CircularProgressIndicator(color: _accentGrey),
                    );
                  }
                  if (!lyricsState.hasLyrics) {
                    return Center(
                      key: const ValueKey('lyrics-empty'),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              Icons.lyrics_outlined,
                              color: Colors.white.withValues(alpha: 0.38),
                              size: 42,
                            ),
                            const SizedBox(height: 18),
                            const Text(
                              'Lyrics are not available for this track yet.',
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: Colors.white70,
                                fontSize: 18,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            const SizedBox(height: 10),
                            Text(
                              lyricsState.error ??
                                  'Swipe right or tap Player to return.',
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                color: Colors.white54,
                                fontSize: 13,
                                height: 1.35,
                              ),
                            ),
                          ],
                        ),
                      ),
                    );
                  }

                  return ListView.builder(
                    key: ValueKey('lyrics-${lyricsState.videoId}'),
                    physics: const BouncingScrollPhysics(
                      parent: AlwaysScrollableScrollPhysics(),
                    ),
                    padding: const EdgeInsets.only(bottom: 140),
                    itemCount: lyricsState.lines.length,
                    itemBuilder: (context, index) {
                      final line = lyricsState.lines[index];
                      final isActive = index == activeIndex;
                      return Padding(
                        key: _lyricKeyFor(index),
                        padding: const EdgeInsets.symmetric(vertical: 8),
                        child: AnimatedContainer(
                          duration: const Duration(milliseconds: 260),
                          curve: Curves.easeOutCubic,
                          transform: Matrix4.translationValues(
                            isActive ? 0.0 : 8.0,
                            0.0,
                            0.0,
                          ),
                          child: AnimatedDefaultTextStyle(
                            duration: const Duration(milliseconds: 260),
                            curve: Curves.easeOutCubic,
                            style: TextStyle(
                              color: isActive
                                  ? Colors.white
                                  : Colors.white.withValues(alpha: 0.34),
                              fontSize: isActive ? 30 : 24,
                              fontWeight:
                                  isActive ? FontWeight.w700 : FontWeight.w500,
                              height: 1.16,
                            ),
                            child: Text(line.text),
                          ),
                        ),
                      );
                    },
                  );
                }(),
              ),
            ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildPlayerPanel(
    BuildContext context,
    PlayerState playerState,
    AudioPlayerNotifier audioNotifier,
    PlaybackQueueState queueState,
  ) {
    const accentColor = _accentGrey;
    final nextUpTrack = _nextUpTrack(queueState);
    final queueNotifier = ref.read(playbackQueueProvider.notifier);
    final bottomInset = MediaQuery.of(context).padding.bottom;
    final artworkSize =
        (MediaQuery.of(context).size.width * 0.76).clamp(248.0, 360.0).toDouble();

    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onVerticalDragEnd: (details) {
        final velocity = details.primaryVelocity ?? 0;
        if (velocity > 700) {
          if (Navigator.canPop(context)) {
            Navigator.pop(context);
          }
        } else if (velocity < -700) {
          _openQueueSheet();
        }
      },
      child: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            return SingleChildScrollView(
              physics: const BouncingScrollPhysics(
                parent: AlwaysScrollableScrollPhysics(),
              ),
              padding: EdgeInsets.fromLTRB(
                24,
                18,
                24,
                math.max(156, bottomInset + 156).toDouble(),
              ),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: constraints.maxHeight),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const SizedBox(height: 8),
                    Center(
                      child: Hero(
                        tag: 'album_art_${playerState.currentTrackName}',
                        child: Container(
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(_radiusLarge),
                            border: Border.all(
                              color: Colors.white.withValues(alpha: 0.08),
                            ),
                            color: Colors.white.withValues(alpha: 0.03),
                            boxShadow: [
                              BoxShadow(
                                color: Colors.black.withValues(alpha: 0.44),
                                blurRadius: 48,
                                offset: const Offset(0, 26),
                              ),
                            ],
                          ),
                          child: AppArtwork(
                            thumbnail: playerState.thumbnail,
                            videoId: playerState.videoId,
                            width: artworkSize,
                            height: artworkSize,
                            radius: _radiusMedium,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 40),
                    Text(
                      playerState.currentTrackName,
                      style: const TextStyle(
                        fontSize: 28,
                        fontWeight: FontWeight.w800,
                        color: Colors.white,
                        height: 1.2,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                    const SizedBox(height: 8),
                    Text(
                      playerState.artist ?? 'Artist Unknown',
                      style: TextStyle(
                        fontSize: 18,
                        color: Colors.white.withValues(alpha: 0.7),
                      ),
                    ),
                    if (nextUpTrack != null) ...[
                      const SizedBox(height: 18),
                      GestureDetector(
                        onTap: _openQueueSheet,
                        child: Container(
                          width: double.infinity,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 16,
                            vertical: 14,
                          ),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.04),
                            borderRadius: BorderRadius.circular(_radiusLarge),
                            border: Border.all(
                              color: Colors.white.withValues(alpha: 0.08),
                            ),
                          ),
                          child: Row(
                            children: [
                              Icon(
                                Icons.queue_music_rounded,
                                color: Colors.white.withValues(alpha: 0.72),
                                size: 18,
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      'Up next',
                                      style: TextStyle(
                                        color: Colors.white.withValues(alpha: 0.46),
                                        fontSize: 11,
                                        fontWeight: FontWeight.w700,
                                        letterSpacing: 0.5,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      nextUpTrack['title'] ?? 'More like this',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        color: Colors.white,
                                        fontSize: 14,
                                        fontWeight: FontWeight.w600,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              Icon(
                                Icons.keyboard_arrow_up_rounded,
                                color: Colors.white.withValues(alpha: 0.56),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ],
                    const SizedBox(height: 26),
                    SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: Colors.white,
                        inactiveTrackColor: Colors.white.withValues(alpha: 0.2),
                        thumbColor: Colors.white,
                        overlayColor: Colors.white.withValues(alpha: 0.1),
                        trackHeight: 4.0,
                        thumbShape: const RoundSliderThumbShape(
                          enabledThumbRadius: 6,
                        ),
                      ),
                      child: Slider(
                        value: playerState.duration > 0
                            ? _dragValue ??
                                (playerState.currentPosition /
                                        playerState.duration)
                                    .clamp(0.0, 1.0)
                            : 0.0,
                        onChanged: playerState.duration > 0
                            ? (val) => setState(() => _dragValue = val)
                            : null,
                        onChangeEnd: playerState.duration > 0
                            ? (val) {
                                unawaited(
                                  audioNotifier.seek(
                                    (val * playerState.duration).toInt(),
                                  ),
                                );
                                setState(() => _dragValue = null);
                              }
                            : null,
                      ),
                    ),
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 8.0),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(
                            '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')}',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.6),
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          Text(
                            '${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.6),
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.shuffle_rounded,
                            color: Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            unawaited(
                              ref.read(playbackQueueProvider.notifier).playShuffled(),
                            );
                          },
                        ),
                        Stack(
                          clipBehavior: Clip.none,
                          children: [
                            IconButton(
                              visualDensity: VisualDensity.compact,
                              iconSize: 28,
                              padding: EdgeInsets.zero,
                              icon: Icon(
                                Icons.timer_outlined,
                                color: playerState.sleepTimerRemainingSeconds > 0
                                    ? accentColor
                                    : Colors.white.withValues(alpha: 0.5),
                              ),
                              onPressed: () => _showSleepTimerSheet(
                                context,
                                audioNotifier,
                                playerState,
                              ),
                            ),
                            if (playerState.sleepTimerRemainingSeconds > 0)
                              Positioned(
                                right: -2,
                                top: 2,
                                child: Container(
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 5,
                                    vertical: 2,
                                  ),
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.14),
                                    borderRadius: BorderRadius.circular(999),
                                  ),
                                  child: Text(
                                    _formatSleepTimerLabel(
                                      playerState.sleepTimerRemainingSeconds,
                                    ),
                                    style: const TextStyle(
                                      color: Colors.white,
                                      fontSize: 9,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.loop,
                            color: playerState.isLooping
                                ? accentColor
                                : Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            audioNotifier.toggleLoop(
                              0,
                              playerState.duration * 1000,
                            );
                          },
                        ),
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.playlist_add,
                            color: Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            if (playerState.videoId == null) return;
                            showAddToPlaylistDialog(
                              context: context,
                              track: {
                                'id': playerState.videoId,
                                'videoId': playerState.videoId,
                                'title': playerState.currentTrackName,
                                'thumbnail': playerState.thumbnail,
                                'channel': playerState.artist,
                                'duration': playerState.duration,
                              },
                            );
                          },
                        ),
                      ],
                    ),
                    const SizedBox(height: 18),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                      crossAxisAlignment: CrossAxisAlignment.center,
                      children: [
                        SizedBox(
                          width: 50,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: 34,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(queueNotifier.playPrevious());
                            },
                            icon: Icon(
                              Icons.skip_previous_rounded,
                              color: Colors.white.withValues(alpha: 0.82),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: 50,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: 34,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(
                                audioNotifier.seek(
                                  playerState.currentPosition - 10,
                                ),
                              );
                            },
                            icon: Icon(
                              Icons.replay_10_rounded,
                              color: Colors.white.withValues(alpha: 0.8),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: 92,
                          child: Center(
                            child: GestureDetector(
                              onTap: () {
                                unawaited(
                                  playerState.isPlaying
                                      ? audioNotifier.pause()
                                      : audioNotifier.play(),
                                );
                              },
                              child: Container(
                                width: 84,
                                height: 84,
                                decoration: BoxDecoration(
                                  shape: BoxShape.circle,
                                  color: _surfaceGreyAlt,
                                  boxShadow: [
                                    BoxShadow(
                                      color: Colors.black.withValues(alpha: 0.26),
                                      blurRadius: 24,
                                      offset: const Offset(0, 10),
                                    ),
                                  ],
                                ),
                                alignment: Alignment.center,
                                child: playerState.isDownloading
                                    ? const SizedBox(
                                        width: 38,
                                        height: 38,
                                        child: CircularProgressIndicator(
                                          color: Colors.white,
                                          strokeWidth: 3,
                                        ),
                                      )
                                    : Transform.translate(
                                        offset: Offset(
                                          playerState.isPlaying ? 0 : 1,
                                          0,
                                        ),
                                        child: Icon(
                                          playerState.isPlaying
                                              ? Icons.pause_rounded
                                              : Icons.play_arrow_rounded,
                                          color: Colors.white,
                                          size: 42,
                                        ),
                                      ),
                              ),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: 50,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: 34,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(
                                audioNotifier.seek(
                                  playerState.currentPosition + 10,
                                ),
                              );
                            },
                            icon: Icon(
                              Icons.forward_10_rounded,
                              color: Colors.white.withValues(alpha: 0.8),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: 50,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: 34,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(queueNotifier.playNext());
                            },
                            icon: Icon(
                              Icons.skip_next_rounded,
                              color: Colors.white.withValues(alpha: 0.82),
                            ),
                          ),
                        ),
                      ],
                    ),
                    SizedBox(height: bottomInset > 0 ? 16 : 8),
                  ],
                ),
              ),
            );
          },
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final lyricsState = ref.watch(lyricsProvider);
    final queueState = ref.watch(playbackQueueProvider);
    _ensureLyricsLoaded(playerState.videoId);

    return Scaffold(
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.keyboard_arrow_down, color: Colors.white),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text(
          'Now Playing',
          style: TextStyle(
            fontSize: 11,
            color: Colors.white,
            fontWeight: FontWeight.w700,
            letterSpacing: 1.4,
          ),
        ),
        centerTitle: true,
      ),
      body: Stack(
        fit: StackFit.expand,
        children: [
          const ColoredBox(color: _voidBlack),
          PageView(
            controller: _pageController,
            onPageChanged: (index) {
              setState(() => _activePage = index);
              if (index == 1) {
                _lastSyncedLyricIndex = -1;
                _dismissQueueSheetIfOpen();
              }
            },
            children: [
              _buildPlayerPanel(
                context,
                playerState,
                audioNotifier,
                queueState,
              ),
              _buildLyricsPanel(context, playerState, lyricsState),
            ],
          ),
          Positioned(
            right: _activePage == 0 ? 0 : null,
            left: _activePage == 1 ? 0 : null,
            top: MediaQuery.of(context).size.height * 0.22,
            child: _buildPanelTab(
              icon: _activePage == 0
                  ? Icons.lyrics_rounded
                  : Icons.graphic_eq_rounded,
              pointLeft: _activePage == 0,
              onTap: _activePage == 0 ? _openLyricsPanel : _closeLyricsPanel,
            ),
          ),
        ],
      ),
    );
  }
}

class AlbumDetailsScreen extends ConsumerStatefulWidget {
  final String albumId;
  final Map<String, dynamic> fallbackAlbum;

  const AlbumDetailsScreen({
    super.key,
    required this.albumId,
    required this.fallbackAlbum,
  });

  @override
  ConsumerState<AlbumDetailsScreen> createState() => _AlbumDetailsScreenState();
}

class _AlbumDetailsScreenState extends ConsumerState<AlbumDetailsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(albumDetailsProvider.notifier).fetchAlbum(widget.albumId),
      );
    });
  }

  Future<void> _playAlbum(
    List<dynamic> tracks, {
    bool shuffle = false,
  }) async {
    if (tracks.isEmpty) return;
    final navigator = Navigator.of(context);
    final currentTrack = shuffle
        ? tracks[DateTime.now().microsecondsSinceEpoch % tracks.length]
        : tracks.first;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'album:${widget.albumId}',
          playlistName:
              widget.fallbackAlbum['title']?.toString() ?? 'Album Queue',
          tracks: tracks,
          currentTrack: currentTrack,
          shuffle: shuffle,
        );
    if (!mounted) return;
    navigator.push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  void _saveAlbumAsPlaylist(Map<String, dynamic> album, List<dynamic> tracks) {
    if (tracks.isEmpty) return;
    final title = album['title']?.toString() ?? 'Saved Album';
    final playlist =
        ref.read(playlistProvider.notifier).createPlaylist(title);
    for (final track in tracks) {
      ref.read(playlistProvider.notifier).addTrackToPlaylist(playlist.id, track);
    }
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Saved "$title" as a playlist.'),
        backgroundColor: Colors.grey[900],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final albumState = ref.watch(albumDetailsProvider);
    final loadedAlbum = albumState.album;
    final album =
        loadedAlbum != null && loadedAlbum['id']?.toString() == widget.albumId
            ? loadedAlbum
            : widget.fallbackAlbum;
    final tracks = (loadedAlbum?['tracks'] as List<dynamic>?) ?? const [];

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: const Text('Album'),
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.03),
                    borderRadius: BorderRadius.circular(_radiusLarge),
                    border: Border.all(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                  child: AppArtwork(
                    thumbnail: album['thumbnail'],
                    width: 240,
                    height: 240,
                    radius: 28,
                  ),
                ),
              ),
              const SizedBox(height: 24),
              Text(
                album['title']?.toString() ?? 'Unknown Album',
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 28,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                [
                  album['artist']?.toString() ?? 'Unknown Artist',
                  if ((album['year'] ?? '').toString().isNotEmpty)
                    album['year'].toString(),
                  if (tracks.isNotEmpty) '${tracks.length} tracks',
                ].join(' • '),
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.56),
                  fontSize: 14,
                ),
              ),
              const SizedBox(height: 22),
              Row(
                children: [
                  Expanded(
                    child: FilledButton(
                      style: FilledButton.styleFrom(
                        backgroundColor: _surfaceGreyAlt,
                        foregroundColor: Colors.white,
                        padding: const EdgeInsets.symmetric(vertical: 14),
                      ),
                      onPressed: tracks.isEmpty ? null : () => _playAlbum(tracks),
                      child: const Text('Play Album'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.white,
                      side: BorderSide(
                        color: Colors.white.withValues(alpha: 0.14),
                      ),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 14,
                      ),
                    ),
                    onPressed:
                        tracks.isEmpty ? null : () => _playAlbum(tracks, shuffle: true),
                    icon: const Icon(Icons.shuffle_rounded),
                    label: const Text('Shuffle'),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
                  side: BorderSide(color: Colors.white.withValues(alpha: 0.14)),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 14,
                  ),
                ),
                onPressed:
                    tracks.isEmpty ? null : () => _saveAlbumAsPlaylist(album, tracks),
                icon: const Icon(Icons.library_add_rounded),
                label: const Text('Save Album As Playlist'),
              ),
              const SizedBox(height: 24),
              const Text(
                'Tracks',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 16),
              if (albumState.isLoading && tracks.isEmpty)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.symmetric(vertical: 32),
                    child: CircularProgressIndicator(color: _accentGrey),
                  ),
                )
              else if (tracks.isEmpty)
                Text(
                  albumState.error ?? 'No track data available for this album.',
                  style: TextStyle(color: Colors.white.withValues(alpha: 0.56)),
                )
              else
                ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: tracks.length,
                  itemBuilder: (context, index) {
                    final track = tracks[index];
                    final videoId = (track['id'] ?? track['videoId'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.06),
                        ),
                      ),
                      child: ListTile(
                        onTap: () async {
                          final navigator = Navigator.of(context);
                          await ref
                              .read(playbackQueueProvider.notifier)
                              .startPlaylistSession(
                                playlistId: 'album:${widget.albumId}',
                                playlistName:
                                    album['title']?.toString() ?? 'Album Queue',
                                tracks: tracks,
                                currentTrack: track,
                              );
                          if (!mounted) return;
                          navigator.push(
                            MaterialPageRoute(
                              builder: (_) => const FullPlayerScreen(),
                            ),
                          );
                        },
                        leading: AppArtwork(
                          thumbnail: track['thumbnail'] ?? album['thumbnail'],
                          videoId: videoId,
                          width: 56,
                          height: 56,
                          radius: 14,
                        ),
                        title: Text(
                          track['title'] ?? 'Unknown Track',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        subtitle: Text(
                          track['channel'] ??
                              track['author'] ??
                              album['artist']?.toString() ??
                              '',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.52),
                            fontSize: 12,
                          ),
                        ),
                        trailing: IconButton(
                          onPressed: () => showAddToPlaylistDialog(
                            context: context,
                            track: track,
                          ),
                          icon: Icon(
                            Icons.playlist_add_rounded,
                            color: Colors.white.withValues(alpha: 0.68),
                          ),
                        ),
                      ),
                    );
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class TrackDetailsScreen extends ConsumerWidget {
  final Map<String, dynamic> track;
  const TrackDetailsScreen({super.key, required this.track});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final details = ref.watch(trackDetailsProvider);
    final videoId = (track['id'] ?? track['videoId'])?.toString();
    final downloadTask = videoId == null
        ? null
        : ref.watch(downloadTaskProvider(videoId));
    final albumTitleValue =
        (details?['album_title'] ??
                details?['album'] ??
                track['album_title'] ??
                track['album'])
            ?.toString()
            .trim();
    final albumTitle =
        albumTitleValue == null || albumTitleValue.isEmpty ? null : albumTitleValue;
    final albumIdValue =
        (details?['album_id'] ?? track['album_id'])?.toString().trim();
    final albumId =
        albumIdValue == null || albumIdValue.isEmpty ? null : albumIdValue;
    final albumArtist =
        (details?['author'] ?? track['channel'] ?? track['author'] ?? '')
            .toString();

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: const Text(''),
        backgroundColor: Colors.transparent,
        elevation: 0,
      ),
      body: SingleChildScrollView(
        child: Column(
          children: [
            // Top Section: Track Info
            Container(
              padding: const EdgeInsets.all(24),
              color: _voidBlack,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  AppArtwork(
                    thumbnail: track['thumbnail'],
                    videoId: videoId,
                    width: 140,
                    height: 140,
                    radius: 18,
                  ),
                  const SizedBox(width: 24),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Song',
                            style: TextStyle(
                                color: Colors.white,
                                fontSize: 12,
                                fontWeight: FontWeight.bold)),
                        const SizedBox(height: 8),
                        Text(track['title'] ?? 'Unknown',
                            style: const TextStyle(
                                color: Colors.white,
                                fontSize: 32,
                                fontWeight: FontWeight.bold),
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis),
                        const SizedBox(height: 8),
                        if (details != null &&
                            ((details['artist'] ?? details['author'] ?? '')
                                    .toString()
                                    .isNotEmpty))
                          Row(
                            children: [
                              const Icon(Icons.person,
                                  color: Colors.white70, size: 16),
                              const SizedBox(width: 4),
                              Expanded(
                                child: Builder(
                                  builder: (context) {
                                     String a = (details['artist'] == null || details['artist'] == 'null')
                                         ? (details['author'] ?? '')
                                         : details['artist'];
                                     if (a.isEmpty) a = track['author'] ?? track['channel'] ?? '';
                                     String d = details['release_date'] ?? '';
                                     if (d == 'null') d = '';
                                     if (d.contains('T')) d = d.split('T').first;
                                     String text = [a, d].where((e) => e.isNotEmpty).join(' • ');
                                     return Text(text,
                                        style: const TextStyle(color: Colors.white70, fontSize: 14),
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis);
                                  }
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),

            if (albumTitle != null)
              Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 24, vertical: 8),
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(_radiusLarge),
                    onTap: albumId == null
                        ? null
                        : () {
                            final resolvedAlbumId = albumId;
                            unawaited(
                              ref
                                  .read(albumDetailsProvider.notifier)
                                  .fetchAlbum(resolvedAlbumId),
                            );
                            Navigator.of(context).push(
                              MaterialPageRoute(
                                builder: (_) => AlbumDetailsScreen(
                                  albumId: resolvedAlbumId,
                                  fallbackAlbum: {
                                    'id': resolvedAlbumId,
                                    'title': albumTitle,
                                    'artist': albumArtist,
                                    'thumbnail': track['thumbnail'],
                                    'year': '',
                                  },
                                ),
                              ),
                            );
                          },
                    child: Ink(
                      padding: const EdgeInsets.all(18),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.04),
                        borderRadius: BorderRadius.circular(_radiusLarge),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.07),
                        ),
                      ),
                      child: Row(
                        children: [
                          Container(
                            width: 46,
                            height: 46,
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.06),
                              borderRadius: BorderRadius.circular(16),
                            ),
                            child: Icon(
                              Icons.album_rounded,
                              color: Colors.white.withValues(alpha: 0.78),
                            ),
                          ),
                          const SizedBox(width: 14),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  'From the album',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.48),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  albumTitle,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Colors.white,
                                    fontSize: 16,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          Icon(
                            Icons.chevron_right_rounded,
                            color: Colors.white.withValues(alpha: 0.42),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),

            // Play & Download Buttons
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
              child: Row(
                children: [
                  IconButton(
                    iconSize: 56,
                    icon: const Icon(Icons.play_circle_fill,
                        color: Colors.white),
                    onPressed: () {
                      unawaited(ref.read(playbackQueueProvider.notifier).startRadioSession(track));
                      Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                    },
                  ),
                  const SizedBox(width: 16),
                  IconButton(
                    iconSize: 36,
                    icon: const Icon(Icons.playlist_add, color: Colors.white70),
                    onPressed: () {
                      showAddToPlaylistDialog(
                        context: context,
                        track: {
                          'id': track['id'] ?? track['videoId'],
                          'videoId': track['id'] ?? track['videoId'],
                          'title': track['title'],
                          'thumbnail': track['thumbnail'],
                          'channel': track['channel'] ?? track['author'],
                          'duration': track['duration'],
                        },
                      );
                    },
                  ),
                  const SizedBox(width: 8),
                  IconButton(
                    iconSize: 36,
                    icon: downloadTask?.phase == DownloadPhase.active
                        ? SizedBox(
                            width: 34,
                            height: 34,
                            child: CircularProgressIndicator(
                              value: downloadTask!.progress > 0
                                  ? downloadTask.progress
                                  : null,
                              strokeWidth: 2.2,
                              color: _accentGrey,
                            ),
                          )
                        : Icon(
                            downloadTask?.phase == DownloadPhase.complete
                                ? Icons.check_circle_outline
                                : Icons.download_for_offline,
                            color: Colors.white70,
                          ),
                    onPressed: videoId == null
                        ? null
                        : () {
                            ref
                                .read(downloadCenterProvider.notifier)
                                .downloadTrack(track);
                          },
                  ),
                ],
              ),
            ),

            // Similar Tracks
            if (details == null)
              const Center(
                  child: Padding(
                      padding: EdgeInsets.all(32),
                      child:
                          CircularProgressIndicator(color: Colors.white)))
            else if (details['similar_tracks'] != null &&
                (details['similar_tracks'] as List).isNotEmpty) ...[
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 24, vertical: 8),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Text('Similar Tracks',
                      style: TextStyle(
                          color: Colors.white,
                          fontSize: 20,
                          fontWeight: FontWeight.bold)),
                ),
              ),
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                padding: const EdgeInsets.symmetric(horizontal: 16),
                itemCount: (details['similar_tracks'] as List).length,
                itemBuilder: (context, i) {
                  final st = details['similar_tracks'][i];
                  return ListTile(
                    onTap: () {
                      ref
                          .read(trackDetailsProvider.notifier)
                          .fetchDetails(st['id']);
                      Navigator.pushReplacement(
                          context,
                          MaterialPageRoute(
                              builder: (_) => TrackDetailsScreen(track: st)));
                    },
                    leading: ClipRRect(
                      borderRadius: BorderRadius.circular(4),
                      child: AppArtwork(
                        thumbnail: st['thumbnail'],
                        videoId: st['id']?.toString(),
                        width: 48,
                        height: 48,
                        radius: 10,
                      ),
                    ),
                    title: Text(st['title'] ?? '',
                        style: const TextStyle(color: Colors.white),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                    subtitle: Text(st['channel'] ?? '',
                        style: const TextStyle(color: Colors.white54),
                        maxLines: 1),
                  );
                },
              ),
            ],
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}

class FadeIndexedStack extends StatefulWidget {
  final int index;
  final List<Widget> children;
  final Duration duration;

  const FadeIndexedStack({
    super.key,
    required this.index,
    required this.children,
    this.duration = const Duration(milliseconds: 300),
  });

  @override
  State<FadeIndexedStack> createState() => _FadeIndexedStackState();
}

class _FadeIndexedStackState extends State<FadeIndexedStack>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    _controller = AnimationController(vsync: this, duration: widget.duration);
    _controller.forward();
    super.initState();
  }

  @override
  void didUpdateWidget(FadeIndexedStack oldWidget) {
    if (widget.index != oldWidget.index) {
      _controller.forward(from: 0.0);
    }
    super.didUpdateWidget(oldWidget);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: _controller,
      child: IndexedStack(
        index: widget.index,
        children: widget.children,
      ),
    );
  }
}
