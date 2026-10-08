import 'dart:convert';

/// Bounded, scoped technical reports. No automatic upload or account identity.
class PlaybackDiagnostics {
  static const _allowed = {
    'at',
    'event',
    'track',
    'session',
    'time',
    'paused',
    'muted',
    'volume',
    'ready',
    'network',
    'mediaCode',
    'mediaMessage',
    'bufferedEnd',
    'visibility',
    'audioSession',
    'repeatOne',
    'sourceMatches',
    'elementSession',
    'selectedTrack',
    'loadedTrack',
    'queueIndex',
    'operation',
    'loading',
    'startupInterrupted',
    'cause',
    'errorType',
    'exceptionName',
    'exceptionMessage',
    'awaitingFirstPlayback',
    'hiddenDuringStartup',
    'path',
    'ms',
    'requestedTrack',
    'status',
    'action',
    'kind',
    'reason',
    'error',
    'failed',
    'prepared',
    'fullyCached',
    'build',
    'phase',
    'playRequested',
    'pauseMs',
    'resume',
    'sourceAgeSeconds',
    'secondsSinceProgress',
    'recoveryAttempts'
  };
  final List<Map<String, dynamic>> _recent = [];
  final List<Map<String, dynamic>> _incidents = [];
  final Map<String, dynamic> device;
  int _serial = 0;

  PlaybackDiagnostics(this.device);

