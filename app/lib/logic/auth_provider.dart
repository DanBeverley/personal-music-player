import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

const String supabaseUrl =
    String.fromEnvironment('SUPABASE_URL', defaultValue: '');
const String supabaseAnonKey =
    String.fromEnvironment('SUPABASE_ANON_KEY', defaultValue: '');
const String supabaseRedirectScheme =
    String.fromEnvironment('SUPABASE_REDIRECT_SCHEME', defaultValue: 'ebb');
const String supabaseRedirectHost =
    String.fromEnvironment('SUPABASE_REDIRECT_HOST', defaultValue: 'login-callback');

bool _supabaseInitialized = false;
const Object _authStateUnset = Object();

bool get isSupabaseConfigured =>
    supabaseUrl.trim().isNotEmpty && supabaseAnonKey.trim().isNotEmpty;

String get supabaseRedirectUrl =>
    '$supabaseRedirectScheme://$supabaseRedirectHost';

SupabaseClient? get supabaseClientOrNull {
  if (!isSupabaseConfigured || !_supabaseInitialized) return null;
  try {
    return Supabase.instance.client;
  } catch (_) {
    return null;
  }
}

String? get currentAuthenticatedUserId =>
    supabaseClientOrNull?.auth.currentUser?.id;

Future<void> initSupabase() async {
  await migrateLegacyStorageIfNeeded();
  if (!isSupabaseConfigured || _supabaseInitialized) return;
  await Supabase.initialize(
    url: supabaseUrl,
    anonKey: supabaseAnonKey,
  );
  _supabaseInitialized = true;
}

String _sanitizeScopeSegment(String value) {
  return value.replaceAll(RegExp(r'[^A-Za-z0-9_\-]'), '_');
}

Future<Directory> getScopedUserDirectory([String? scopeId]) async {
  final dir = await getApplicationDocumentsDirectory();
  final resolvedScope = _sanitizeScopeSegment(
    scopeId ?? currentAuthenticatedUserId ?? 'guest',
  );
  final userDir = Directory('${dir.path}/users/$resolvedScope');
  if (!userDir.existsSync()) {
    userDir.createSync(recursive: true);
  }
  return userDir;
}

Future<Directory> getScopedDownloadsDirectory([String? scopeId]) async {
  final userDir = await getScopedUserDirectory(scopeId);
  final downloadsDir = Directory('${userDir.path}/downloads');
  if (!downloadsDir.existsSync()) {
    downloadsDir.createSync(recursive: true);
  }
  return downloadsDir;
}

Future<Directory> getScopedPlaylistCoversDirectory([String? scopeId]) async {
  final userDir = await getScopedUserDirectory(scopeId);
  final coversDir = Directory('${userDir.path}/playlist_covers');
  if (!coversDir.existsSync()) {
    coversDir.createSync(recursive: true);
  }
  return coversDir;
}

Future<File> getScopedDataFile(String fileName, [String? scopeId]) async {
  final userDir = await getScopedUserDirectory(scopeId);
  return File('${userDir.path}/$fileName');
}

Future<void> _copyFileIfMissing(File source, File destination) async {
  if (!source.existsSync() || destination.existsSync()) return;
  if (!destination.parent.existsSync()) {
    destination.parent.createSync(recursive: true);
  }
  await source.copy(destination.path);
}

Future<void> _copyDirectoryRecursively(
  Directory source,
  Directory destination,
) async {
  if (!source.existsSync()) return;
  if (!destination.existsSync()) {
    destination.createSync(recursive: true);
  }
  for (final entity in source.listSync(followLinks: false)) {
    final name = entity.uri.pathSegments.isEmpty
        ? ''
        : entity.uri.pathSegments.last;
    if (name.isEmpty) continue;
    if (entity is File) {
      await _copyFileIfMissing(entity, File('${destination.path}/$name'));
      continue;
    }
    if (entity is Directory) {
      await _copyDirectoryRecursively(
        entity,
        Directory('${destination.path}/$name'),
      );
    }
  }
}

bool _directoryHasContent(Directory directory) {
  if (!directory.existsSync()) return false;
  return directory.listSync(followLinks: false).isNotEmpty;
}

