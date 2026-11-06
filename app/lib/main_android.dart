import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:permission_handler/permission_handler.dart';

import 'logic/audio_provider.dart';
import 'logic/app_update_provider.dart';
import 'logic/auth_provider.dart';
import 'main_shell.dart';
import 'navigation/player_navigation.dart';
import 'ui/app_theme_tokens.dart';
import 'widgets/app_update_banner.dart';
import 'widgets/player/mini_player.dart';

const _accentGrey = neatieActive;
const _surfaceGrey = neatieRaised;
const _voidBlack = neatieInk;

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initSupabase();
  runApp(const ProviderScope(child: AuralisApp()));
  unawaited(initAudioService());
}

class AuralisApp extends ConsumerStatefulWidget {
  const AuralisApp({super.key});

  @override
  ConsumerState<AuralisApp> createState() => _AuralisAppState();
}

class _AuralisAppState extends ConsumerState<AuralisApp>
    with WidgetsBindingObserver {
  final _navigatorKey = GlobalKey<NavigatorState>();
  final _routeTracker = _AppRouteTracker();
  Timer? _introFadeTimer;
  Timer? _introDismissTimer;
  bool _showIntro = true;
  bool _introFading = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(_requestNotificationPermissionIfNeeded());
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(
        ref.read(appUpdateProvider.notifier).checkForUpdate(automatic: true),
      );
    });
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
    _routeTracker.dispose();
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
    if (state == AppLifecycleState.resumed &&
        ref.read(appUpdateProvider).phase ==
            AppUpdatePhase.permissionRequired) {
      unawaited(
        ref.read(appUpdateProvider.notifier).resumeInstallAfterPermission(),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final authState = ref.watch(authProvider);
    final hasActiveTrack = ref.watch(
      audioPlayerProvider.select(
        (state) => state.currentTrackName != 'No track loaded',
      ),
    );
    final Widget home;
    if (authState.canUseApp) {
      home = const MainLayout();
    } else if (authState.requiresAuthGate) {
      home = const AuthGateScreen();
    } else if (authState.isRestoring || authState.isAuthRedirectSettling) {
      home = const _AuthRestoringScreen();
    } else {
      home = const AuthGateScreen();
    }

    final appShellKey = authState.canUseApp
        ? ValueKey('app:${authState.storageScopeId}')
        : ValueKey(
            authState.isAuthRedirectSettling ? 'auth:redirect' : 'auth:gate',
          );

    return MaterialApp(
      key: appShellKey,
      title: 'Neatie',
      navigatorKey: _navigatorKey,
      navigatorObservers: <NavigatorObserver>[_routeTracker],
      builder: (context, child) => AnimatedBuilder(
        animation: _routeTracker,
        builder: (context, _) {
          final showMiniPlayer = authState.canUseApp &&
              hasActiveTrack &&
              !_showIntro &&
              !_routeTracker.isFullPlayer;
          final bottomInset = MediaQuery.viewPaddingOf(context).bottom;
          return Stack(
            children: <Widget>[
              if (child != null) child,
              Positioned(
                left: 18,
                right: 18,
                bottom:
                    _routeTracker.isRoot ? bottomInset + 66 : bottomInset + 8,
                child: IgnorePointer(
                  ignoring: !showMiniPlayer,
                  child: AnimatedSlide(
                    offset: showMiniPlayer ? Offset.zero : const Offset(0, 0.3),
                    duration: const Duration(milliseconds: 240),
                    curve: Curves.easeOutCubic,
                    child: AnimatedOpacity(
                      opacity: showMiniPlayer ? 1 : 0,
                      duration: const Duration(milliseconds: 200),
                      child: MiniPlayer(
                        onOpenPlayer: () => unawaited(
                          openFullPlayerWithNavigator(
                            _navigatorKey.currentState,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          );
        },
      ),
      onGenerateRoute: (settings) {
        final routeName = settings.name ?? '';
        if (routeName.startsWith('/?code=') ||
            routeName.startsWith('/?error=') ||
            routeName.startsWith('/#')) {
          return MaterialPageRoute<void>(
            settings: settings,
            builder: (_) => home,
          );
        }
        return null;
      },
      onUnknownRoute: (settings) => MaterialPageRoute<void>(
        settings: settings,
        builder: (_) => home,
      ),
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: neatieInk,
        primaryColor: _accentGrey,
        colorScheme: const ColorScheme.dark(
          primary: _accentGrey,
          secondary: _accentGrey,
          surface: _surfaceGrey,
          onSurface: Colors.white,
        ),
        textTheme:
            GoogleFonts.spaceGroteskTextTheme(Theme.of(context).textTheme)
                .apply(bodyColor: Colors.white, displayColor: Colors.white),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
        ),
        splashColor: Colors.white10,
        highlightColor: Colors.white10,
      ),
      home: Stack(
        children: [
          home,
          if (!_showIntro)
            const Positioned(
              top: 0,
              left: 0,
              right: 0,
              child: AppUpdateBanner(),
            ),
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
                          'assets/branding/neatie_3rd.png',
                          width: 37,
                          height: 37,
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

class _AppRouteTracker extends NavigatorObserver with ChangeNotifier {
  final List<Route<dynamic>> _routes = <Route<dynamic>>[];

  bool get isRoot => _routes.length <= 1;
  bool get isFullPlayer =>
      _routes.isNotEmpty && _routes.last.settings.name == fullPlayerRouteName;

  void _changed() => notifyListeners();

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.add(route);
    _changed();
  }

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.remove(route);
    _changed();
  }

  @override
  void didRemove(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.remove(route);
    _changed();
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    final index = oldRoute == null ? -1 : _routes.indexOf(oldRoute);
    if (index >= 0 && newRoute != null) {
      _routes[index] = newRoute;
    } else if (newRoute != null) {
      _routes.add(newRoute);
    }
    _changed();
  }
}

class _AuthRestoringScreen extends StatelessWidget {
  const _AuthRestoringScreen();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      backgroundColor: _voidBlack,
      body: Center(
        child: SizedBox(
          width: 24,
          height: 24,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: Colors.white70,
          ),
        ),
      ),
    );
  }
}
