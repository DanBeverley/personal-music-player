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
    'bufferedRanges',
    'nativeDuration',
    'expectedDuration',
    'playbackRate',
    'failureStage',
    'failureType',
    'failureHttpStatus',
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
    'startupMs',
    'declaredMediaType',
    'stableResource',
    'sourceAssignments',
    'sourceReloads',
    'loadStartMs',
    'metadataMs',
    'canPlayMs',
    'playingEventMs',
    'resourceTimingCount',
    'resourceFirstByteMs',
    'resourceTotalMs',
    'resourceTransferBytes',
    'nextGapMs',
    'resumeMs',
    'sourceRetained',
    'fullyBuffered',
    'resultCount',
    'preview',
    'first_response_contract_ms',
    'direct_lookup_ms',
    'inputToResultsMs',
    'queryLength',
    'serverMs',
    'serverRequestId',
    'searchSurface',
    'searchOutcome',
    'snapshotHit',
    'retrievalMs',
    'rankingMs',
    'essential_targetMs',
    'artist_surface_completionMs',
    'snapshot_storeMs',
    'localIndexMs',
    'providerTimeoutCount',
    'cachedPrefixBytes',
    'chunkDeferred',
    'requestedTrack',
    'requestPurpose',
    'requestId',
    'requestBytes',
    'transportRoute',
    'online',
    'failureCode',
    'cacheTier',
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
  final List<Map<String, dynamic>> _performance = [];
  final Map<String, dynamic> device;
  int _serial = 0;

  // Actual player failures and remote commands outrank speculative API work.
  static int _priority(String reason) => reason.startsWith('api-') ||
          reason.startsWith('history-') ||
          const {
            'prepare-result',
            'search-response',
            'search-rendered',
            'suggest-response'
          }.contains(reason)
      ? 0
      : 2;

  static int _auxiliaryIndex(List events) => events.indexWhere((row) =>
      '${row['event']}'.startsWith('api-') ||
      '${row['event']}'.startsWith('history-') ||
      const {
        'prepare-result',
        'search-response',
        'search-rendered',
        'suggest-response'
      }.contains(row['event']));

  static int _budgetEventIndex(List events) {
    final auxiliary = _auxiliaryIndex(events);
    if (auxiliary >= 0) return auxiliary;
    // Keep remote commands and failure evidence before routine media events.
    final ordinary = events.indexWhere((row) =>
        row['event'] != 'media-action' &&
        row['failed'] != true &&
        !'${row['event']}'.contains('failed') &&
        !'${row['event']}'.contains('rejected') &&
        row['event'] != 'error');
    return ordinary >= 0 && ordinary < events.length - 1 ? ordinary : 0;
  }

  void _trimIncidents() {
    while (_incidents.length > 4) {
      var oldestLowest = 0;
      for (var i = 1; i < _incidents.length; i++) {
        if (_priority('${_incidents[i]['reason']}') <
            _priority('${_incidents[oldestLowest]['reason']}'))
          oldestLowest = i;
      }
      _incidents.removeAt(oldestLowest);
    }
  }

  PlaybackDiagnostics(this.device);

  static String? _performanceKind(Map row) {
    final event = '${row['event']}';
    if (event == 'suggest-response') return 'suggest';
    if (event == 'search-response' ||
        event == 'search-rendered' ||
        event == 'api-failure' &&
            row['failureCode'] != 'request_aborted' &&
            const {'/search', '/suggest'}.contains(row['path']))
      return 'search';
    if (event.startsWith('resume-')) return 'resume';
    if (event == 'playback-startup') return 'playback';
    if (event == 'prepare-result') return 'prepare';
    return null;
  }

  void _retainPerformance(Map<String, dynamic> row) {
    final kind = _performanceKind(row);
    if (kind == null) return;
    _performance.add({
      for (final key in const [
        'at',
        'event',
        'track',
        'session',
        'build',
        'ms',
        'startupMs',
        'declaredMediaType',
        'stableResource',
        'sourceAssignments',
        'sourceReloads',
        'loadStartMs',
        'metadataMs',
        'canPlayMs',
        'playingEventMs',
        'resourceTimingCount',
        'resourceFirstByteMs',
        'resourceTotalMs',
        'resourceTransferBytes',
        'nextGapMs',
        'resumeMs',
        'pauseMs',
        'ready',
        'bufferedEnd',
        'bufferedRanges',
        'nativeDuration',
        'expectedDuration',
        'playbackRate',
        'time',
        'visibility',
        'sourceRetained',
        'fullyBuffered',
        'reason',
        'kind',
        'requestId',
        'requestBytes',
        'transportRoute',
        'path',
        'resultCount',
        'preview',
        'inputToResultsMs',
        'queryLength',
        'first_response_contract_ms',
        'direct_lookup_ms',
        'cacheTier',
        'prepared',
        'fullyCached',
        'failureCode',
        'requestedTrack',
        'requestPurpose',
        'failureStage',
        'failureType',
        'failureHttpStatus',
        'serverMs',
        'serverRequestId',
        'searchSurface',
        'searchOutcome',
        'snapshotHit',
        'retrievalMs',
        'rankingMs',
        'essential_targetMs',
        'artist_surface_completionMs',
        'snapshot_storeMs',
        'localIndexMs',
        'providerTimeoutCount'
      ])
        if (row[key] != null) key: row[key],
    });
    final limit = kind == 'resume' || kind == 'search' ? 6 : 4;
    while (_performance.where((item) => _performanceKind(item) == kind).length >
        limit) {
      _performance.removeAt(
          _performance.indexWhere((item) => _performanceKind(item) == kind));
    }
  }

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
    _performance.clear();
    if (saved == null || utf8.encode(saved).length > 65536) return;
    try {
      final decoded = jsonDecode(saved);
      if (decoded is Map && decoded['performance'] is List) {
        for (final item
            in (decoded['performance'] as List).whereType<Map>().take(24)) {
          _retainPerformance(_safe(item));
        }
      }
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
    _retainPerformance(safe);
    _recent.add(safe);
    if (_recent.length > 80) _recent.removeAt(0);
    // Keep a short aftermath, without appending unrelated preparation or tracks.
    if (_incidents.isNotEmpty) {
      final latest = _incidents.last;
      final events = latest['events'] as List;
      if (latest['session'] == safe['session'] &&
          latest['track'] == safe['track']) {
        if (events.length >= 24 && _priority('${safe['event']}') > 0) {
          final auxiliary = _auxiliaryIndex(events);
          if (auxiliary >= 0) events.removeAt(auxiliary);
        }
        if (events.length < 24) events.add(safe);
      }
    }
    final event = '${safe['event']}';
    final failure = event == 'error' ||
        event == 'playback-interrupted' ||
        event == 'media-aborted' ||
        event == 'api-failure' && safe['failureCode'] != 'request_aborted' ||
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
    _trimIncidents();
  }

  String encodeSaved() {
    String encode() => jsonEncode(
        {'version': 2, 'incidents': _incidents, 'performance': _performance});
    var encoded = encode();
    while (utf8.encode(encoded).length > 65536) {
      if (_incidents.length > 1) {
        final auxiliary = _incidents
            .indexWhere((item) => _priority('${item['reason']}') == 0);
        _incidents.removeAt(auxiliary < 0 ? 0 : auxiliary);
      } else if (_incidents.isNotEmpty &&
          (_incidents.first['events'] as List).length > 8) {
        final events = _incidents.first['events'] as List;
        events.removeAt(_budgetEventIndex(events));
      } else if (_performance.isNotEmpty) {
        _performance.removeAt(0);
      } else {
        final events = _incidents.first['events'] as List;
        if (events.length <= 1) break;
        events.removeAt(_budgetEventIndex(events));
      }
      encoded = encode();
    }
    return encoded;
  }

  Map<String, dynamic> report({bool current = false}) {
    if (current || _incidents.isEmpty) capture('user-report');
    var incident = _incidents.last;
    if (!current) {
      for (final item in _incidents.reversed) {
        if (_priority('${item['reason']}') > _priority('${incident['reason']}'))
          incident = item;
      }
    }
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
        'session': incident['session'],
        'requestedTrack': last['requestedTrack'],
        'failureCode': last['failureCode'],
        'cacheTier': last['cacheTier'],
        'requestPurpose': last['requestPurpose']
      },
      'events': events,
      'performance': _performance,
      'incidents': [
        for (final item in _incidents)
          {
            'reportId': item['id'],
            'reason': item['reason'],
            'at': item['at'],
            'track': item['track'],
            'session': item['session'],
            'context': {
              for (final key in const [
                'mediaCode',
                'mediaMessage',
                'phase',
                'time',
                'nativeDuration',
                'expectedDuration',
                'bufferedRanges',
                'playbackRate',
                'paused',
                'ready',
                'network',
                'bufferedEnd',
                'visibility',
                'audioSession',
                'cause',
                'failureCode',
                'requestPurpose',
                'recoveryAttempts',
                'build'
              ])
                if ((item['context'] as Map?)?[key] != null)
                  key: (item['context'] as Map)[key],
            },
          }
      ],
    })) as Map<String, dynamic>;
    final exported = output['events'] as List;
    while (utf8
                .encode(const JsonEncoder.withIndent('  ').convert(output))
                .length >
            32768 &&
        (exported.length > 1 || (output['performance'] as List).isNotEmpty)) {
      if (exported.length > 8) {
        exported.removeAt(_budgetEventIndex(exported));
        continue;
      }
      if ((output['performance'] as List).isNotEmpty) {
        (output['performance'] as List).removeAt(0);
        continue;
      }
      exported.removeAt(_budgetEventIndex(exported));
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
