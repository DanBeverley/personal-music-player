import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../logic/charts_provider.dart' show chartCountries;
import 'playback_port.dart';

/// Standalone public lookup: never uses MusicApi's authentication/diagnostics.
Future<String?> detectChartCountry({http.Client? client}) async {
  final owned = client == null;
  final transport = client ?? http.Client();
  final cancel = Completer<void>();
  final timeout = Timer(const Duration(seconds: 3), () => cancel.complete());
  try {
    final response = await transport.send(http.AbortableRequest(
        'GET', Uri.parse('https://api.country.is/'),
        abortTrigger: cancel.future));
    if (response.statusCode != 200) return null;
    final bytes = <int>[];
    await for (final chunk in response.stream) {
      if (bytes.length + chunk.length > 4096) return null;
      bytes.addAll(chunk);
    }
    final data = jsonDecode(utf8.decode(bytes));
    final country = data is Map ? '${data['country'] ?? ''}'.toUpperCase() : '';
    return RegExp(r'^[A-Z]{2}$').hasMatch(country) ? country : null;
  } catch (_) {
    return null;
  } finally {
    timeout.cancel();
    if (!cancel.isCompleted) cancel.complete();
    if (owned) transport.close();
  }
}

/// Browser preference, separate from accounts and playback persistence.
class ChartRegion extends ChangeNotifier {
  ChartRegion({this.store, this.detect}) {
    try {
      final saved = jsonDecode(store?.read() ?? '{}');
      if (saved is Map && saved['version'] == 1) {
        final choice = '${saved['selection'] ?? 'AUTO'}';
        if (choice == 'AUTO' || chartCountries.containsKey(choice))
          selection = choice;
        final country = '${saved['country'] ?? 'GLOBAL'}';
        if (chartCountries.containsKey(country)) this.country = country;
        if (selection != 'AUTO') this.country = selection;
        _saved = true;
      }
    } catch (_) {}
  }
  final SessionStore? store;
  final Future<String?> Function()? detect;
  String selection = 'AUTO', country = 'GLOBAL';
  bool detecting = false, _started = false, _disposed = false;
  bool _saved = false;
  String outcome = '';
  int _version = 0;
  String get description => selection != 'AUTO'
      ? _saved
          ? 'Your choice · saved in this browser'
          : 'Your choice · browser storage unavailable'
      : detecting
          ? 'Finding your country…'
          : outcome == 'detected'
              ? 'Automatic · approximate IP country'
              : outcome == 'unsupported'
                  ? 'Regional charts are not supported here yet. Choose a region.'
                  : 'Automatic location unavailable. Choose a region.';

  Future<void> initialize() async {
    if (_started || _disposed) return;
    _started = true;
    if (selection == 'AUTO') await _detect();
  }

  Future<void> choose(String value) async {
    if (_disposed || (value != 'AUTO' && !chartCountries.containsKey(value)))
      return;
    _version++;
    selection = value;
    detecting = false;
    if (value != 'AUTO') country = value;
    _save();
    notifyListeners();
    if (value == 'AUTO') await _detect();
  }

  Future<void> _detect() async {
    final version = ++_version;
    detecting = true;
    notifyListeners();
    String? found;
    try {
      found = (await detect?.call().timeout(const Duration(seconds: 4)))
          ?.trim()
          .toUpperCase();
    } catch (_) {}
    if (_disposed || version != _version || selection != 'AUTO') return;
    detecting = false;
    if (found != null && chartCountries.containsKey(found)) {
      country = found;
      outcome = 'detected';
    } else {
      country = 'GLOBAL';
      outcome = found == null ? 'unavailable' : 'unsupported';
    }
    _save();
    notifyListeners();
  }

  void _save() {
    try {
      _saved = false;
      store?.write(jsonEncode(
          {'version': 1, 'selection': selection, 'country': country}));
      _saved = store != null;
    } catch (_) {}
  }

  @override
  void dispose() {
    _disposed = true;
    _version++;
    super.dispose();
  }
}
