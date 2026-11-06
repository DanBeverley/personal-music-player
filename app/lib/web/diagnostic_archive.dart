import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'package:web/web.dart' as web;

/// Append-only, account-scoped diagnostics. The small UI report is independent
/// of this archive; export never applies the summary's byte/event limits.
class DiagnosticArchive {
  DiagnosticArchive(String scope)
      : _databaseName = 'neatie-diagnostics-v2:$scope',
        _legacyDatabaseName = 'neatie-diagnostics-v1:$scope' {
    try {
      final saved = jsonDecode(
          web.window.localStorage.getItem('$_databaseName:run') ?? '{}');
      _run = '${saved['run'] ?? _run}';
      _lastActivity = DateTime.tryParse('${saved['lastActivity']}');
      _lastTrack = '${saved['track'] ?? ''}';
      _build = '${saved['build'] ?? ''}';
      _exported = saved['exported'] == true;
    } catch (_) {}
  }
  final String _databaseName;
  final String _legacyDatabaseName;
  String _run = web.window.crypto.randomUUID();
  String _lastTrack = '', _build = '', _persistedRun = '';
  DateTime? _lastActivity, _persistedAt;
  bool _exported = false;
  final Map<String, String> _traceRuns = {};
  final List<String> _pending = [];
  Future<web.IDBDatabase>? _opening;
  Future<void> _tail = Future.value();
  Timer? _timer;
  int writeFailures = 0;
  String? _lastWriteFailure;

  Future<web.IDBDatabase> _open() => _opening ??= _openDatabase(_databaseName);

  Future<web.IDBDatabase> _openDatabase(String name, {bool indexed = true}) {
    final done = Completer<web.IDBDatabase>();
    final request = web.window.indexedDB.open(name, 1);
    request.onupgradeneeded = ((web.Event event) {
      final db = request.result as web.IDBDatabase;
      final store = db.createObjectStore(
          'events', web.IDBObjectStoreParameters(autoIncrement: true));
      if (indexed) store.createIndex('by_run', 'run'.toJS);
    }).toJS;
    request.onsuccess = ((web.Event _) {
      final db = request.result as web.IDBDatabase;
      if (done.isCompleted) {
        db.close();
        return;
      }
      db.onversionchange = ((web.Event _) => db.close()).toJS;
      done.complete(db);
    }).toJS;
    void fail(web.Event _) {
      if (!done.isCompleted)
        done.completeError(StateError('diagnostic_archive_unavailable'));
    }

    request.onerror = fail.toJS;
    request.onblocked = fail.toJS;
    return done.future.timeout(const Duration(seconds: 5));
  }

  void add(Map<String, dynamic> record) {
    final at = DateTime.tryParse('${record['at']}') ?? DateTime.now().toUtc();
    final build = '${record['build'] ?? ''}';
    final track = '${record['track'] ?? ''}';
    // Reload/export-only pages retain the preceding listening run. A new
    // activity after an export, build change or long idle starts another run.
    final activity =
        record['event'] == 'load' || record['event'] == 'search-rendered';
    if (activity &&
        ((_exported &&
                (record['resume'] != true ||
                    track != _lastTrack ||
                    record['event'] == 'search-rendered')) ||
            (_lastActivity != null &&
                at.difference(_lastActivity!) > const Duration(hours: 2)) ||
            (_build.isNotEmpty && build.isNotEmpty && build != _build))) {
      _run = web.window.crypto.randomUUID();
      _exported = false;
    }
    if (activity ||
        track.isNotEmpty &&
            !const {'ui-frame-summary', 'image-load-summary', 'image-warmup'}
                .contains(record['event'])) {
      _lastActivity = at;
      if (track.isNotEmpty) _lastTrack = track;
      if (build.isNotEmpty) _build = build;
    }
    final trace = '${record['traceId'] ?? ''}';
    if (trace.isNotEmpty && record['event'] == 'load') _traceRuns[trace] = _run;
    while (_traceRuns.length > 64) _traceRuns.remove(_traceRuns.keys.first);
    final delayedDelivery = record['event'] == 'delivery-snapshot' ||
        record['event'] == 'resource-delivery';
    _pending.add(jsonEncode({
      ...record,
      'diagnosticRun': delayedDelivery ? (_traceRuns[trace] ?? _run) : _run
    }));
    // Batch writes outside the native playback/event callback. Never rewrite
    // the entire archive on each progress event.
    _timer ??= Timer(const Duration(milliseconds: 200), () {
      _timer = null;
      unawaited(flush());
    });
  }

