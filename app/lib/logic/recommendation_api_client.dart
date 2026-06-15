import 'dart:convert';

import 'package:http/http.dart' as http;

import 'proxy_runtime.dart';

Future<http.Response> postRecommendation(
  Map<String, dynamic> body, {
  Duration timeout = recommendRequestTimeout,
}) {
  return runRecommendationRequest(
    proxyControlHttpClient.post(
      buildProxyUri('/recommend'),
      headers: const {'Content-Type': 'application/json'},
      body: jsonEncode(body),
    ),
    timeout,
  );
}
