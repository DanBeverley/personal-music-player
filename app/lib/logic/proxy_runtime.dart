import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

const String _configuredProxyBaseUrl =
    String.fromEnvironment('AURALIS_PROXY_URL', defaultValue: '');
const String proxyUnavailableMessage =
    'Recommendation service is unavailable right now. Check the proxy connection and try again.';
const String searchUnavailableMessage =
    'Search could not complete right now. Try again in a moment.';
const String searchTimeoutMessage =
    'Search is taking longer than expected. Try again in a moment.';
const bool disableRecommendationClientTimeouts = true;
const bool disableSearchClientTimeouts = true;
const Duration recommendRequestTimeout = Duration(seconds: 45);
const Duration recommendRowPageTimeout = Duration(seconds: 30);
const String recommendTimeoutMessage =
    'Recommendation service took too long to respond. Please try again.';
const String _disableRecommendationTimeoutsRaw = String.fromEnvironment(
  'AURALIS_DISABLE_TIMEOUTS',
  defaultValue: '0',
);
const bool disableRecommendationTimeouts =
    _disableRecommendationTimeoutsRaw == '1' ||
        _disableRecommendationTimeoutsRaw == 'true' ||
        _disableRecommendationTimeoutsRaw == 'TRUE';

final http.Client appHttpClient = http.Client();
final http.Client proxyControlHttpClient = http.Client();

Future<T> runRecommendationRequest<T>(Future<T> future, Duration timeout) {
  if (disableRecommendationClientTimeouts) {
    return future;
  }
  return future.timeout(timeout);
}

Future<T> runSearchRequest<T>(Future<T> future, Duration timeout) {
  if (disableSearchClientTimeouts) {
    return future;
  }
  return future.timeout(timeout);
}

String _normalizeProxyBaseUrl(String baseUrl) {
  final trimmed = baseUrl.trim();
  if (trimmed.isEmpty) return trimmed;
  return trimmed.endsWith('/')
      ? trimmed.substring(0, trimmed.length - 1)
      : trimmed;
}

List<String> get proxyBaseUrlCandidates {
  final configured = _configuredProxyBaseUrl.trim();
  if (configured.isNotEmpty) {
    return <String>[_normalizeProxyBaseUrl(configured)];
  }

  final candidates = <String>[
    if (Platform.isAndroid) 'http://10.0.2.2:8000',
    'http://127.0.0.1:8000',
  ];
  final normalized = candidates
      .map(_normalizeProxyBaseUrl)
      .where((value) => value.isNotEmpty)
      .toSet()
      .toList(growable: false);
  return normalized.isEmpty
      ? const <String>['http://127.0.0.1:8000']
      : normalized;
}

String get proxyBaseUrl => proxyBaseUrlCandidates.first;

Uri buildProxyUri(String path) {
  final normalizedPath = path.startsWith('/') ? path : '/$path';
  return Uri.parse('$proxyBaseUrl$normalizedPath');
}

Future<bool> probeProxyHealth({
  Duration timeout = const Duration(seconds: 3),
}) async {
  for (final baseUrl in proxyBaseUrlCandidates) {
    try {
      final res = await proxyControlHttpClient
          .get(Uri.parse('$baseUrl/'))
          .timeout(timeout);
      if (res.statusCode >= 200 && res.statusCode < 500) {
        return true;
      }
    } catch (_) {
      // Try the next candidate endpoint.
    }
  }
  return false;
}

void debugProxyLog(String area, String message) {
  if (!kDebugMode) return;
  debugPrint('[EBB:$area] $message');
}

String compactDiagnosticValue(Object? value) {
  if (value == null) return 'null';
  try {
    final encoded = jsonEncode(value);
    if (encoded.length <= 480) {
      return encoded;
    }
    return '${encoded.substring(0, 480)}...';
  } catch (_) {
    return value.toString();
  }
}
