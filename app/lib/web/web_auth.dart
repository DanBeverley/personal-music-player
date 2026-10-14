import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

/// Browser-only authentication state backed by Supabase's persisted web session.
class WebAuth extends ChangeNotifier {
  WebAuth._(this._client, {required this.configured}) {
    _syncFromClient();
    _subscription = _client?.auth.onAuthStateChange.listen((state) {
      _session = state.session ?? _client?.auth.currentSession;
      _user = _session?.user ?? _client?.auth.currentUser;
      if (state.event == AuthChangeEvent.signedOut) {
        _session = null;
        _user = null;
      }
      if (state.event == AuthChangeEvent.passwordRecovery)
        _passwordRecovery = true;
      if (state.event == AuthChangeEvent.signedOut) _passwordRecovery = false;
      _busy = false;
      _error = null;
      notifyListeners();
    }, onError: (Object error) {
      _busy = false;
      _error = _safeError(error);
      notifyListeners();
    });
  }

  static Future<WebAuth>? _initializing;

  /// Creates the shared browser adapter. Only the public anonymous key is read.
  static Future<WebAuth> initialize() => _initializing ??= _initialize();

  static Future<WebAuth> _initialize() async {
    const url = String.fromEnvironment('SUPABASE_URL', defaultValue: '');
    const anonKey =
        String.fromEnvironment('SUPABASE_ANON_KEY', defaultValue: '');
    if (url.trim().isEmpty || anonKey.trim().isEmpty) {
      return WebAuth._(null, configured: false);
    }

    // The SDK can complete the callback before this adapter subscribes.
    final recoveryRequested = Uri.base.queryParameters['flow'] == 'recovery' ||
        Uri.base.fragment.split('&').contains('type=recovery');
    SupabaseClient? client;
    try {
      await Supabase.initialize(url: url.trim(), anonKey: anonKey.trim());
      client = Supabase.instance.client;
    } catch (_) {
      // A host may have initialized Supabase before this adapter is created.
      // Reuse that client when it is available, without exposing setup details.
      try {
        client = Supabase.instance.client;
      } catch (_) {
        return WebAuth._(null, configured: false);
      }
    }
    final adapter = WebAuth._(client, configured: true);
    adapter._passwordRecovery =
        recoveryRequested && client.auth.currentSession != null;
    return adapter;
  }

  final SupabaseClient? _client;
  final bool configured;
  StreamSubscription<AuthState>? _subscription;
  Session? _session;
  User? _user;
  bool _busy = false;
  String? _error;
  bool _passwordRecovery = false;

  SupabaseClient? get client => _client;
  String? get userId => _user?.id;
  String? get email => _user?.email;
  String? get displayName =>
      (_user?.userMetadata?['full_name'] ?? _user?.userMetadata?['name'])
          ?.toString();
  String? get avatarUrl {
    final value =
        (_user?.userMetadata?['avatar_url'] ?? _user?.userMetadata?['picture'])
            ?.toString();
    final uri = Uri.tryParse(value ?? '');
    return uri != null && uri.scheme == 'https' && uri.host.isNotEmpty
        ? uri.toString()
        : null;
  }

  String get initial {
    final name = displayName?.trim() ?? '';
    final value = name.isEmpty ? email?.trim() ?? 'N' : name;
    return value.isEmpty ? 'N' : value.substring(0, 1).toUpperCase();
  }

  bool get busy => _busy;
  String? get error => _error;
  bool get passwordRecovery => _passwordRecovery;

  void _syncFromClient() {
    _session = _client?.auth.currentSession;
    _user = _client?.auth.currentUser ?? _session?.user;
  }

  String? _validateEmail(String value) {
    final email = value.trim();
    if (email.isEmpty) return 'Enter your email address.';
    if (!RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(email)) {
      return 'Enter a valid email address.';
    }
    return null;
  }

  bool _begin({required String email, String? password}) {
    final validation = _validateEmail(email);
    if (validation != null) {
      _error = validation;
      notifyListeners();
      return false;
    }
    if (password != null && password.length < 6) {
      _error = 'Your password must be at least 6 characters.';
      notifyListeners();
      return false;
    }
    if (_client == null) {
      _error = 'Authentication is not configured.';
      notifyListeners();
      return false;
    }
    _busy = true;
    _error = null;
    notifyListeners();
    return true;
  }

  Future<void> signIn(String email, String password) async {
    if (!_begin(email: email, password: password)) return;
    try {
      await _client!.auth.signInWithPassword(
        email: email.trim(),
        password: password,
      );
      _syncFromClient();
      _error = null;
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> signUp(String email, String password) async {
    if (!_begin(email: email, password: password)) return;
    try {
      await _client!.auth.signUp(
        email: email.trim(),
        password: password,
        emailRedirectTo: Uri.base.origin,
      );
      _syncFromClient();
      _error = null;
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> resetPassword(String email) async {
    if (!_begin(email: email)) return;
    try {
      await _client!.auth.resetPasswordForEmail(
        email.trim(),
        redirectTo: '${Uri.base.origin}/?flow=recovery',
      );
      _error = null;
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> signInWithGoogle() => _signInWithOAuth(OAuthProvider.google);

  Future<void> signInWithGitHub() => _signInWithOAuth(OAuthProvider.github);

  Future<void> _signInWithOAuth(OAuthProvider provider) async {
    if (_client == null) {
      _error = 'Authentication is not configured.';
      notifyListeners();
      return;
    }
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      await _client.auth.signInWithOAuth(
        provider,
        redirectTo: Uri.base.origin,
      );
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> updatePassword(String password) async {
    if (password.length < 6) {
      _error = 'Your password must be at least 6 characters.';
      notifyListeners();
      return;
    }
    if (_client == null) {
      _error = 'Authentication is not configured.';
      notifyListeners();
      return;
    }
    if (!_passwordRecovery && _user == null) {
      _error = 'Open the password reset link from your email first.';
      notifyListeners();
      return;
    }
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      await _client.auth.updateUser(UserAttributes(password: password));
      _passwordRecovery = false;
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> signOut() async {
    if (_client == null) return;
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      await _client.auth.signOut();
      _session = null;
      _user = null;
      _passwordRecovery = false;
    } catch (error) {
      _error = _safeError(error);
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  String _safeError(Object error) {
    final message = error.toString().toLowerCase();
    if (message.contains('invalid login credentials')) {
      return 'The email or password is incorrect.';
    }
    if (message.contains('email not confirmed')) {
      return 'Please verify your email before signing in.';
    }
    if (message.contains('already registered')) {
      return 'An account with this email already exists.';
    }
    if (message.contains('rate limit') || message.contains('too many')) {
      return 'Too many attempts. Please try again later.';
    }
    if (message.contains('provider') || message.contains('oauth')) {
      return 'This sign-in provider is unavailable. Please use another method.';
    }
    return 'Authentication is unavailable. Please try again.';
  }

  @override
  void dispose() {
    _subscription?.cancel();
    super.dispose();
  }
}