  static String safeText(String value) {
    final text = value
        .replaceAll(RegExp(r'https?://[^\s]+'), '[url]')
        .replaceAll(
            RegExp(r'Bearer\s+\S+', caseSensitive: false), '[credential]')
        .replaceAll(
            RegExp(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
            '[credential]');
    final output = StringBuffer();
    var bytes = 0;
    for (final rune in text.runes) {
      final character = String.fromCharCode(rune);
      // Budget the encoded JSON bytes, including escapes in error messages.
      bytes += rune < 0x20
          ? const {8, 9, 10, 12, 13}.contains(rune)
              ? 2
              : 6
          : rune == 0x22 || rune == 0x5c
              ? 2
              : utf8.encode(character).length;
      if (bytes > 320) break;
      output.write(character);
    }
    return output.toString();
  }

  Map<String, dynamic> _device(Map? raw) => {
        for (final key in ['userAgent', 'platform', 'language'])
          if (raw?[key] != null) key: safeText('${raw![key]}'),
      };

  Map<String, dynamic> _safe(Map row) => {
        for (final entry in row.entries)
          if (_allowed.contains(entry.key) &&
              (entry.value == null ||
                  entry.value is String ||
                  entry.value is bool ||
                  entry.value is num && (entry.value as num).isFinite))
            entry.key.toString():
                entry.value is String ? safeText(entry.value) : entry.value,
      };

  void restore(String? saved) {
    _recent.clear();
    _incidents.clear();
    if (saved == null || utf8.encode(saved).length > 65536) return;
    try {
      final decoded = jsonDecode(saved);
      final entries = decoded is List ? decoded : decoded['incidents'] as List;
      for (final item in entries.reversed.take(4).toList().reversed) {
        final rows = item is List ? item : item['events'] as List;
        final events = rows.whereType<Map>().take(24).map(_safe).toList();
        if (events.isEmpty) continue;
        _incidents.add({
          'id': item is Map ? safeText('${item['id']}') : 'legacy-${_serial++}',
          'reason':
              item is Map ? safeText('${item['reason']}') : 'previous-failure',
          'at': item is Map && item['at'] is String
              ? safeText(item['at'])
              : events.last['at'],
          'track': events.last['track'],
          'session': events.last['session'],
          'events': events,
          'device': item is Map
              ? _device(item['device'] as Map?)
              : {'userAgent': 'not retained'},
          'context': item is Map && item['context'] is Map
              ? _safe(item['context'])
              : events.last
        });
      }
    } catch (_) {/* Invalid/legacy storage must not block playback. */}
  }

  void record(Map<String, dynamic> row) {
    final safe = _safe(row);
    _recent.add(safe);
    if (_recent.length > 80) _recent.removeAt(0);
    // Keep a short aftermath, without appending unrelated preparation or tracks.
    if (_incidents.isNotEmpty) {
      final latest = _incidents.last;
      final events = latest['events'] as List;
      if (events.length < 24 &&
          latest['session'] == safe['session'] &&
          latest['track'] == safe['track']) events.add(safe);
    }
    final event = '${safe['event']}';
    final failure = event == 'error' ||
        event.contains('failed') ||
        event.contains('rejected') ||
        safe['failed'] == true ||
        safe['status'] is num && safe['status'] >= 400;
    if (failure) capture(event);
  }

  void capture(String reason) {
    final last = _recent.isEmpty ? <String, dynamic>{} : _recent.last;
    final events = _recent
        .where((row) =>
            row['session'] == last['session'] && row['track'] == last['track'])
        .toList()
        .reversed
        .take(16)
        .toList()
        .reversed
        .toList();
    _incidents.add({
      'id':
          '${DateTime.now().toUtc().microsecondsSinceEpoch.toRadixString(36)}-${_serial++}',
      'reason': safeText(reason),
      'at': last['at'],
      'track': last['track'],
      'session': last['session'],
      'events': events,
      'context': last,
      'device': _device(device)
    });
    while (_incidents.length > 4) {
      final api = _incidents
          .indexWhere((item) => '${item['reason']}'.startsWith('api-'));
      _incidents.removeAt(api < 0 ? 0 : api);
    }
  }

  String encodeSaved() {
    var encoded = jsonEncode({'version': 2, 'incidents': _incidents});
    while (utf8.encode(encoded).length > 65536) {
      if (_incidents.length > 1) {
        _incidents.removeAt(0);
      } else {
        final events = _incidents.first['events'] as List;
        if (events.length <= 1) break;
        events.removeAt(0);
      }
      encoded = jsonEncode({'version': 2, 'incidents': _incidents});
    }
    return encoded;
  }

  Map<String, dynamic> report({bool current = false}) {
    if (current || _incidents.isEmpty) capture('user-report');
    final incident = current
        ? _incidents.last
        : _incidents.reversed.firstWhere(
            (item) => !'${item['reason']}'.startsWith('api-'),
            orElse: () => _incidents.last);
    final events = incident['events'] as List;
    final last = incident['context'] as Map? ??
        (events.isEmpty ? <String, dynamic>{} : events.last as Map);
    final output = jsonDecode(jsonEncode({
      'version': 2,
      'reportId': incident['id'],
      'createdAt': DateTime.now().toUtc().toIso8601String(),
      'device': incident['device'],
      'summary': {
        'reason': incident['reason'],
        'occurredAt': incident['at'],
        'build': last['build'],
        'phase': last['phase'],
        'mediaCode': last['mediaCode'],
        'visibility': last['visibility'],
        'audioSession': last['audioSession'],
        'track': incident['track'],
        'session': incident['session']
      },
      'events': events
    })) as Map<String, dynamic>;
    final exported = output['events'] as List;
    while (
        utf8.encode(const JsonEncoder.withIndent('  ').convert(output)).length >
                32768 &&
            exported.length > 1) {
      exported.removeAt(0);
    }
    return output;
  }

  static String summary(Map<String, dynamic> report) {
    final info = report['summary'] as Map;
    return 'Report ${report['reportId']}\n${info['occurredAt'] ?? report['createdAt']}\n'
        'Issue: ${info['reason']} · State: ${info['phase'] ?? 'unknown'}\n'
        'Build: ${info['build'] ?? 'unknown'} · Media error: ${info['mediaCode'] ?? 'none'}\n'
        'Page: ${info['visibility'] ?? 'unknown'} · Audio session: ${info['audioSession'] ?? 'unavailable'}\n'
        'Device: ${(report['device'] as Map?)?['userAgent'] ?? 'not retained'}';
  }
}
