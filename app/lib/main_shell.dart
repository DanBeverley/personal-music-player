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
import 'ui/app_theme_tokens.dart';
import 'widgets/app_bottom_nav_bar.dart';
import 'widgets/download_hud.dart';
import 'widgets/player/mini_player.dart';

const _surfaceGreyAlt = appSurfaceGreyAlt;
const _voidBlack = appVoidBlack;
const double _radiusLarge = appRadiusLarge;
const double _radiusMedium = appRadiusMedium;

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
      ),
      HomeScreen(
        controller: _searchController,
        searchOnly: true,
      ),
      const LibraryScreen(),
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
            const Positioned.fill(child: ColoredBox(color: _voidBlack)),
            FadeIndexedStack(
              index: _currentIndex,
              children: pages,
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