  Future<void> flush() {
    _timer?.cancel();
    _timer = null;
    final batch = List<String>.from(_pending);
    _pending.clear();
    try {
      if (_persistedRun != _run ||
          _persistedAt == null ||
          DateTime.now().difference(_persistedAt!) >
              const Duration(seconds: 30)) {
        web.window.localStorage.setItem(
            '$_databaseName:run',
            jsonEncode({
              'run': _run,
              'lastActivity': _lastActivity?.toIso8601String(),
              'track': _lastTrack,
              'build': _build,
              'exported': _exported,
            }));
        _persistedAt = DateTime.now();
        _persistedRun = _run;
      }
    } catch (_) {
      /* IndexedDB evidence remains available without run metadata. */
    }
    _tail = _tail.then((_) async {
      if (batch.isEmpty) return;
      try {
        final db = await _open();
        final done = Completer<void>();
        final tx = db.transaction('events'.toJS, 'readwrite');
        tx.oncomplete = ((web.Event _) {
          if (!done.isCompleted) done.complete();
        }).toJS;
        void fail(web.Event _) {
          if (!done.isCompleted)
            done.completeError(StateError(
                'diagnostic_archive_write_failed:${tx.error?.name ?? 'unknown'}'));
        }

        tx.onerror = fail.toJS;
        tx.onabort = fail.toJS;
        final store = tx.objectStore('events');
        for (final row in batch) {
          store.add({
            'run': (jsonDecode(row) as Map)['diagnosticRun'],
            'json': row
          }.jsify());
        }
        await done.future;
      } catch (error) {
        writeFailures++;
        final message = error.toString();
        _lastWriteFailure = const [
          'QuotaExceededError', 'AbortError', 'InvalidStateError',
          'UnknownError', 'diagnostic_archive_unavailable',
          'diagnostic_archive_write_failed'
        ].firstWhere(message.contains, orElse: () => 'other');
        _opening = null;
        // Keep failed writes available to this page's export. Do not silently
        // discard evidence if browser storage is unavailable or full.
        _pending.insertAll(0, batch);
      }
    });
    return _tail;
  }

  Future<Map<String, dynamic>> export({bool fullHistory = false}) async {
    await flush();
    final rows = <String>[];
    String? error;
    Future<void> readArchive(web.IDBDatabase db, {bool legacy = false}) async {
      final done = Completer<void>();
      final store =
          db.transaction('events'.toJS, 'readonly').objectStore('events');
      final request = fullHistory || legacy
          ? store.openCursor(null, 'prev')
          : store.index('by_run').openCursor(_run.toJS, 'prev');
      request.onsuccess = ((web.Event _) {
        final result = request.result;
        if (result == null) {
          done.complete();
          return;
        }
        final cursor = result as web.IDBCursorWithValue;
        final value = cursor.value.dartify();
        // Legacy v1 strings stay intact and remain in full-history exports.
        final row = value is String ? value : (value as Map)['json'] as String;
        rows.add(row);
        cursor.continue_();
      }).toJS;
      request.onerror = ((web.Event _) {
        done.completeError(StateError('diagnostic_archive_read_failed'));
      }).toJS;
      await done.future;
    }

    try {
      await readArchive(await _open());
      if (fullHistory) {
        // Keep the old archive schema untouched so an older frontend rollback
        // can still write/read its original database without a version error.
        final legacy = await _openDatabase(_legacyDatabaseName, indexed: false);
        try {
          await readArchive(legacy, legacy: true);
        } finally {
          legacy.close();
        }
      }
    } catch (_) {
      error = 'archive_read_unavailable';
    }
    final ordered = [
      ...rows.reversed,
      ..._pending.where((row) =>
          fullHistory || (jsonDecode(row) as Map)['diagnosticRun'] == _run)
    ];
    if (fullHistory)
      ordered.sort((a, b) => '${(jsonDecode(a) as Map)['at'] ?? ''}'
          .compareTo('${(jsonDecode(b) as Map)['at'] ?? ''}'));
    _exported = true;
    _persistedRun = ''; // Persist the boundary on the next flush/reload.
    await flush();
    return {
      'format': 'ndjson',
      'scope': fullHistory ? 'full-history' : 'listening-run',
      'run': fullHistory ? null : _run,
      'records': ordered.length,
      'writeFailures': writeFailures,
      if (_lastWriteFailure != null) 'lastWriteFailure': _lastWriteFailure,
      'writesRecovered': writeFailures > 0 && _pending.isEmpty,
      'pendingRecords': _pending.length,
      'complete': error == null && writeFailures == 0,
      if (error != null) 'error': error,
      'data': ordered.join('\n'),
    };
  }

  Future<void> close() async {
    await flush();
    try {
      (await _opening)?.close();
    } catch (_) {}
  }
}
