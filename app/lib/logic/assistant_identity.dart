import 'dart:math';
import 'package:shared_preferences/shared_preferences.dart';

/// A private guest capability, separate from the public recommendation scope.
/// Only AI requests need this; playback never waits for preferences.
Future<String>? _guestKey;
Future<String> _loadGuestKey() async {
  final preferences = await SharedPreferences.getInstance();
  const name = 'neatie.assistant.guestCapability.v1';
  final saved = preferences.getString(name);
  if (saved != null && RegExp(r'^[a-f0-9]{64}$').hasMatch(saved)) return saved;
  final random = Random.secure();
  final key = List.generate(
      32, (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
  await preferences.setString(name, key);
  return key;
}

Future<Map<String, String>> assistantHeaders({String? accessToken}) async => {
      'Content-Type': 'application/json',
      if (accessToken != null && accessToken.isNotEmpty)
        'Authorization': 'Bearer $accessToken'
      else
        'X-Neatie-Guest-Key': await (_guestKey ??= _loadGuestKey()),
    };