Future<void> migrateLegacyStorageIfNeeded() async {
  final docsDir = await getApplicationDocumentsDirectory();
  final marker = File('${docsDir.path}/users/.phase1_legacy_migrated');
  if (marker.existsSync()) return;

  final guestUserDir = await getScopedUserDirectory('guest');
  final guestDownloadsDir = await getScopedDownloadsDirectory('guest');
  final guestCoversDir = await getScopedPlaylistCoversDirectory('guest');

  final legacyHistory = File('${docsDir.path}/history.json');
  final legacyPlaylists = File('${docsDir.path}/playlists.json');
  await _copyFileIfMissing(
    legacyHistory,
    File('${guestUserDir.path}/history.json'),
  );
  await _copyFileIfMissing(
    legacyPlaylists,
    File('${guestUserDir.path}/playlists.json'),
  );

  final legacyCoversDir = Directory('${docsDir.path}/playlist_covers');
  if (legacyCoversDir.existsSync()) {
    await _copyDirectoryRecursively(legacyCoversDir, guestCoversDir);
  }

  for (final entity in docsDir.listSync(followLinks: false)) {
    if (entity is! File || !entity.path.endsWith('.mp3')) continue;
    final fileName = entity.uri.pathSegments.last;
    await _copyFileIfMissing(entity, File('${guestDownloadsDir.path}/$fileName'));
    final jsonCompanion = File(entity.path.replaceAll('.mp3', '.json'));
    await _copyFileIfMissing(
      jsonCompanion,
      File('${guestDownloadsDir.path}/${fileName.replaceAll('.mp3', '.json')}'),
    );
  }

  if (!marker.parent.existsSync()) {
    marker.parent.createSync(recursive: true);
  }
  await marker.writeAsString(DateTime.now().toUtc().toIso8601String());
}

Future<void> migrateGuestScopeToUser(String userId) async {
  if (userId.isEmpty) return;
  final guestDir = await getScopedUserDirectory('guest');
  if (!_directoryHasContent(guestDir)) return;

  final userDir = await getScopedUserDirectory(userId);
  final marker = File('${userDir.path}/.guest_scope_imported');
  if (marker.existsSync()) return;

  final existingUserEntries = userDir
      .listSync(followLinks: false)
      .where((entity) => !entity.path.endsWith('.guest_scope_imported'))
      .toList(growable: false);
  if (existingUserEntries.isNotEmpty) {
    await marker.writeAsString('existing');
    return;
  }

  await _copyDirectoryRecursively(guestDir, userDir);
  await marker.writeAsString(DateTime.now().toUtc().toIso8601String());
}

class AppAuthState {
  final bool isConfigured;
  final bool isInitialized;
  final Session? session;
  final User? user;
  final bool isBusy;
  final String? error;

  const AppAuthState({
    required this.isConfigured,
    this.isInitialized = false,
    this.session,
    this.user,
    this.isBusy = false,
    this.error,
  });

  bool get isAuthenticated => user != null;
  String get storageScopeId => user?.id ?? 'guest';

  AppAuthState copyWith({
    bool? isConfigured,
    bool? isInitialized,
    Object? session = _authStateUnset,
    Object? user = _authStateUnset,
    bool? isBusy,
    Object? error = _authStateUnset,
    bool clearError = false,
  }) {
    return AppAuthState(
      isConfigured: isConfigured ?? this.isConfigured,
      isInitialized: isInitialized ?? this.isInitialized,
      session: identical(session, _authStateUnset)
          ? this.session
          : session as Session?,
      user: identical(user, _authStateUnset) ? this.user : user as User?,
      isBusy: isBusy ?? this.isBusy,
      error: clearError
          ? null
          : identical(error, _authStateUnset)
              ? this.error
              : error as String?,
    );
  }
}

class AuthNotifier extends StateNotifier<AppAuthState> {
  final Ref ref;
  StreamSubscription<AuthState>? _authSubscription;

