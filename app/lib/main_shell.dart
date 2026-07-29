import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import 'home_screen_controller.dart';
import 'logic/audio_provider.dart';
import 'logic/audio_provider_queue.dart';
import 'logic/auth_provider.dart';
import 'logic/song_match_provider.dart';
import 'main_fade_indexed_stack.dart';
import 'main_home.dart';
import 'main_library.dart';
import 'screens/neatie_profile_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'ui/neatie_components.dart';
import 'widgets/app_bottom_nav_bar.dart';
import 'widgets/download_hud.dart';
import 'widgets/player/mini_player.dart';

const _voidBlack = neatieInk;

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
    final authControlsLocked =
        authState.isBusy || authState.isRedirectSettling || authState.canUseApp;
    final statusText = authState.canUseApp
        ? 'Opening your feed...'
        : authState.isRedirectSettling
            ? 'Finishing sign in...'
            : authState.isBusy
                ? 'Contacting Neatie...'
                : '';

    return Scaffold(
      backgroundColor: _voidBlack,
      body: Stack(
        children: [
          Positioned.fill(
            child: Image.asset(
              'assets/branding/neatie_login_wave_bg.png',
              fit: BoxFit.cover,
              alignment: Alignment.bottomCenter,
            ),
          ),
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [
                    Colors.black.withValues(alpha: 0.96),
                    Colors.black.withValues(alpha: 0.82),
                    Colors.black.withValues(alpha: 0.38),
                  ],
                  stops: const [0, 0.56, 1],
                ),
              ),
            ),
          ),
          SafeArea(
            child: LayoutBuilder(
              builder: (context, constraints) {
                return SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(26, 44, 26, 42),
                  child: ConstrainedBox(
                    constraints: BoxConstraints(
                      minHeight: constraints.maxHeight - 86,
                      maxWidth: 520,
                    ),
                    child: Center(
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Image.asset(
                            'assets/branding/neatie_3rd.png',
                            width: 88,
                            height: 58,
                            fit: BoxFit.contain,
                          ),
                          const SizedBox(height: 54),
                          Text(
                            'Welcome back',
                            textAlign: TextAlign.center,
                            style: GoogleFonts.spaceGrotesk(
                              fontSize: 42,
                              height: 1.05,
                              fontWeight: FontWeight.w800,
                              color: Colors.white,
                              letterSpacing: -1.4,
                            ),
                          ),
                          const SizedBox(height: 14),
                          Text(
                            'Sign in to continue listening.',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.58),
                              fontSize: 18,
                              height: 1.35,
                            ),
                          ),
                          const SizedBox(height: 48),
                          _AuthTextField(
                            controller: _emailController,
                            enabled: !authControlsLocked,
                            icon: Icons.mail_outline_rounded,
                            hintText: 'Email for magic link',
                          ),
                          const SizedBox(height: 18),
                          if (statusText.isNotEmpty)
                            _AuthStatusPill(text: statusText)
                          else
                            const SizedBox(height: 54),
                          if (authState.error != null &&
                              authState.error!.isNotEmpty) ...[
                            const SizedBox(height: 14),
                            Text(
                              authState.error!,
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.7),
                                fontSize: 13,
                                height: 1.35,
                              ),
                            ),
                          ],
                          const SizedBox(height: 26),
                          SizedBox(
                            width: double.infinity,
                            height: 62,
                            child: FilledButton(
                              onPressed: authControlsLocked
                                  ? null
                                  : () => authNotifier.signInWithGoogle(),
                              style: FilledButton.styleFrom(
                                backgroundColor: Colors.white,
                                disabledBackgroundColor:
                                    Colors.white.withValues(alpha: 0.2),
                                foregroundColor: Colors.black,
                                disabledForegroundColor:
                                    Colors.white.withValues(alpha: 0.38),
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(999),
                                ),
                              ),
                              child: const Text(
                                'Continue with Google',
                                style: TextStyle(
                                  fontSize: 17,
                                  fontWeight: FontWeight.w800,
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(height: 20),
                          TextButton(
                            onPressed: authControlsLocked
                                ? null
                                : () {
                                    final email = _emailController.text.trim();
                                    if (email.isEmpty) return;
                                    authNotifier.sendMagicLink(email);
                                  },
                            child: Text(
                              'Send magic link',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.86),
                                fontSize: 15,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                          const SizedBox(height: 26),
                          Row(
                            children: [
                              Expanded(
                                child: Divider(
                                  color: Colors.white.withValues(alpha: 0.14),
                                ),
                              ),
                              Padding(
                                padding:
                                    const EdgeInsets.symmetric(horizontal: 20),
                                child: Text(
                                  'or',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.58),
                                    fontSize: 15,
                                  ),
                                ),
                              ),
                              Expanded(
                                child: Divider(
                                  color: Colors.white.withValues(alpha: 0.14),
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 26),
                          SizedBox(
                            width: double.infinity,
                            height: 58,
                            child: OutlinedButton.icon(
                              onPressed: authControlsLocked
                                  ? null
                                  : () => authNotifier.signInWithGitHub(),
                              style: OutlinedButton.styleFrom(
                                foregroundColor: Colors.white,
                                disabledForegroundColor:
                                    Colors.white.withValues(alpha: 0.36),
                                side: BorderSide(
                                  color: Colors.white.withValues(alpha: 0.22),
                                ),
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(999),
                                ),
                              ),
                              icon: const Icon(Icons.code_rounded),
                              label: const Text(
                                'Continue with GitHub',
                                style: TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

class _AuthStatusPill extends StatelessWidget {
  const _AuthStatusPill({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      height: 54,
      padding: const EdgeInsets.symmetric(horizontal: 18),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: Colors.white.withValues(alpha: 0.1)),
      ),
      child: Row(
        children: [
          const SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: Colors.white,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              text,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.72),
                fontSize: 14,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _AuthTextField extends StatelessWidget {
  const _AuthTextField({
    required this.controller,
    required this.enabled,
    required this.icon,
    required this.hintText,
  });

  final TextEditingController controller;
  final bool enabled;
  final IconData icon;
  final String hintText;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 68,
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.24),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: Colors.white.withValues(alpha: 0.18)),
      ),
      child: TextField(
        controller: controller,
        enabled: enabled,
        keyboardType: TextInputType.emailAddress,
        style: const TextStyle(color: Colors.white, fontSize: 17),
        decoration: InputDecoration(
          border: InputBorder.none,
          prefixIcon: Icon(
            icon,
            color: Colors.white.withValues(alpha: 0.7),
            size: 25,
          ),
          hintText: hintText,
          hintStyle: TextStyle(
            color: Colors.white.withValues(alpha: 0.42),
            fontSize: 17,
          ),
          contentPadding: const EdgeInsets.symmetric(vertical: 22),
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
  int _lastHandledSongMatchIntentToken = 0;
  final HomeScreenController _homeController = HomeScreenController();
  final HomeScreenController _searchController = HomeScreenController();

  @override
  void dispose() {
    _homeController.dispose();
    _searchController.dispose();
    super.dispose();
  }

  void _openSearchSongMatchFlow({bool preferPendingShared = false}) {
    if (!mounted) return;
    if (_currentIndex != 1) {
      setState(() => _currentIndex = 1);
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _searchController.requestFocusSearch();
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _searchController.requestOpenSongMatchSheet(
          preferPendingShared: preferPendingShared,
        );
      });
    });
  }

  void _handleBottomSelection(int index) {
    if (_currentIndex != index) {
      setState(() => _currentIndex = index);
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (index == 0) {
        _homeController.requestShowHomeFeed();
      } else if (index == 1) {
        _searchController.requestFocusSearch();
      }
    });
  }

  bool _handleBackForCurrentTab() {
    if (_currentIndex == 1) {
      if (!_searchController.value.canHandleBack) return false;
      _searchController.requestHandleBack();
      return true;
    }
    if (_currentIndex == 0) {
      if (!_homeController.value.canHandleBack) return false;
      _homeController.requestHandleBack();
      return true;
    }
    return false;
  }

  Widget _buildBottomArea(bool hasActiveTrack) {
    final viewBottomInset = MediaQuery.of(context).viewPadding.bottom;
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
              ? Padding(
                  key: const ValueKey('mini-player'),
                  padding: EdgeInsets.fromLTRB(
                    18,
                    0,
                    18,
                    viewBottomInset > 0 ? 8 : 6,
                  ),
                  child: const MiniPlayer(),
                )
              : const SizedBox.shrink(key: ValueKey('mini-player-hidden')),
        ),
        AppBottomNavBar(
          currentIndex: _currentIndex,
          onSelected: _handleBottomSelection,
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
    final songMatchState = ref.watch(songMatchProvider);
    if (songMatchState.shareIntentToken > _lastHandledSongMatchIntentToken &&
        songMatchState.hasPendingSharedMedia) {
      _lastHandledSongMatchIntentToken = songMatchState.shareIntentToken;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _openSearchSongMatchFlow(preferPendingShared: true);
      });
    }
    final viewBottomInset = MediaQuery.of(context).viewPadding.bottom;
    final pages = <Widget>[
      HomeScreen(
        controller: _homeController,
        onOpenSearchTab: () => _handleBottomSelection(1),
        onOpenProfileTab: () => _handleBottomSelection(3),
      ),
      HomeScreen(
        controller: _searchController,
        searchOnly: true,
      ),
      LibraryScreen(onOpenSearchTab: () => _handleBottomSelection(1)),
      const NeatieProfileScreen(),
    ];

    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) {
        if (didPop) return;
        final handledByCurrentTab = _handleBackForCurrentTab();
        if (handledByCurrentTab) {
          return;
        }
        if (_currentIndex != 0) {
          setState(() => _currentIndex = 0);
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (!mounted) return;
            _homeController.requestShowHomeFeed();
          });
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
            Positioned.fill(
              child: NeatieBackground(
                child: FadeIndexedStack(
                  index: _currentIndex,
                  children: pages,
                ),
              ),
            ),
            Positioned(
              right: 24,
              bottom: hasActiveTrack
                  ? viewBottomInset + 148
                  : viewBottomInset + 24,
              child: const DownloadHud(),
            ),
          ],
        ),
        bottomNavigationBar: _buildBottomArea(hasActiveTrack),
      ),
    );
  }
}
