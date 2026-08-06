import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
  bool _isSignUp = false;
  bool _obscurePassword = true;
  final TextEditingController _fullNameController = TextEditingController();
  final TextEditingController _emailController = TextEditingController();
  final TextEditingController _passwordController = TextEditingController();
  final TextEditingController _confirmPasswordController =
      TextEditingController();

  @override
  void dispose() {
    _fullNameController.dispose();
    _emailController.dispose();
    _passwordController.dispose();
    _confirmPasswordController.dispose();
    super.dispose();
  }

  void _setAuthMode(bool isSignUp) {
    if (_isSignUp == isSignUp) return;
    setState(() => _isSignUp = isSignUp);
    ref.read(authProvider.notifier).clearError();
  }

  void _submit(AuthNotifier authNotifier) {
    if (_isSignUp) {
      unawaited(
        authNotifier.signUpWithPassword(
          fullName: _fullNameController.text,
          email: _emailController.text,
          password: _passwordController.text,
          confirmPassword: _confirmPasswordController.text,
        ),
      );
      return;
    }
    unawaited(
      authNotifier.signInWithPassword(
        _emailController.text,
        _passwordController.text,
      ),
    );
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
                  padding: const EdgeInsets.fromLTRB(10, 34, 10, 38),
                  child: ConstrainedBox(
                    constraints: BoxConstraints(
                      minHeight: constraints.maxHeight - 86,
                      maxWidth: 520,
                    ),
                    child: Align(
                      alignment: Alignment.bottomCenter,
                      child: Transform.scale(
                        scale: 0.88,
                        alignment: Alignment.topCenter,
                        child: Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                          Image.asset(
                            'assets/branding/neatie_3rd.png',
                            width: 88,
                            height: 58,
                            fit: BoxFit.contain,
                          ),
                          const SizedBox(height: 48),
                          AnimatedSize(
                            duration: const Duration(milliseconds: 420),
                            curve: Curves.easeInOutCubic,
                            child: AnimatedSwitcher(
                              duration: const Duration(milliseconds: 420),
                              switchInCurve: Curves.easeOutCubic,
                              switchOutCurve: Curves.easeInCubic,
                              transitionBuilder: (child, animation) {
                                final curved = CurvedAnimation(
                                  parent: animation,
                                  curve: Curves.easeOutCubic,
                                );
                                final slide = Tween<Offset>(
                                  begin: Offset(
                                      _isSignUp ? 0.035 : -0.035, 0.015),
                                  end: Offset.zero,
                                ).animate(curved);
                                final scale = Tween<double>(
                                  begin: 0.97,
                                  end: 1,
                                ).animate(curved);
                                return FadeTransition(
                                  opacity: curved,
                                  child: SlideTransition(
                                    position: slide,
                                    child: ScaleTransition(
                                      scale: scale,
                                      child: child,
                                    ),
                                  ),
                                );
                              },
                              child: NeatieSurface(
                              key: ValueKey<bool>(_isSignUp),
                            width: double.infinity,
                            padding: const EdgeInsets.fromLTRB(28, 28, 28, 28),
                            radius: 28,
                            color: Colors.black.withValues(alpha: 0.34),
                            blur: true,
                            bordered: true,
                            elevated: false,
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                if (_isSignUp) ...[
                                  _AuthTextField(
                                    controller: _fullNameController,
                                    enabled: !authControlsLocked,
                                    icon: Icons.person_outline_rounded,
                                    hintText: 'Username',
                                    keyboardType: TextInputType.name,
                                  ),
                                  const SizedBox(height: 18),
                                ],
                                _AuthTextField(
                                  controller: _emailController,
                                  enabled: !authControlsLocked,
                                  icon: Icons.mail_outline_rounded,
                                  hintText: 'you@domain.com',
                                  keyboardType: TextInputType.emailAddress,
                                ),
                                const SizedBox(height: 18),
                                _AuthTextField(
                                  controller: _passwordController,
                                  enabled: !authControlsLocked,
                                  icon: Icons.lock_outline_rounded,
                                  hintText: 'Password',
                                  obscureText: _obscurePassword,
                                  keyboardType: TextInputType.visiblePassword,
                                  suffixIcon: IconButton(
                                    onPressed: authControlsLocked
                                        ? null
                                        : () => setState(
                                              () => _obscurePassword =
                                                  !_obscurePassword,
                                            ),
                                    icon: Icon(
                                      _obscurePassword
                                          ? Icons.visibility_off_outlined
                                          : Icons.visibility_outlined,
                                    ),
                                    color: Colors.white.withValues(alpha: 0.58),
                                    tooltip: _obscurePassword
                                        ? 'Show password'
                                        : 'Hide password',
                                  ),
                                ),
                                if (_isSignUp) ...[
                                  const SizedBox(height: 18),
                                  _AuthTextField(
                                    controller: _confirmPasswordController,
                                    enabled: !authControlsLocked,
                                    icon: Icons.lock_outline_rounded,
                                    hintText: 'Confirm Password',
                                    obscureText: _obscurePassword,
                                    keyboardType: TextInputType.visiblePassword,
                                    suffixIcon: IconButton(
                                      onPressed: authControlsLocked
                                          ? null
                                          : () => setState(
                                                () => _obscurePassword =
                                                    !_obscurePassword,
                                              ),
                                      icon: Icon(
                                        _obscurePassword
                                            ? Icons.visibility_off_outlined
                                            : Icons.visibility_outlined,
                                      ),
                                      color: Colors.white.withValues(alpha: 0.58),
                                      tooltip: _obscurePassword
                                          ? 'Show password'
                                          : 'Hide password',
                                    ),
                                  ),
                                ],
                                const SizedBox(height: 28),
                                SizedBox(
                                  width: double.infinity,
                                  height: 62,
                                  child: FilledButton(
                                    onPressed: authControlsLocked
                                        ? null
                                        : () => _submit(authNotifier),
                                    style: FilledButton.styleFrom(
                                      backgroundColor: Colors.white,
                                      disabledBackgroundColor:
                                          Colors.white.withValues(alpha: 0.2),
                                      foregroundColor: Colors.black,
                                      disabledForegroundColor:
                                          Colors.white.withValues(alpha: 0.38),
                                      shape: RoundedRectangleBorder(
                                        borderRadius: BorderRadius.circular(15),
                                      ),
                                    ),
                                    child: Text(
                                      _isSignUp
                                          ? 'Create account'
                                          : 'Continue',
                                      style: const TextStyle(
                                        fontSize: 17,
                                        fontWeight: FontWeight.w800,
                                      ),
                                    ),
                                  ),
                                ),
                                if (!_isSignUp) ...[
                                  const SizedBox(height: 18),
                                  TextButton.icon(
                                    onPressed: authControlsLocked
                                        ? null
                                        : () => authNotifier.sendMagicLink(
                                              _emailController.text,
                                            ),
                                    style: TextButton.styleFrom(
                                      foregroundColor: Colors.white,
                                      disabledForegroundColor:
                                          Colors.white.withValues(alpha: 0.36),
                                    ),
                                    icon: const Icon(Icons.send_outlined,
                                        size: 22),
                                    label: const Text(
                                      'Send magic link',
                                      style: TextStyle(
                                        fontSize: 15,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                  ),
                                ],
                                const SizedBox(height: 24),
                                Row(
                                  children: [
                                    Expanded(
                                      child: Divider(
                                        color: Colors.white
                                            .withValues(alpha: 0.18),
                                      ),
                                    ),
                                    Padding(
                                      padding: const EdgeInsets.symmetric(
                                          horizontal: 20),
                                      child: Text(
                                        'or',
                                        style: TextStyle(
                                          color: Colors.white
                                              .withValues(alpha: 0.58),
                                          fontSize: 15,
                                        ),
                                      ),
                                    ),
                                    Expanded(
                                      child: Divider(
                                        color: Colors.white
                                            .withValues(alpha: 0.18),
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 24),
                                _OAuthButton(
                                  enabled: !authControlsLocked,
                                  isGoogle: true,
                                  label: _isSignUp
                                      ? 'Sign up with Google'
                                      : 'Continue with Google',
                                  onPressed: () =>
                                      authNotifier.signInWithGoogle(),
                                ),
                                const SizedBox(height: 18),
                                _OAuthButton(
                                  enabled: !authControlsLocked,
                                  label: _isSignUp
                                      ? 'Sign up with GitHub'
                                      : 'Continue with GitHub',
                                  onPressed: () =>
                                      authNotifier.signInWithGitHub(),
                                ),
                              ],
                              ),
                            ),
                          ),
                          ),
                          if (statusText.isNotEmpty) ...[
                            const SizedBox(height: 16),
                            _AuthStatusPill(text: statusText),
                          ],
                          if (authState.error != null &&
                              authState.error!.isNotEmpty) ...[
                            const SizedBox(height: 14),
                            Text(
                              authState.error!,
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.72),
                                fontSize: 13,
                                height: 1.35,
                              ),
                            ),
                          ],
                          const SizedBox(height: 30),
                          Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              Text(
                                _isSignUp
                                    ? 'Already had an account ?'
                                    : 'New here?',
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.58),
                                  fontSize: 16,
                                ),
                              ),
                              TextButton(
                                onPressed: authControlsLocked
                                    ? null
                                    : () => _setAuthMode(!_isSignUp),
                                style: TextButton.styleFrom(
                                  foregroundColor: Colors.white,
                                  padding: const EdgeInsets.only(left: 6),
                                  minimumSize: Size.zero,
                                  tapTargetSize:
                                      MaterialTapTargetSize.shrinkWrap,
                                ),
                                child: Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Text(
                                      _isSignUp ? 'Sign in' : 'Create account',
                                      style: const TextStyle(
                                        fontSize: 16,
                                        fontWeight: FontWeight.w800,
                                      ),
                                    ),
                                    const SizedBox(width: 2),
                                    const Icon(Icons.chevron_right_rounded,
                                        size: 22),
                                  ],
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
    this.keyboardType = TextInputType.text,
    this.obscureText = false,
    this.suffixIcon,
  });

  final TextEditingController controller;
  final bool enabled;
  final IconData icon;
  final String hintText;
  final TextInputType keyboardType;
  final bool obscureText;
  final Widget? suffixIcon;

  @override
  Widget build(BuildContext context) {
    return Container(
          height: 68,
          decoration: BoxDecoration(
            color: Colors.black.withValues(alpha: 0.24),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: Colors.white.withValues(alpha: 0.24)),
          ),
          child: TextField(
            controller: controller,
            enabled: enabled,
            obscureText: obscureText,
            keyboardType: keyboardType,
            style: const TextStyle(color: Colors.white, fontSize: 17),
            decoration: InputDecoration(
              border: InputBorder.none,
              prefixIcon: Icon(
                icon,
                color: Colors.white.withValues(alpha: 0.7),
                size: 25,
              ),
              suffixIcon: suffixIcon,
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

class _OAuthButton extends StatelessWidget {
  const _OAuthButton({
    required this.enabled,
    required this.label,
    required this.onPressed,
    this.isGoogle = false,
  });

  final bool enabled;
  final String label;
  final VoidCallback onPressed;
  final bool isGoogle;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 58,
      child: OutlinedButton.icon(
        onPressed: enabled ? onPressed : null,
      style: OutlinedButton.styleFrom(
          backgroundColor: Colors.black.withValues(alpha: 0.18),
          disabledBackgroundColor: Colors.white.withValues(alpha: 0.06),
          foregroundColor: Colors.white,
          disabledForegroundColor: Colors.white.withValues(alpha: 0.36),
          side: BorderSide(color: Colors.white.withValues(alpha: 0.24)),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
          ),
        ),
        icon: CustomPaint(
          size: const Size.square(22),
          painter: isGoogle
              ? const _GoogleMarkPainter(color: Colors.white)
              : const _GithubMarkPainter(color: Colors.white),
        ),
        label: Text(
          label,
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        ),
      ),
    );
  }
}

class _GoogleMarkPainter extends CustomPainter {
  const _GoogleMarkPainter({required this.color});

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.fill;
    final path = Path()
      ..moveTo(21.35, 11.1)
      ..lineTo(12.17, 11.1)
      ..lineTo(12.17, 14.08)
      ..lineTo(17.49, 14.08)
      ..cubicTo(17.26, 15.64, 15.67, 18.65, 12.17, 18.65)
      ..cubicTo(8.97, 18.65, 6.36, 16, 6.36, 13)
      ..cubicTo(6.36, 10, 8.97, 7.35, 12.17, 7.35)
      ..cubicTo(14, 7.35, 15.23, 8.13, 15.93, 8.8)
      ..lineTo(18.49, 6.31)
      ..cubicTo(16.7, 4.76, 14.7, 3.75, 12.17, 3.75)
      ..cubicTo(7.56, 3.75, 3.83, 7.48, 3.83, 12)
      ..cubicTo(3.83, 16.52, 7.56, 20.25, 12.17, 20.25)
      ..cubicTo(17.18, 20.25, 20.51, 16.73, 20.51, 11.77)
      ..cubicTo(20.51, 11.2, 20.45, 10.78, 20.35, 10.36)
      ..close();
    final scale = size.shortestSide / 24;
    canvas.save();
    canvas.scale(scale, scale);
    canvas.drawPath(path, paint);
    canvas.restore();
  }

  @override
  bool shouldRepaint(_GoogleMarkPainter oldDelegate) =>
      oldDelegate.color != color;
}