  AuthNotifier(this.ref)
      : super(
          AppAuthState(
            isConfigured: isSupabaseConfigured,
            isInitialized: !isSupabaseConfigured || _supabaseInitialized,
            session: supabaseClientOrNull?.auth.currentSession,
            user: supabaseClientOrNull?.auth.currentUser,
          ),
        ) {
    final client = supabaseClientOrNull;
    if (client == null) {
      state = state.copyWith(isInitialized: true);
      return;
    }

    state = state.copyWith(
      isInitialized: true,
      session: client.auth.currentSession,
      user: client.auth.currentUser,
    );
    final currentUser = client.auth.currentUser;
    if (currentUser != null) {
      unawaited(_handleSignedInUser(currentUser));
    }

    _authSubscription = client.auth.onAuthStateChange.listen((authState) {
      state = state.copyWith(
        session: authState.session,
        user: authState.session?.user,
        isInitialized: true,
        isBusy: false,
        clearError: true,
      );
      final user = authState.session?.user;
      if (user != null) {
        unawaited(_handleSignedInUser(user));
      }
    });
  }

  Future<void> _handleSignedInUser(User user) async {
    try {
      await migrateLegacyStorageIfNeeded();
      await migrateGuestScopeToUser(user.id);
      await _ensureProfile(user);
      ref.read(storageRefreshTickProvider.notifier).state++;
    } catch (error) {
      debugPrint('Signed-in user bootstrap failed: $error');
    }
  }

  Future<void> _ensureProfile(User user) async {
    final client = supabaseClientOrNull;
    if (client == null) return;
    final metadata = user.userMetadata ?? const <String, dynamic>{};
    final displayName = metadata['full_name']?.toString() ??
        metadata['name']?.toString() ??
        user.email?.split('@').first ??
        'EBB Listener';
    final avatarUrl = metadata['avatar_url']?.toString() ??
        metadata['picture']?.toString();
    try {
      await client.from('profiles').upsert(
        {
          'id': user.id,
          'email': user.email,
          'display_name': displayName,
          'avatar_url': avatarUrl,
          'updated_at': DateTime.now().toUtc().toIso8601String(),
        },
        onConflict: 'id',
      );
    } catch (error) {
      debugPrint('Profile upsert failed: $error');
    }
  }

  Future<void> signInWithOAuth(OAuthProvider provider) async {
    final client = supabaseClientOrNull;
    if (client == null) return;
    state = state.copyWith(isBusy: true, clearError: true);
    try {
      await client.auth.signInWithOAuth(
        provider,
        redirectTo: kIsWeb ? null : supabaseRedirectUrl,
      );
    } catch (error) {
      state = state.copyWith(
        isBusy: false,
        error: error.toString(),
      );
    }
  }

  Future<void> signInWithGoogle() => signInWithOAuth(OAuthProvider.google);

  Future<void> signInWithGitHub() => signInWithOAuth(OAuthProvider.github);

  Future<void> sendMagicLink(String email) async {
    final client = supabaseClientOrNull;
    if (client == null) return;
    state = state.copyWith(isBusy: true, clearError: true);
    try {
      await client.auth.signInWithOtp(
        email: email,
        emailRedirectTo: kIsWeb ? null : supabaseRedirectUrl,
      );
      state = state.copyWith(
        isBusy: false,
        error: 'Magic link sent to $email',
      );
    } catch (error) {
      state = state.copyWith(
        isBusy: false,
        error: error.toString(),
      );
    }
  }

  Future<void> signOut() async {
    final client = supabaseClientOrNull;
    if (client == null) return;
    state = state.copyWith(isBusy: true, clearError: true);
    try {
      await client.auth.signOut();
      state = state.copyWith(
        session: null,
        user: null,
        isBusy: false,
      );
    } catch (error) {
      state = state.copyWith(
        isBusy: false,
        error: error.toString(),
      );
    }
  }

  @override
  void dispose() {
    _authSubscription?.cancel();
    super.dispose();
  }
}

final storageRefreshTickProvider = StateProvider<int>((ref) => 0);

final authProvider = StateNotifierProvider<AuthNotifier, AppAuthState>((ref) {
  return AuthNotifier(ref);
});
