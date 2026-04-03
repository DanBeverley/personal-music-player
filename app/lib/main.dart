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
import 'screens/assistant_screen.dart';
import 'widgets/app_artwork.dart';
import 'widgets/app_bottom_nav_bar.dart';
import 'widgets/download_hud.dart';
import 'widgets/track_list_skeleton.dart';

part 'main_dialogs.dart';
part 'main_library.dart';
part 'main_details.dart';
part 'main_fade_indexed_stack.dart';
part 'main_home.dart';
part 'main_player.dart';
part 'main_shell.dart';

const _accentGrey = Color(0xFFD0D5D8);
const _surfaceGrey = Color(0xFF363C40);
const _surfaceGreyAlt = Color(0xFF2C3135);
const _voidBlack = Color(0xFF252A2D);
const double _radiusLarge = 12;
const double _radiusMedium = 10;
const List<Color> _playlistCoverPalette = <Color>[
  Color(0xFF4A5155),
  Color(0xFF41484C),
  Color(0xFF383E42),
  Color(0xFF50575C),
  Color(0xFF444B4F),
  Color(0xFF565D62),
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
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (state == AppLifecycleState.resumed ||
        state == AppLifecycleState.inactive ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached) {
      audioNotifier.refreshMediaSession();
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