class _GithubMarkPainter extends CustomPainter {
  const _GithubMarkPainter({required this.color});

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()..color = color;
    final path = Path()
      ..moveTo(8, 0)
      ..cubicTo(3.58, 0, 0, 3.58, 0, 8)
      ..cubicTo(0, 11.54, 2.29, 14.53, 5.47, 15.59)
      ..cubicTo(5.87, 15.66, 6.02, 15.42, 6.02, 15.21)
      ..cubicTo(6.02, 15.02, 6.01, 14.39, 6.01, 13.62)
      ..cubicTo(4, 13.99, 3.48, 13.13, 3.32, 12.68)
      ..cubicTo(3.23, 12.45, 2.84, 11.74, 2.5, 11.55)
      ..cubicTo(2.22, 11.4, 1.82, 11.03, 2.49, 11.02)
      ..cubicTo(3.12, 11.01, 3.58, 11.61, 3.73, 11.85)
      ..cubicTo(4.45, 13.06, 5.6, 12.72, 6.06, 12.51)
      ..cubicTo(6.13, 11.99, 6.34, 11.64, 6.57, 11.44)
      ..cubicTo(4.79, 11.24, 2.93, 10.55, 2.93, 7.49)
      ..cubicTo(2.93, 6.62, 3.24, 5.91, 3.75, 5.35)
      ..cubicTo(3.67, 5.15, 3.39, 4.33, 3.83, 3.23)
      ..cubicTo(3.83, 3.23, 4.5, 3.02, 6.03, 4.05)
      ..cubicTo(6.67, 3.87, 7.35, 3.78, 8, 3.78)
      ..cubicTo(8.65, 3.78, 9.33, 3.87, 9.97, 4.05)
      ..cubicTo(11.5, 3.02, 12.17, 3.23, 12.17, 3.23)
      ..cubicTo(12.61, 4.33, 12.33, 5.15, 12.25, 5.35)
      ..cubicTo(12.76, 5.91, 13.07, 6.62, 13.07, 7.49)
      ..cubicTo(13.07, 10.56, 11.2, 11.24, 9.42, 11.44)
      ..cubicTo(9.71, 11.69, 9.96, 12.17, 9.96, 12.91)
      ..cubicTo(9.96, 13.97, 9.95, 14.83, 9.95, 15.21)
      ..cubicTo(9.95, 15.42, 10.1, 15.66, 10.5, 15.59)
      ..cubicTo(13.71, 14.53, 16, 11.54, 16, 8)
      ..cubicTo(16, 3.58, 12.42, 0, 8, 0)
      ..close();
    final scale = size.shortestSide / 16;
    canvas.save();
    canvas.scale(scale, scale);
    canvas.drawPath(path, paint);
    canvas.restore();
  }

  @override
  bool shouldRepaint(_GithubMarkPainter oldDelegate) =>
      oldDelegate.color != color;
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
