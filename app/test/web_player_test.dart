import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:auralis_engine/web/music_api.dart';
import 'package:auralis_engine/web/playback_port.dart';
import 'package:auralis_engine/web/web_player.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

const first = <String, dynamic>{'id': 'recording:first', 'title': 'First'};
const second = <String, dynamic>{'id': 'recording:second', 'title': 'Second'};

class MemoryStore implements SessionStore {
  String? value;
  @override
  String? read() => value;
  @override
  void write(String value) => this.value = value;
}

class CheckpointStore extends MemoryStore implements CheckpointSessionStore {
  String? checkpoint;
  int fullWrites = 0, positionWrites = 0;
  @override
  void write(String value) {
    fullWrites++;
    super.write(value);
  }

  @override
  String? readCheckpoint() => checkpoint;
  @override
  bool writeCheckpoint(String value) {
    positionWrites++;
    checkpoint = value;
    return true;
  }
}

class ReloadingAudio extends FakeAudio {
  bool reloadOnPlay = false;
  @override
  Future<void> play() async {
    if (reloadOnPlay) {
      reloadOnPlay = false;
      position = 0;
      scheduleMicrotask(() {
        changes.add('source-reloading');
        changes.add('source-reloaded');
        changes.add('loadedmetadata');
      });
    }
    await super.play();
  }
}

class FakeAudio extends PlaybackPort {
  final changes = StreamController<String>.broadcast(sync: true);
  @override
  Stream<String> get events => changes.stream;
  @override
  double position = 0;
  @override
  double duration = 100;
  @override
  bool paused = true;
  PlaybackPhase? reportedPhase;
  @override
  PlaybackPhase get phase =>
      reportedPhase ?? (paused ? PlaybackPhase.paused : PlaybackPhase.playing);
  @override
  bool needsNewSource = false;
  @override
  String? sourceFailureCode;
  @override
  bool fullyBuffered = false;
  final records = <String>[];
  final recordDetails = <Map<String, dynamic>>[];
  @override
  void record(String event, [Map<String, dynamic> extra = const {}]) {
    records.add(event);
    recordDetails.add({'event': event, ...extra});
  }

  @override
  String? loadedTrackId;
  @override
  bool sourceMatches = true;
  @override
  void syncMetadata(Map<String, dynamic> track) {}
  Uri? uri;
  String? declaredMime;
  int plays = 0;
  bool blocked = false;
  PlaybackStartException? startFailure;
  bool metadataAvailable = true;
  int reloads = 0;
  @override
  void reloadSource() {
    reloads++;
    position = 0;
    scheduleMicrotask(() => changes.add('loadedmetadata'));
  }

  Completer<void>? delayedPlay;
  @override
  void load(Uri value, Map<String, dynamic> track, {String? mimeType}) {
    uri = value;
    declaredMime = mimeType;
    loadedTrackId = track['id'] as String?;
    position = 0;
    if (metadataAvailable)
      scheduleMicrotask(() => changes.add('loadedmetadata'));
  }

  @override
  Future<void> play() async {
    if (startFailure != null) throw startFailure!;
    if (blocked) throw StateError('NotAllowed');
    plays++;
    paused = false;
    await delayedPlay?.future;
    if (!changes.isClosed) changes.add('playback-state');
  }

  @override
  void pause() {
    paused = true;
  }

  @override
  void seek(double seconds) {
    position = seconds;
  }

  @override
  void dispose() {
    changes.close();
  }
}

class FakeApi extends MusicApi {
  FakeApi() : super(Uri.parse('https://music.test'));
  Completer<Uri>? delayedResolve;
  final prepared = <String>[];
  final expiry = <String, DateTime>{};
  @override
  DateTime? preparedUntil(String id) => expiry[id];
  final fullDownloads = <String>[];
  final queuePlans = <List<String>>[];
  final queuePlanRevisions = <int>[];
  final cancelledQueueRevisions = <int>[];
  int queueStatusPolls = 0;
  String queueStatus = 'running';
  @override
  Future<Map<String, dynamic>> queueCacheStatus(
      String token, int revision) async {
    queueStatusPolls++;
    return {
      'queue_cache': {'status': queueStatus}
    };
  }

  @override
  Future<Map<String, dynamic>> cacheQueue(
      String token, int revision, List<Map<String, dynamic>> tracks,
      {bool cancel = false}) async {
    if (cancel) {
      cancelledQueueRevisions.add(revision);
    } else {
      queuePlans.add(tracks.map((t) => t['id'] as String).toList());
      queuePlanRevisions.add(revision);
    }
    return {
      'status': 'success',
      'queue_cache': {'status': 'running'}
    };
  }

  final delayedTracks = <String, Completer<Uri>>{};
  Completer<Uri>? delayedFullDownload;
  Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>?
      delayedRelated;
  String? relatedSession;
  List<String> relatedRecent = [];
  @override
  Future<Uri> prepare(Map<String, dynamic> track,
      {bool activeQueue = false, bool cacheFull = false}) {
    final id = track['id'] as String;
    if (cacheFull) {
      fullDownloads.add(id);
      return delayedFullDownload?.future ?? Future.value(streamUri(track));
    }
    prepared.add(id);
    return delayedTracks[id]?.future ??
        delayedResolve?.future ??
        Future.value(base.resolve('/playback/stream/${track['id']}'));
  }

  @override
  Future<({List<Map<String, dynamic>> tracks, bool hasMore})> related(
      Map<String, dynamic> seed, List<Map<String, dynamic>> queue,
      {String discoverySession = '', List<String> recentIds = const []}) {
    relatedSession = discoverySession;
    relatedRecent = List.of(recentIds);
    return delayedRelated?.future ??
        Future.value((tracks: [second], hasMore: false));
  }
}

class StagedAudio extends FakeAudio {
  @override
  bool get supportsStagedHandoff => true;
  Completer<void>? staged;
  Map<String, dynamic>? candidate;
  bool rejectCommit = false;
  Completer<void>? delayedCommit;
  int commits = 0, cancellations = 0;
  @override
  Future<void> stage(Uri uri, Map<String, dynamic> track, {String? mimeType}) {
    candidate = track;
    staged = Completer<void>();
    return staged!.future;
  }

  @override
  Future<void> commitStaged() async {
    final selected = candidate;
    await delayedCommit?.future;
    if (selected == null || !identical(candidate, selected))
      throw const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'stage-cancelled');
    if (rejectCommit)
      throw const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'permission');
    commits++;
    load(Uri.parse('https://music.test/candidate'), candidate!);
    candidate = null;
    await super.play();
  }

  @override
  void cancelStaged() {
    if (candidate == null) return;
    cancellations++;
    candidate = null;
    if (!staged!.isCompleted)
      staged!.completeError(const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'stage-cancelled'));
  }
}

void main() {
  test('artist queues start immediately and use the full provider song order',
      () async {
    final audio = FakeAudio();
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response('{}', 200)));
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(audio.changes.close);
    addTearDown(player.dispose);
    addTearDown(api.client.close);
    final songs = [
      for (var index = 1; index <= 7; index++)
        {'id': 'recording:song-$index', 'title': 'Song $index'},
    ];
    final full = Completer<List<Map<String, dynamic>>>();
    await player.startArtistQueue(songs[4], songs.take(5).toList(), 'Artist',
        loadSongs: () => full.future);
    expect(audio.plays, 1);
    expect(player.current?['title'], 'Song 5');
    full.complete(songs);
    await Future<void>.delayed(Duration.zero);
    expect(player.queue.map((item) => item['title']),
        ['Song 5', 'Song 6', 'Song 7', 'Song 1', 'Song 2', 'Song 3', 'Song 4']);

    final search = Completer<List<Map<String, dynamic>>>();
    await player.startArtistQueue(songs[4], [], 'Artist',
        wrap: false, loadSongs: () => search.future);
    search.complete(songs);
    await Future<void>.delayed(Duration.zero);
    expect(player.queue.map((item) => item['title']),
        ['Song 5', 'Song 1', 'Song 2', 'Song 3', 'Song 4', 'Song 6', 'Song 7']);
  });

  group('selected-track handoff', () {
    const third = {'id': 'recording:third', 'title': 'Third'};
    late StagedAudio audio;
    late WebPlayer player;
    late MemoryStore store;
    setUp(() async {
      audio = StagedAudio();
      store = MemoryStore();
      player = WebPlayer(FakeApi(), audio, store);
      await player.startQueue([first, second, third], 0, 'Original');
      await Future<void>.delayed(Duration.zero);
      audio.position = 24;
    });
    tearDown(() => player.dispose());

    test('A remains current and advancing while B prepares, then B takes over',
        () async {
      final next = player.playIndex(1);
      expect(audio.paused, isFalse);
      expect(player.current!['id'], first['id']);
      expect(player.preparingSelection, isTrue);
      expect(player.busy, isFalse);
      expect(player.canPause, isTrue);
      audio.position = 26;
      audio.changes.add('timeupdate');
      expect(player.position, 26);
      expect(jsonDecode(store.value!)['current'], first['id']);
      audio.staged!.complete();
      await next;
      expect(player.current!['id'], second['id']);
      expect(audio.loadedTrackId, second['id']);
      expect(audio.commits, 1);
      expect(audio.paused, isFalse);
      expect(player.selectionError, isNull);
    });

    test('failed B retains A and its saved position', () async {
      final next = player.playIndex(1);
      audio.staged!.completeError(const PlaybackStartException(
          PlaybackStartFailure.failed, 'stage-media-error'));
      await next;
      expect(player.current!['id'], first['id']);
      expect(player.position, 24);
      expect(audio.paused, isFalse);
      expect(player.playing, isTrue);
      expect(player.selectionError, contains('could not load'));
      expect(audio.commits, 0);
    });

    test('denied B start does not replace A or its queue', () async {
      final next = player.start(second);
      audio.rejectCommit = true;
      audio.staged!.complete();
      await next;
      expect(player.current!['id'], first['id']);
      expect(player.queue, hasLength(3));
      expect(audio.paused, isFalse);
      expect(player.selectionError, contains('browser'));
    });

    test('Pause cancels B without restarting either track', () async {
      final next = player.playIndex(1);
      player.pause();
      await next;
      expect(player.current!['id'], first['id']);
      expect(audio.paused, isTrue);
      expect(audio.commits, 0);
      expect(player.preparingSelection, isFalse);
    });

    test('rapid B then C selects only C', () async {
      final b = player.playIndex(1);
      final c = player.playIndex(2);
      audio.staged!.complete();
      await Future.wait([b, c]);
      expect(player.current!['id'], third['id']);
      expect(audio.cancellations, 1);
      expect(audio.commits, 1);
    });

    test('reselecting A cancels B', () async {
      final b = player.playIndex(1);
      await player.playIndex(0);
      await b;
      expect(audio.commits, 0);
      expect(player.current!['id'], first['id']);
      expect(audio.paused, isFalse);
    });

    test('B follows its identity after queue reorder', () async {
      final b = player.playIndex(1);
      expect(player.moveQueueItem(1, 2), isTrue);
      audio.staged!.complete();
      await b;
      expect(player.index, 2);
      expect(player.current!['id'], second['id']);
    });

    test('removed pending B cannot start', () async {
      final b = player.playIndex(1);
      expect(player.removeQueueItem(1), isTrue);
      expect(audio.staged!.isCompleted, isTrue);
      await b;
      expect(audio.commits, 0);
      expect(player.current!['id'], first['id']);
      expect(player.preparingSelection, isFalse);
    });

    test('removing B during native commit cancels without an invalid index',
        () async {
      audio.delayedCommit = Completer<void>();
      final b = player.playIndex(1);
      audio.staged!.complete();
      await Future<void>.delayed(Duration.zero);
      player.removeQueueItem(1);
      audio.delayedCommit!.complete();
      await b;
      expect(player.index, 0);
      expect(player.current!['id'], first['id']);
      expect(audio.commits, 0);
    });

    test('selecting A in a new collection during commit retains its position',
        () async {
      audio.delayedCommit = Completer<void>();
      final b = player.playIndex(1);
      audio.staged!.complete();
      await Future<void>.delayed(Duration.zero);
      audio.pause(); // Native handoff has reached the brief commit boundary.
      await player.startQueue([first, third], 0, 'Retained A');
      audio.delayedCommit!.complete();
      await b;
      expect(player.current!['id'], first['id']);
      expect(player.title, 'Retained A');
      expect(audio.position, 24);
      expect(audio.paused, isFalse);
      expect(audio.commits, 0);
    });

    test('A error is reconciled if B also fails', () async {
      final b = player.playIndex(1);
      audio.reportedPhase = PlaybackPhase.failed;
      audio.changes.add('error');
      audio.staged!.completeError(const PlaybackStartException(
          PlaybackStartFailure.failed, 'stage-media-error'));
      await b;
      expect(player.current!['id'], first['id']);
      expect(player.error, contains('audio stream failed'));
      expect(player.playing, isFalse);
    });

    test('A ending followed by B failing reports the ended state', () async {
      final b = player.playIndex(1);
      audio.reportedPhase = PlaybackPhase.ended;
      audio.changes.add('ended');
      audio.staged!.completeError(const PlaybackStartException(
          PlaybackStartFailure.failed, 'stage-media-error'));
      await b;
      expect(player.current!['id'], first['id']);
      expect(player.selectionError, contains('previous song has ended'));
      expect(audio.commits, 0);
    });

    test('A ending does not auto-next over pending C', () async {
      final c = player.playIndex(2);
      audio.changes.add('ended');
      expect(audio.candidate!['id'], third['id']);
      audio.staged!.complete();
      await c;
      expect(player.current!['id'], third['id']);
    });

    test('remote Play after A ends does not abandon pending B', () async {
      final b = player.playIndex(1);
      audio.reportedPhase = PlaybackPhase.ended;
      audio.changes.add('ended');
      expect(player.busy, isTrue);
      expect(player.canPause, isTrue);
      await player.play();
      expect(player.preparingSelection, isTrue);
      expect(audio.candidate!['id'], second['id']);
      audio.reportedPhase = null;
      audio.staged!.complete();
      await b;
      expect(player.current!['id'], second['id']);
      expect(audio.commits, 1);
    });

    test('new queue becomes authoritative only after B can start', () async {
      final b = player.startQueue([second, third], 0, 'New list');
      expect(player.title, 'Original');
      expect(player.queue.first['id'], first['id']);
      audio.staged!.complete();
      await b;
      expect(player.title, 'New list');
      expect(player.queue.first['id'], second['id']);
      expect(player.current!['id'], second['id']);
    });

    test('account switch cancels B and prevents a late commit', () async {
      final b = player.playIndex(1);
      player.changeAccount(MemoryStore());
      await b;
      expect(audio.commits, 0);
      expect(player.current, isNull);
      expect(audio.paused, isTrue);
    });
  });
  test(
      'progress checkpoints restore without rewriting queue or rebuilding identity',
      () async {
    final store = CheckpointStore(), audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, store);
    addTearDown(player.dispose);
    await player.startQueue([first, second], 0, 'Fixture');
    await Future<void>.delayed(Duration.zero);
    player.save();
    final writes = store.fullWrites;
    var identity = 0, ticks = 0;
    player.addListener(() => identity++);
    player.progress.addListener(() => ticks++);
    for (var i = 1; i <= 10; i++) {
      audio.position = i.toDouble();
      audio.changes.add('timeupdate');
    }
    expect(store.fullWrites, writes);
    expect(store.positionWrites, 10);
    expect(identity, 0);
    expect(ticks, 10);
    final restored = WebPlayer(FakeApi(), FakeAudio(), store);
    expect(restored.position, 10);
    expect(restored.queue.length, 2);
    restored.dispose();
    player.seek(0);
    final atZero = WebPlayer(FakeApi(), FakeAudio(), store);
    expect(
        atZero.position, 0); // Old position checkpoint is fenced by full save.
    atZero.dispose();
  });

  test('foreign and malformed checkpoints do not replace a saved position',
      () async {
    final store = CheckpointStore(), audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, store);
    await player.startQueue([first], 0, 'Fixture');
    player.seek(23);
    for (final value in [
      '{bad',
      jsonEncode({'stamp': 'other-account', 'position': 80})
    ]) {
      store.checkpoint = value;
      final restored = WebPlayer(FakeApi(), FakeAudio(), store);
      expect(restored.position, 23);
      restored.dispose();
    }
    player.dispose();
  });
  test(
      'unknown typed-source failure retries the same resource once and stays bounded',
      () async {
    final audio = FakeAudio()..metadataAvailable = false;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Fixture');
    final original = audio.uri;
    audio.changes.add('media-source-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.reloads, 1);
    expect(audio.uri, original);
    expect(audio.position, 0);
    expect(player.diagnostics['recoveryAttempts'], 1);
    audio.changes.add('media-source-error');
    await Future<void>.delayed(Duration.zero);
    expect(player.diagnostics['recoveryAttempts'], 2);
    audio.changes.add('media-source-error');
    await Future<void>.delayed(Duration.zero);
    expect(player.diagnostics['loadedTrack'], isNull);
    expect(player.startupInterrupted, isTrue);
  });

  test(
      'typed-source recovery preserves position and explicit pause stops retry',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Fixture');
    await Future<void>.delayed(Duration.zero);
    audio.position = 19.2;
    audio.changes.add('timeupdate');
    audio.changes.add('media-source-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.position, closeTo(19.2, .01));
    player.pause();
    final plays = audio.plays;
    audio.changes.add('media-source-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, plays);
  });

  test(
      'locked transport failure survives a native reload that resets time to zero',
      () async {
    final audio = ReloadingAudio(), store = MemoryStore();
    final player = WebPlayer(FakeApi(), audio, store);
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 21.56;
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error');
    audio.position =
        0; // Native failed-resource reset before foreground activation.
    expect(player.position, closeTo(21.56, .001));
    player.save();
    expect(jsonDecode(store.value!)['position'], closeTo(21.56, .001));
    audio.reloadOnPlay = true;
    player.lifecycle('visibilitychange', visible: true);
    await Future<void>.delayed(Duration.zero);
    expect(audio.position, closeTo(21.56, .001));
    expect(player.current, first);
  });
  test(
      'position is restored before renewed-source play even before metadata arrives',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 21.56;
    audio.metadataAvailable = false;
    audio.needsNewSource = true;
    await player.play();
    expect(audio.position, closeTo(21.56, .001));
    expect(player.position, closeTo(21.56, .001));
    audio.changes.add('loadedmetadata');
    expect(audio.position, closeTo(21.56, .001));
    await player.start(second);
    expect(player.position, 0);
  });
  test('explicit seek to zero replaces the retained recovery position',
      () async {
    final audio = ReloadingAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 21.56;
    player.seek(0);
    player.pause();
    audio.reloadOnPlay = true;
    await player.play();
    expect(audio.position, 0);
  });
  test('a seek during reload replaces the queued restoration target', () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 21.56;
    audio.changes.add('source-reloading');
    audio.position = 0;
    audio.changes.add('source-reloaded');
    expect(audio.position, closeTo(21.56, .001));
    player.seek(0);
    expect(audio.position, 0);
    audio.changes.add('loadedmetadata');
    expect(player.position, 0);
  });
  test(
      'immutable format survives a long hidden song while refresh is unavailable',
      () async {
    var now = DateTime.utc(2026, 10, 16);
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response('{"prepared":{}}', 200)))
      ..clock = () => now;
    addTearDown(api.client.close);
    const path =
        'https://music.test/playback/stream/generated01?cache_variant=seek-v1&cache_version=v1-0123456789abcdef0123456789abcdef';
    api.restorePreparedMedia([
      {
        'id': 'generated01',
        'uri': path,
        'mime': 'audio/webm',
        'until': now.add(const Duration(minutes: 2)).millisecondsSinceEpoch
      }
    ]);
    now = now.add(const Duration(minutes: 10));
    expect(api.preparedMedia('generated01')?.mimeType, 'audio/webm');
    expect(api.preparedMedia('generated01')?.uri.toString(), path);
    expect(api.needsRepresentationRefresh('generated01'), isTrue);
    api.forgetPreparedMedia('generated01');
    expect(api.preparedMedia('generated01'), isNull);
  });
  test('old cached hints discover Opus without clearing usable AAC', () async {
    final now = DateTime.now();
    const oldPath =
        '/playback/stream/generated01?cache_variant=seek-v2&cache_version=v1-0123456789abcdef0123456789abcdef';
    const newPath =
        '/playback/stream/generated01?cache_variant=opus-v1&cache_version=v1-abcdef0123456789abcdef0123456789';
    final response = Completer<http.Response>();
    var requests = 0;
    final api =
        MusicApi(Uri.parse('https://music.test'), client: MockClient((request) {
      requests++;
      expect(jsonDecode(request.body)['cached_only'], true);
      return response.future;
    }));
    api.catalogMediaHints = true;
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': 'https://music.test$oldPath',
        'mime': 'audio/mp4',
        'until': now.add(const Duration(hours: 12)).millisecondsSinceEpoch
      }
    ]);
    api.preloadCachedMedia([first]);
    expect(api.preparedMedia(first['id']!)!.uri.path,
        '/playback/stream/generated01');
    expect(api.preparedMedia(first['id']!)!.mimeType, 'audio/mp4');
    await Future<void>.delayed(Duration.zero);
    expect(requests, 1);
    response.complete(http.Response(
        jsonEncode({
          'prepared': {
            first['id']: {
              'playback_path': newPath,
              'mime_type': 'audio/webm',
              'cache_tier': 'r2_full',
              'expires_at':
                  now.add(const Duration(hours: 1)).millisecondsSinceEpoch /
                      1000
            }
          }
        }),
        200));
    await Future<void>.delayed(Duration.zero);
    expect(api.preparedMedia(first['id']!)!.mimeType, 'audio/webm');
    expect(
        api.preparedMedia(first['id']!)!.uri.queryParameters['cache_variant'],
        'opus-v1');
    api.preloadCachedMedia([first]);
    await Future<void>.delayed(Duration.zero);
    expect(requests, 1);
  });

  test('cached AAC schedules upgrade after progression without reloading audio',
      () async {
    final api = FakeApi();
    final old = Uri.parse(
        'https://music.test/playback/stream/generated01?cache_variant=seek-v2&cache_version=v1-0123456789abcdef0123456789abcdef');
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': old.toString(),
        'mime': 'audio/mp4',
        'until':
            DateTime.now().add(const Duration(hours: 12)).millisecondsSinceEpoch
      }
    ]);
    final audio = FakeAudio()..reportedPhase = PlaybackPhase.starting;
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    expect(api.prepared, isEmpty);
    audio.position = .2;
    audio.reportedPhase = PlaybackPhase.playing;
    audio.changes.add('playback-state');
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, contains(first['id']));
    expect(audio.uri, old);
    expect(audio.plays, 1);
    expect(audio.reloads, 0);
    expect(audio.position, .2);
  });

  const cachedPath =
      '/playback/stream/generated01?cache_variant=seek-v2&cache_version=v1-0123456789abcdef0123456789abcdef';
  Map<String, dynamic> cachedDescriptor(
          {String path = cachedPath, String mime = 'audio/webm'}) =>
      {
        'playback_path': path,
        'mime_type': mime,
        'cache_tier': 'r2_full',
        'expires_at': DateTime.now()
                .add(const Duration(hours: 1))
                .millisecondsSinceEpoch /
            1000,
      };
  Future<void> settleMetadata() async {
    for (var i = 0; i < 12; i++) await Future<void>.delayed(Duration.zero);
  }

  test(
      'catalog result does not wait for metadata and first selection beyond five has a verified type',
      () async {
    final metadata = Completer<http.Response>();
    final tracks = List.generate(
        12, (i) => {'id': 'recording:visible$i', 'title': 'Visible $i'});
    final requests = <Map>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      if (request.url.path == '/search')
        return http.Response(jsonEncode({'tracks': tracks}), 200);
      if (request.url.path == '/prepare_session') {
        final body = jsonDecode(request.body) as Map;
        requests.add(body);
        if (body['cached_only'] == true) return metadata.future;
      }
      return http.Response('{"tracks":[],"has_more":false}', 200);
    }))
      ..catalogMediaHints = true;
    addTearDown(api.client.close);
    final result = await api.post('/search', {'query': 'generated'});
    expect(result['tracks'], hasLength(12));
    expect(metadata.isCompleted, isFalse);
    await settleMetadata();
    expect(requests.single['track_keys'], hasLength(12));
    api.preloadCachedMedia(tracks);
    expect(requests, hasLength(1));
    metadata.complete(http.Response(
        jsonEncode({
          'prepared': {
            for (final track in tracks) track['id']: cachedDescriptor(),
          }
        }),
        200));
    await settleMetadata();
    final audio = FakeAudio();
    final selected = WebPlayer(api, audio, MemoryStore());
    addTearDown(selected.dispose);
    await selected.start(tracks[9]);
    expect(audio.declaredMime, 'audio/webm');
    expect(audio.uri!.queryParameters['cache_variant'], 'seek-v2');
    expect(audio.uri!.queryParameters.containsKey('playback_session'), isFalse);
    expect(requests.every((body) => body['cached_only'] == true), isTrue);
  });

  test(
      'pending metadata never delays native Play or replaces an active resource',
      () async {
    final metadata = Completer<http.Response>();
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      if (request.url.path == '/prepare_session' &&
          jsonDecode(request.body)['cached_only'] == true)
        return metadata.future;
      return http.Response('{"tracks":[],"has_more":false}', 200);
    }))
      ..catalogMediaHints = true;
    addTearDown(api.client.close);
    api.preloadCachedMedia([first]);
    final audio = FakeAudio();
    final selected = WebPlayer(api, audio, MemoryStore());
    addTearDown(selected.dispose);
    final start = selected.start(first);
    expect(audio.plays, 1);
    final active = audio.uri;
    expect(audio.declaredMime, isNull);
    await start;
    metadata.complete(http.Response(
        jsonEncode({
          'prepared': {first['id']: cachedDescriptor()}
        }),
        200));
    await settleMetadata();
    expect(api.preparedMedia(first['id'] as String), isNotNull);
    expect(audio.uri, active);
    expect(audio.plays, 1);
    expect(audio.reloads, 0);
  });

  test('late cache metadata stays fenced across account changes', () async {
    final metadata = Completer<http.Response>();
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) => metadata.future))
      ..catalogMediaHints = true;
    addTearDown(api.client.close);
    api.preloadCachedMedia([first]);
    api.userScopeId = 'different-account';
    metadata.complete(http.Response(
        jsonEncode({
          'prepared': {first['id']: cachedDescriptor()}
        }),
        200));
    await settleMetadata();
    expect(api.preparedMedia(first['id'] as String), isNull);
  });

  test(
      'Home, artist and cloud-history responses prepare cached metadata independently',
      () async {
    final payloads = {
      '/recommend': {
        'rows': [
          {
            'item_type': 'track',
            'items': [first]
          },
          {
            'item_type': 'mix',
            'items': [
              {
                'tracks': [second]
              }
            ]
          }
        ]
      },
      '/artist/generated': {
        'top_songs': [first, second]
      },
      '/history_seed': {
        'tracks': [first],
        'top_tracks': [second]
      },
    };
    for (final entry in payloads.entries) {
      final api = MusicApi(Uri.parse('https://music.test'),
          client: MockClient((request) async {
        if (request.url.path == '/prepare_session')
          return http.Response(
              jsonEncode({
                'prepared': {
                  for (final id in jsonDecode(request.body)['track_keys'])
                    id: cachedDescriptor(),
                }
              }),
              200);
        return http.Response(jsonEncode(entry.value), 200);
      }))
        ..catalogMediaHints = true;
      await api.post(entry.key, {});
      await settleMetadata();
      expect(api.preparedMedia(first['id'] as String), isNotNull,
          reason: entry.key);
      expect(api.preparedMedia(second['id'] as String), isNotNull,
          reason: entry.key);
      api.client.close();
    }
  });

  test('cache metadata rejects unsafe, guessed and old AAC descriptors',
      () async {
    final variants = [
      cachedDescriptor(path: 'https://other.test$cachedPath'),
      cachedDescriptor(mime: 'video/mp4'),
      cachedDescriptor(
          path: cachedPath.replaceFirst('seek-v2', 'seek-v1'),
          mime: 'audio/mp4'),
      cachedDescriptor(path: '$cachedPath&token=private'),
      {...cachedDescriptor(), 'cache_tier': 'prefix'},
    ];
    for (final descriptor in variants) {
      final api = MusicApi(Uri.parse('https://music.test'),
          client: MockClient((_) async => http.Response(
              jsonEncode({
                'prepared': {first['id']: descriptor}
              }),
              200)))
        ..catalogMediaHints = true;
      api.preloadCachedMedia([first]);
      await settleMetadata();
      expect(api.preparedMedia(first['id'] as String), isNull);
      api.client.close();
    }
  });

  test(
      'queue reload restores bounded same-origin media hints and scope clears them',
      () async {
    const id = 'recording:first';
    final uri = Uri.parse(
        'https://music.test/playback/stream/source?cache_variant=seek-v2&cache_version=v1-0123456789abcdef0123456789abcdef');
    final entry = {
      'id': id,
      'uri': uri.toString(),
      'mime': 'audio/webm',
      'until':
          DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch
    };
    MusicApi makeApi() => MusicApi(Uri.parse('https://music.test'),
        client: MockClient(
            (_) async => http.Response('{"tracks":[],"has_more":false}', 200)));
    final api = makeApi();
    api.restorePreparedMedia([
      entry,
      {
        ...entry,
        'id': 'bad-origin',
        'uri': uri.replace(host: 'outside.test').toString()
      },
      {...entry, 'id': 'bad-query', 'uri': '$uri&token=private'},
      {
        ...entry,
        'id': 'expired',
        'until': DateTime.now()
            .subtract(const Duration(seconds: 1))
            .millisecondsSinceEpoch
      }
    ]);
    expect(api.savedPreparedMedia().length, 1);
    final store = MemoryStore(), audio = FakeAudio();
    final player = WebPlayer(api, audio, store);
    await player.start(first);
    player.save();
    player.dispose();
    final next = makeApi(),
        nextAudio = FakeAudio(),
        restored = WebPlayer(next, nextAudio, store);
    addTearDown(restored.dispose);
    expect(next.preparedMedia(id)!.uri, uri);
    await restored.play();
    expect(nextAudio.uri, uri);
    expect(nextAudio.declaredMime, 'audio/webm');
    next.userScopeId = 'another-account';
    expect(next.preparedMedia(id), isNull);
  });
  test(
      'completed media reuses versioned URI and format; retry and scope stay isolated',
      () async {
    const version = 'v1-0123456789abcdef0123456789abcdef';
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      if (request.url.path == '/prepare_session') {
        final keys =
            (jsonDecode(request.body) as Map)['track_keys'] as List? ?? [];
        return http.Response(
            jsonEncode({
              'prepared': {
                for (final id in keys)
                  id: {
                    'playback_path':
                        '/playback/stream/source?cache_variant=seek-v1&cache_version=$version',
                    'mime_type': 'audio/webm',
                    'cache_tier': 'r2_full',
                    'expires_at': DateTime.now()
                            .add(const Duration(hours: 1))
                            .millisecondsSinceEpoch /
                        1000,
                  }
              }
            }),
            200);
      }
      return http.Response('{"tracks":[],"has_more":false}', 200);
    }));
    final audio = FakeAudio();
    final realPlayer = WebPlayer(api, audio, MemoryStore());
    addTearDown(realPlayer.dispose);
    await api.prepare(first);
    await realPlayer.start(first);
    final stable = audio.uri;
    expect(stable!.queryParameters.containsKey('playback_session'), isFalse);
    expect(audio.declaredMime, 'audio/webm');
    await realPlayer.start(second);
    await realPlayer.start(first);
    expect(audio.uri, stable);
    audio.startFailure =
        const PlaybackStartException(PlaybackStartFailure.failed, 'fixture');
    await realPlayer.start(second);
    audio.startFailure = null;
    await realPlayer.start(first);
    realPlayer.pause();
    audio.startFailure =
        const PlaybackStartException(PlaybackStartFailure.failed, 'fixture');
    await realPlayer.playIndex(0, explicitRetry: true);
    expect(audio.uri!.queryParameters['retry_source'], isNull);
    expect(audio.uri!.queryParameters['cache_version'], version);
    expect(audio.uri!.queryParameters['playback_session'], isNotEmpty);
    expect(audio.declaredMime, 'audio/webm');
    api.userScopeId = 'other-account';
    expect(api.preparedMedia(first['id'] as String), isNull);
  });

  test('prepared upstream keeps its exact source and type only in memory',
      () async {
    final uri =
        '/playback/stream/source?playback_session=${'a' * 32}&stream_version=${'b' * 64}';
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async => http.Response(
            request.url.path == '/prepare_session'
                ? jsonEncode({
                    'prepared': {
                      first['id']: {
                        'playback_path': uri,
                        'mime_type': 'audio/webm',
                        'cache_tier': 'prefix',
                        'expires_at': DateTime.now()
                                .add(const Duration(minutes: 10))
                                .millisecondsSinceEpoch /
                            1000
                      }
                    }
                  })
                : '{"tracks":[],"has_more":false}',
            200)));
    final audio = FakeAudio();
    final realPlayer = WebPlayer(api, audio, MemoryStore());
    addTearDown(realPlayer.dispose);
    await api.prepare(first);
    expect(api.savedPreparedMedia(), isEmpty);
    await realPlayer.start(first);
    expect(audio.uri.toString(), 'https://music.test$uri');
    expect(audio.declaredMime, 'audio/webm');
    api.clock = () => DateTime.now().add(const Duration(minutes: 11));
    expect(api.preparedMedia(first['id'] as String), isNull);
  });

  test('prefix-only descriptor cannot publish a reusable media URL', () async {
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response(
            jsonEncode({
              'prepared': {
                first['id']: {
                  'playback_path':
                      '/playback/stream/source?cache_variant=seek-v1&cache_version=v1-0123456789abcdef0123456789abcdef',
                  'mime_type': 'audio/webm',
                  'cache_tier': 'prefix',
                  'expires_at': DateTime.now()
                          .add(const Duration(hours: 1))
                          .millisecondsSinceEpoch /
                      1000
                }
              }
            }),
            200)));
    final uri = await api.prepare(first);
    expect(uri.queryParameters['cache_version'], isNull);
    expect(api.preparedMedia(first['id'] as String), isNull);
  });
  test(
      'short collection prefetches related tail without waiting for playback or changing original order',
      () async {
    final api = FakeApi();
    api.delayedRelated =
        Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>();
    final audio = FakeAudio()..delayedPlay = Completer<void>();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    final originals = [
      first,
      second,
      {'id': 'third'},
      {'id': 'fourth'}
    ];
    final starting = player.startQueue(originals, 0, 'Frequently played');
    expect(api.relatedSession, isNotEmpty);
    expect(audio.plays, 1);
    final uri = audio.uri;
    final pending = player.replenish();
    api.delayedRelated!.complete((
      tracks: [
        second,
        {'id': 'fifth'},
        {'id': 'sixth'}
      ],
      hasMore: true
    ));
    await pending;
    expect(player.queue.map((t) => t['id']), [
      'recording:first',
      'recording:second',
      'third',
      'fourth',
      'fifth',
      'sixth'
    ]);
    expect(audio.uri, uri);
    expect(audio.plays, 1);
    expect(player.index, 0);
    audio.delayedPlay!.complete();
    await starting;
  });

  test(
      'related work from a replaced collection cannot append into its successor',
      () async {
    final api = FakeApi();
    final old = api.delayedRelated =
        Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Old');
    api.delayedRelated =
        Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>();
    await player.startQueue([second], 0, 'New');
    old.complete((
      tracks: [
        {'id': 'old-result'}
      ],
      hasMore: true
    ));
    await Future<void>.delayed(Duration.zero);
    expect(player.queue, [second]);
    api.delayedRelated!.complete((
      tracks: [
        {'id': 'new-result'}
      ],
      hasMore: false
    ));
    await player.replenish();
    expect(
        player.queue.map((t) => t['id']), ['recording:second', 'new-result']);
  });
  test(
      'explicit failed Play requests one source retry; ordinary play and automatic recovery do not',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    expect(audio.uri!.queryParameters['retry_source'], isNull);
    audio.startFailure =
        const PlaybackStartException(PlaybackStartFailure.failed, 'source');
    audio.changes.add('error');
    await player.play();
    expect(audio.uri!.queryParameters['retry_source'], '1');
    audio.startFailure = null;
    await player.play(automatic: true);
    expect(audio.uri!.queryParameters['retry_source'], isNull);
  });
  const third = {'id': 'recording:third', 'title': 'Third'};
  const fourth = {'id': 'recording:fourth', 'title': 'Fourth'};
  test('queue edits preserve current audio, position and listening epoch',
      () async {
    final audio = FakeAudio(), api = FakeApi();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second, third, fourth], 1, 'Playlist');
    audio.position = 23;
    final uri = audio.uri, epoch = player.listeningEpoch, plays = audio.plays;
    expect(player.removeQueueItem(0), true);
    expect(player.index, 0);
    expect(player.playNext(player.queue.last, fromIndex: 2), true);
    expect(player.queue.map((t) => t['id']),
        ['recording:second', 'recording:fourth', 'recording:third']);
    expect(player.current?['id'], 'recording:second');
    expect(audio.uri, uri);
    expect(player.position, 23);
    expect(audio.plays, plays);
    expect(player.listeningEpoch, epoch);
    await Future<void>.delayed(Duration.zero);
    expect(api.queuePlans.last, ['recording:fourth', 'recording:third']);
    expect(
        api.queuePlanRevisions.last, greaterThan(api.queuePlanRevisions.first));
    expect(player.removeQueueItem(player.index), false);
    expect(player.moveQueueItem(player.index, 1), false);
    await player.next();
    expect(player.current?['id'], 'recording:fourth');
  });

  test('reordering across the current row keeps its exact occurrence selected',
      () async {
    final player = WebPlayer(FakeApi(), FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second, third, fourth], 1, 'Test');
    expect(player.moveQueueItem(3, 0), true);
    expect(player.index, 2);
    expect(player.current?['id'], 'recording:second');
    expect(player.playNext(player.queue[0], fromIndex: 0), true);
    expect(player.index, 1);
    expect(player.queue[2]['id'], 'recording:fourth');
  });

  test(
      'late related results keep manual order and cannot restore removed songs',
      () async {
    final api = FakeApi()
      ..delayedRelated =
          Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second, third], 0, 'Test');
    final request = player.replenish();
    final stale = player.queueRevision;
    player.removeQueueItem(2);
    player.playNext(fourth);
    api.delayedRelated!.complete((
      tracks: [
        third,
        {'id': 'recording:fifth', 'title': 'Fifth'}
      ],
      hasMore: false
    ));
    await request;
    expect(player.queue.map((t) => t['id']), [
      'recording:first',
      'recording:fourth',
      'recording:second',
      'recording:fifth'
    ]);
    expect(player.removeQueueItem(1, revision: stale), false);
    expect(player.queue[1]['id'], 'recording:fourth');
  });

  test('editing during startup preserves the pending play promise', () async {
    final audio = FakeAudio()..delayedPlay = Completer<void>();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    final starting = player.startQueue([first, second, third], 1, 'Test');
    player.removeQueueItem(0);
    audio.delayedPlay!.complete();
    await starting;
    expect(player.current?['id'], 'recording:second');
    expect(player.index, 0);
    expect(audio.loadedTrackId, 'recording:second');
    expect(audio.plays, 1);
    expect(player.error, isNull);
  });

  test('duplicate occurrences restore the selected index and position',
      () async {
    final store = MemoryStore(), audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, store);
    await player.startQueue([first, second, first], 2, 'Repeats');
    audio.position = 19;
    player.save();
    expect(identical(player.queue[0], player.queue[2]), false);
    player.dispose();
    final restored = WebPlayer(FakeApi(), FakeAudio(), store);
    addTearDown(restored.dispose);
    expect(restored.index, 2);
    expect(restored.position, 19);
  });

  test('account changes reject stale edit actions and reset removed exclusions',
      () async {
    final player = WebPlayer(FakeApi(), FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second], 0, 'Old account');
    final revision = player.queueRevision;
    player.removeQueueItem(1);
    player.changeAccount(MemoryStore());
    player.playNext(second);
    player.playNext(first);
    expect(player.removeQueueItem(1, revision: revision), false);
    expect(player.current?['id'], 'recording:second');
  });

  test('lyrics starts after audio and panel joins the background request',
      () async {
    final pending = Completer<http.Response>();
    var calls = 0;
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      if (request.url.path.startsWith('/lyrics/')) {
        calls++;
        return pending.future;
      }
      return http.Response('{"url":"/audio"}', 200);
    }));
    final audio = FakeAudio()
      ..delayedPlay = Completer<void>()
      ..reportedPhase = PlaybackPhase.starting;
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    addTearDown(api.client.close);
    final playing = player.startQueue([first], 0, 'Test');
    await Future<void>.delayed(Duration.zero);
    expect(calls, 0);
    audio.reportedPhase = PlaybackPhase.playing;
    audio.delayedPlay!.complete();
    await playing;
    await Future<void>.delayed(Duration.zero);
    expect(calls, 1);
    final panel = player.lyricsFor(first);
    expect(calls, 1);
    pending.complete(http.Response(
        '{"lyrics_status":"found","lines":[{"text":"Ready lyric"}]}', 200));
    await panel;
    expect(player.cachedLyrics(first)?['lyrics_status'], 'found');
    await player.lyricsFor(first);
    expect(calls, 1);
  });
  test(
      'fully buffered resume retains expired URL; progress timing waits for advancing audio',
      () async {
    final api = FakeApi();
    var now = DateTime.utc(2026, 10, 10);
    api.clock = () => now;
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Fixture');
    audio.position = 37;
    audio.fullyBuffered = true;
    player.pause();
    now = now.add(const Duration(minutes: 21));
    final uri = audio.uri;
    audio.reportedPhase = PlaybackPhase.starting;
    await player.play();
    expect(audio.uri, uri);
    expect(audio.position, 37);
    expect(audio.reloads, 0);
    expect(audio.records, isNot(contains('resume-progress')));
    expect(player.diagnostics['resumeMs'], isNull);
    audio.position = 37.1;
    audio.reportedPhase = PlaybackPhase.playing;
    audio.changes.add('playback-state');
    expect(
        audio.records.where((event) => event == 'resume-progress').length, 1);
    expect(player.diagnostics['resumeMs'], isA<int>());
    expect(
        audio.recordDetails.lastWhere(
            (row) => row['event'] == 'resume-progress')['fullyBuffered'],
        true);
    audio.changes.add('timeupdate');
    expect(
        audio.records.where((event) => event == 'resume-progress').length, 1);
  });
  test('native session recovery works while hidden and respects explicit pause',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Saved');
    final source = audio.uri;
    player.lifecycle('pagehide', visible: false);
    audio.paused = true;
    audio.reportedPhase = PlaybackPhase.interrupted;
    audio.changes.add('interrupted');
    audio.reportedPhase = null;
    audio.changes.add('session-active');
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 2);
    expect(audio.uri, source);
    expect(player.playing, true);
    player.pause();
    audio.changes.add('session-active');
    audio.changes.add('canplay');
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 2);
    expect(audio.paused, true);
  });

  test(
      'native activation cannot loop through denied resume or an account change',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Saved');
    audio.paused = true;
    audio.reportedPhase = PlaybackPhase.interrupted;
    audio.changes.add('interrupted');
    audio.startFailure = const PlaybackStartException(
        PlaybackStartFailure.interrupted, 'permission');
    audio.changes.add('session-active');
    await Future<void>.delayed(Duration.zero);
    audio.startFailure = null;
    audio.changes.add('session-active');
    audio.changes.add('canplay');
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 1);
    player.changeAccount(MemoryStore());
    audio.changes.add('session-active');
    expect(audio.plays, 1);
  });
  test('expired paused resource renews immediately and restores position',
      () async {
    final api = FakeApi();
    var now = DateTime.utc(2026, 10, 8);
    api.clock = () => now;
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Fixture');
    audio.position = 37;
    player.pause();
    final old = audio.uri;
    now = now.add(const Duration(minutes: 21));
    final pending = player.play();
    expect(audio.uri, isNot(old));
    expect(audio.plays, 2);
    await pending;
    expect(audio.position, 37);
    expect(audio.records, contains('resume-new-session'));
  });
  test(
      'expired direct storage resumes through canonical relay at retained position',
      () async {
    var now = DateTime.utc(2026, 10, 29, 12);
    const track = {'id': 'generated01', 'title': 'Generated'};
    final version = 'v1-${'a' * 32}';
    final disposition =
        Uri.encodeQueryComponent('inline; filename="$version.aac-v1"');
    final api = MusicApi(Uri.parse('https://music.test'),
        storageBase: Uri.parse('https://storage.test'),
        client: MockClient((request) async => http.Response(
            jsonEncode({
              'prepared': {
                'generated01': {
                  'playback_path':
                      '/playback/stream/generated01?source_id=generated01&cache_variant=aac-v1&cache_version=$version',
                  'cache_tier': 'r2_full',
                  'mime_type': 'audio/mp4',
                  'expires_at':
                      DateTime.utc(2026, 10, 29, 22).millisecondsSinceEpoch /
                          1000,
                  'delivery_version': version,
                  'delivery_expires_at':
                      DateTime.utc(2026, 10, 29, 13).millisecondsSinceEpoch /
                          1000,
                  'delivery_url':
                      'https://storage.test/bucket/streams/generated01.bin.aac-v1?'
                          'X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261029T120000Z&'
                          'X-Amz-Expires=3600&X-Amz-Signature=${'b' * 64}&response-content-disposition=$disposition',
                }
              }
            }),
            200)))
      ..clock = () => now;
    addTearDown(api.client.close);
    await api.prepare(track);
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([track], 0, 'Fixture');
    expect(audio.uri!.origin, 'https://storage.test');
    audio.position = 37;
    player.pause();
    now = now.add(const Duration(minutes: 61));
    await player.play();
    expect(audio.uri!.origin, api.base.origin);
    expect(audio.position, 37);
    expect(audio.records, contains('resume-new-session'));
  });
  test(
      'native progress refreshes hidden lookahead without replacing current audio',
      () async {
    final api = FakeApi();
    var now = DateTime.utc(2026, 10, 8);
    api.clock = () => now;
    api.expiry[second['id'] as String] = now.add(const Duration(minutes: 2));
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second], 0, 'Fixture');
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, [second['id']]);
    final current = audio.uri;
    player.lifecycle('visibilitychange', visible: false);
    now = now.add(const Duration(minutes: 2));
    api.expiry[second['id'] as String] = now.add(const Duration(minutes: 5));
    audio.position = 1;
    audio.changes.add('timeupdate');
    expect(api.prepared.length, 2);
    player.lifecycle('visibilitychange', visible: true);
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared.length, 2);
    expect(audio.uri, current);
    expect(audio.plays, 1);
  });
  test('prepare expiry is scoped and missing expiry cannot retain stale data',
      () async {
    var withExpiry = true;
    final now = DateTime.utc(2026, 10, 8);
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response(
            jsonEncode({
              'prepared': {
                first['id']: withExpiry
                    ? {
                        'expires_at': now
                                .add(const Duration(minutes: 5))
                                .millisecondsSinceEpoch /
                            1000
                      }
                    : {}
              }
            }),
            200)))
      ..clock = () => now;
    addTearDown(api.client.close);
    await api.prepare(first);
    expect(api.preparedUntil(first['id'] as String),
        now.add(const Duration(minutes: 5)));
    withExpiry = false;
    await api.prepare(first);
    expect(api.preparedUntil(first['id'] as String), isNull);
    withExpiry = true;
    await api.prepare(first);
    api.userScopeId = 'account';
    expect(api.preparedUntil(first['id'] as String), isNull);
  });
  test('late request diagnostics cannot cross an account switch or switch back',
      () async {
    final response = Completer<http.Response>();
    final api = MusicApi(Uri.parse('http://test/'),
        client: MockClient((_) => response.future));
    final events = <String>[];
    api.userScopeId = 'first-account';
    api.onDiagnostic = (event, _) => events.add(event);
    final pending = api.prepare(first);
    api.userScopeId = 'second-account';
    api.userScopeId = 'first-account';
    response.complete(http.Response(
        jsonEncode({
          'prepared': {first['id']: {}}
        }),
        200));
    await pending;
    expect(events, isEmpty);
    api.client.close();
  });

  test('current request diagnostics still include API and cache evidence',
      () async {
    final api = MusicApi(Uri.parse('http://test/'),
        client: MockClient((_) async => http.Response(
            jsonEncode({
              'prepared': {
                first['id']: {'fully_cached': true}
              }
            }),
            200)));
    final events = <String, Map<String, dynamic>>{};
    api.onDiagnostic = (event, data) => events[event] = data;
    await api.prepare(first);
    expect(events.keys, ['api-response', 'prepare-result']);
    expect(events['prepare-result']!['fullyCached'], true);
    api.client.close();
  });

  test('prepared cache representation is used without losing the gateway',
      () async {
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response(
            jsonEncode({
              'prepared': {
                first['id']: {
                  'playback_path':
                      '/playback/stream/source00001?resolve=1&cache_variant=seek-v1'
                }
              }
            }),
            200)));
    addTearDown(api.client.close);
    final uri = await api.prepare(first);
    expect(uri.origin, 'https://music.test');
    expect(uri.path, '/playback/stream/source00001');
    expect(uri.queryParameters['cache_variant'], 'seek-v1');
  });

  test('prepared resource rejects external and unrelated gateway paths',
      () async {
    for (final path in [
      'https://other.test/playback/stream/id',
      '/history_seed'
    ]) {
      final api = MusicApi(Uri.parse('https://music.test'),
          client: MockClient((_) async => http.Response(
              jsonEncode({
                'prepared': {
                  first['id']: {'playback_path': path}
                }
              }),
              200)));
      try {
        await expectLater(api.prepare(first), throwsStateError);
      } finally {
        api.client.close();
      }
    }
  });

  test('accepted play without progress stays starting, not playing', () async {
    final audio = FakeAudio()..reportedPhase = PlaybackPhase.starting;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    expect(audio.paused, false);
    expect(player.playing, false);
    expect(player.busy, true);
    audio.reportedPhase = PlaybackPhase.playing;
    audio.changes.add('timeupdate');
    expect(player.playing, true);
    expect(player.busy, false);
  });

  test(
      'stuck resume reloads the same session first, then bounds source renewal',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Saved');
    audio.position = 37;
    final original = audio.uri;
    player.pause();
    final older = Completer<void>();
    audio.delayedPlay = older;
    final resume = player.play();
    audio.delayedPlay = null;
    audio.changes.add('resume-stalled');
    await Future<void>.delayed(Duration.zero);
    expect(audio.uri, original);
    expect(audio.reloads, 1);
    expect(audio.position, 37);
    expect(player.diagnostics['recoveryAttempts'], 1);
    audio.changes.add('resume-stalled');
    await Future<void>.delayed(Duration.zero);
    expect(audio.uri, isNot(original));
    expect(audio.uri!.queryParameters['playback_session'],
        isNot(original!.queryParameters['playback_session']));
    expect(audio.position, 37);
    expect(player.diagnostics['recoveryAttempts'], 2);
    older.completeError(const PlaybackStartException(
        PlaybackStartFailure.interrupted, 'abort'));
    await resume;
    expect(player.error, isNull);
    audio.changes.add('resume-stalled');
    expect(player.startupInterrupted, true);
    expect(player.diagnostics['loadedTrack'], isNull);
    expect(audio.plays, 4);
  });

  test(
      'network renewal bypasses failed immutable resource with same bytes and MIME',
      () async {
    const version = 'v1-0123456789abcdef0123456789abcdef';
    final uri = Uri.parse(
        'https://music.test/playback/stream/generated01?cache_variant=opus-v1&cache_version=$version');
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient(
            (_) async => http.Response('{"tracks":[],"has_more":false}', 200)));
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': uri.toString(),
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch
      }
    ]);
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 23;
    final epoch = player.listeningEpoch;
    audio.changes.add('media-network-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.reloads, 1);
    expect(audio.uri, uri);
    audio.changes.add('media-network-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.uri, isNot(uri));
    expect(audio.uri!.path, uri.path);
    expect(audio.uri!.queryParameters['cache_version'], version);
    expect(audio.uri!.queryParameters['cache_variant'], 'opus-v1');
    expect(audio.uri!.queryParameters['playback_session'], isNotEmpty);
    expect(audio.uri!.queryParameters['retry_source'], isNull);
    expect(audio.declaredMime, 'audio/webm');
    expect(audio.position, 23);
    expect(player.listeningEpoch, epoch);
    expect(api.preparedMedia(first['id'] as String), isNull);
    audio.changes.add('media-network-error');
    expect(player.diagnostics['loadedTrack'], isNull);
    expect(audio.plays, 3);
  });

  test('hidden stuck resume waits for explicit Play, without a reload loop',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Saved');
    audio.position = 28;
    player.lifecycle('visibilitychange', visible: false);
    final original = audio.uri;
    audio.needsNewSource = true;
    audio.changes.add('resume-stalled');
    expect(audio.uri, original);
    player.lifecycle('pageshow', visible: true);
    expect(audio.plays, 1);
    await player.play();
    expect(audio.uri, isNot(original));
    expect(audio.position, 28);
  });

  test(
      'foreground resumes interruption, but never explicit pause or restored queue',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first], 0, 'Saved');
    player.lifecycle('visibilitychange', visible: false);
    audio.paused = true;
    audio.reportedPhase = PlaybackPhase.interrupted;
    audio.changes.add('interrupted');
    audio.reportedPhase = null;
    player.lifecycle('pageshow', visible: true);
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 2);
    player.pause();
    player.lifecycle('pageshow', visible: true);
    expect(audio.plays, 2);
    player.changeAccount(MemoryStore());
    player.lifecycle('pageshow', visible: true);
    expect(audio.plays, 2);
  });

  test(
      'permission rejection requires a fresh gesture, not foreground auto retry',
      () async {
    final audio = FakeAudio()
      ..startFailure = const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'permission');
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.startFailure = null;
    player.lifecycle('pageshow', visible: true);
    expect(audio.plays, 0);
    await player.play();
    expect(audio.plays, 1);
  });

  test(
      'abort after the start promise settles retains the same source for retry',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    final source = audio.uri;
    expect(player.loading, false);
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-aborted');
    expect(player.startupInterrupted, true);
    expect(player.diagnostics['loadedTrack'], first['id']);
    await player.play();
    expect(audio.uri, source);
    expect(player.error, isNull);
  });

  test(
      'late rejection from interrupted start cannot overwrite a successful retry',
      () async {
    final oldStart = Completer<void>();
    final audio = FakeAudio()..delayedPlay = oldStart;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    final pending = player.start(first);
    audio.changes.add('media-aborted');
    audio.delayedPlay = null;
    await player.play();
    oldStart.completeError(const PlaybackStartException(
        PlaybackStartFailure.interrupted, 'abort'));
    await pending;
    expect(player.error, isNull);
    expect(player.diagnostics['loadedTrack'], first['id']);
    expect(audio.paused, false);
  });

  test(
      'foreground reconciliation and events from a different source cannot restart healthy audio',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    final source = audio.uri;
    audio.sourceMatches = false;
    audio.changes.add('error');
    expect(player.error, isNull);
    audio.sourceMatches = true;
    player.lifecycle('pageshow', visible: true);
    player.reconcile();
    expect(audio.uri, source);
    expect(audio.plays, 1);
    expect(player.error, isNull);
  });
  test('account switch fences old queue work and keeps queues separate',
      () async {
    final api = FakeApi()..delayedRelated = Completer();
    final audio = FakeAudio();
    final guest = MemoryStore();
    final account = MemoryStore();
    final player = WebPlayer(api, audio, guest);
    await player.start(first);
    player.changeAccount(account);
    api.delayedRelated!.complete((tracks: [second], hasMore: false));
    await Future<void>.delayed(Duration.zero);
    expect(player.queue, isEmpty);
    expect(audio.paused, isTrue);
    audio.changes.add('ended');
    expect(player.queue, isEmpty);
    player.changeAccount(guest);
    expect(player.current?['id'], first['id']);
    expect(audio.paused, isTrue);
    player.dispose();
  });

  test('album queue preserves original order and permits related additions',
      () async {
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    await player.startQueue([first, second], 0, 'Album');
    expect(player.queue, [first, second]);
    expect(api.relatedSession, isNotEmpty);
    await player.next();
    expect(player.current, second);
    expect(player.title, 'Album');
    player.dispose();
  });
  test('recent IDs carry across explicit discovery and queue starts', () async {
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    await Future<void>.delayed(Duration.zero);
    expect(api.relatedRecent, ['recording:first']);
    await player.startQueue([second, first], 0, 'Playlist');
    await player.start({'id': 'recording:third', 'title': 'Third'});
    await Future<void>.delayed(Duration.zero);
    expect(api.relatedRecent,
        ['recording:first', 'recording:second', 'recording:third']);
  });

  test('account switch clears recent IDs before new account discovery',
      () async {
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    await Future<void>.delayed(Duration.zero);
    player.changeAccount(MemoryStore());
    await player.start({'id': 'recording:account-b', 'title': 'Account B'});
    await Future<void>.delayed(Duration.zero);
    expect(api.relatedRecent, ['recording:account-b']);
  });
  test(
      'two workers continuously fill five startup buffers without a batch barrier',
      () async {
    final api = FakeApi();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    player.queue = [
      first,
      for (var i = 1; i <= 7; i++) {'id': 'recording:q$i'}
    ];
    player.hasMore = false;
    api.delayedResolve = Completer<Uri>();
    await player.playIndex(0);
    expect(api.prepared, ['recording:q1', 'recording:q2']);
    expect(api.fullDownloads, isEmpty);
    final pending = api.delayedResolve!;
    api.delayedResolve = null;
    pending.complete(api.streamUri(player.queue[1]));
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, [for (var i = 1; i <= 5; i++) 'recording:q$i']);
    expect(
        api.queuePlans.single, [for (var i = 1; i <= 7; i++) 'recording:q$i']);
    expect(player.queue.map((t) => t['id']),
        [first['id'], for (var i = 1; i <= 7; i++) 'recording:q$i']);
  });

  test('unrelated Search and full download cannot hold upcoming buffers',
      () async {
    final api = FakeApi();
    final search = Completer<Uri>();
    final download = Completer<Uri>();
    api.delayedTracks['recording:search'] = search;
    api.delayedFullDownload = download;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([
      {'id': 'recording:search'}
    ]);
    final third = {'id': 'recording:third'};
    final fourth = {'id': 'recording:fourth'};
    await player.startQueue([first, second, third, fourth], 0, 'Ordered');
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared,
        ['recording:search', second['id'], third['id'], fourth['id']]);
    expect(api.queuePlans.last, [second['id'], third['id'], fourth['id']]);
    await player.next();
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared.last, fourth['id']);
    expect(player.current, second);
    search.complete(api.streamUri(first));
    download.complete(api.streamUri(second));
  });

  test(
      'same Search and queue track share preparation; second song does not wait',
      () async {
    final api = FakeApi();
    final slow = Completer<Uri>();
    api.delayedTracks[second['id'] as String] = slow;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([second]);
    final third = {'id': 'recording:third'};
    await player.startQueue([first, second, third], 0, 'Ordered');
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, [second['id'], third['id']]);
    expect(api.fullDownloads, isEmpty);
    slow.complete(api.streamUri(second));
    await Future<void>.delayed(Duration.zero);
    expect(api.queuePlans.last, [second['id'], third['id']]);
  });

  test('rapid skips keep at most two queue preparations in flight', () async {
    final api = FakeApi();
    final slow1 = Completer<Uri>();
    final slow2 = Completer<Uri>();
    api.delayedTracks[second['id'] as String] = slow1;
    api.delayedTracks['recording:third'] = slow2;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([
      first,
      second,
      {'id': 'recording:third'},
      {'id': 'recording:fourth'}
    ], 0, 'Ordered');
    await player.next();
    expect(api.prepared.length, 2);
    slow2.complete(api.streamUri(first));
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared.last, 'recording:fourth');
    slow1.complete(api.streamUri(second));
  });

  test(
      'backend queue plan survives hiding even when a neighbour preparation fails',
      () async {
    final api = FakeApi();
    final failed = Completer<Uri>();
    api.delayedTracks[second['id'] as String] = failed;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([
      first,
      second,
      {'id': 'recording:third'}
    ], 0, 'Queue');
    await Future<void>.delayed(Duration.zero);
    expect(api.queuePlans.single, [second['id'], 'recording:third']);
    failed.completeError(StateError('provider unavailable'));
    player.lifecycle('pagehide', visible: false);
    await Future<void>.delayed(Duration.zero);
    expect(api.cancelledQueueRevisions, isEmpty);
    expect(api.queuePlans.length, 1);
  });

  testWidgets(
      'queue status refresh is bounded, survives restart and stops while hidden or paused',
      (tester) async {
    final api = FakeApi();
    var now = DateTime.utc(2026, 10, 17);
    api.clock = () => now;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    await player.startQueue([first, second], 0, 'Queue');
    await tester.pump();
    expect(api.queuePlans.length, 1);
    now = now.add(const Duration(seconds: 15));
    await tester.pump(const Duration(seconds: 15));
    expect(api.queuePlans.length, 1);
    expect(api.queueStatusPolls, 1);
    player.lifecycle('hidden', visible: false);
    now = now.add(const Duration(seconds: 30));
    await tester.pump(const Duration(seconds: 30));
    expect(api.queuePlans.length, 1);
    expect(api.queueStatusPolls, 1);
    api.queueStatus =
        'missing'; // The API restarted and lost its transient plan.
    player.lifecycle('visible', visible: true);
    await tester.pump();
    expect(api.queuePlans.length, 2);
    expect(
        api.queuePlanRevisions.last, greaterThan(api.queuePlanRevisions.first));
    player.pause();
    now = now.add(const Duration(seconds: 30));
    await tester.pump(const Duration(seconds: 30));
    expect(api.queuePlans.length, 2);
    player.dispose();
    await tester.pump();
  });

  testWidgets(
      'busy preparation retries in two seconds without a provider cooldown',
      (tester) async {
    final api = FakeApi();
    var now = DateTime.utc(2026, 10, 17);
    api.clock = () => now;
    final busy = Completer<Uri>();
    api.delayedTracks[second['id'] as String] = busy;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    await player.startQueue([first, second], 0, 'Queue');
    busy.completeError(PlaybackPreparationException('preparation_busy'));
    await tester.pump();
    expect(api.prepared.length, 1);
    api.delayedTracks.remove(second['id']);
    now = now.add(const Duration(seconds: 2));
    await tester.pump(const Duration(seconds: 2));
    expect(api.prepared.length, 2);
    player.dispose();
    await tester.pump();
  });

  test('replacing the queue and changing account cancel earlier backend intent',
      () async {
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.startQueue([first, second], 0, 'First');
    await Future<void>.delayed(Duration.zero);
    await player.startQueue([
      first,
      {'id': 'recording:third'}
    ], 0, 'Second');
    await Future<void>.delayed(Duration.zero);
    expect(api.cancelledQueueRevisions.length, 1);
    player.changeAccount(MemoryStore());
    await Future<void>.delayed(Duration.zero);
    expect(api.cancelledQueueRevisions.length, 2);
    expect(api.cancelledQueueRevisions.last,
        greaterThan(api.cancelledQueueRevisions.first));
  });

  test('account switch fences old shared preparation', () async {
    final api = FakeApi();
    final old = Completer<Uri>();
    api.delayedTracks[second['id'] as String] = old;
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([second]);
    player.changeAccount(MemoryStore());
    api.delayedTracks.remove(second['id']);
    await player.startQueue([first, second], 0, 'New account');
    await Future<void>.delayed(Duration.zero);
    old.complete(Uri.parse('https://foreign.test/old'));
    await Future<void>.delayed(Duration.zero);
    await player.next();
    expect(audio.uri!.host, 'music.test');
    expect(api.prepared, [second['id'], second['id']]);
  });
  test('return preserves active position without reloading audio', () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 42;
    final uri = audio.uri;
    player.reconcile();
    expect(player.position, 42);
    expect(audio.uri, uri);
    expect(audio.plays, 1);
    expect(audio.paused, false);
  });
  test('mismatch stops foreign source and Play reloads selected track',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.loadedTrackId = 'recording:foreign';
    player.reconcile();
    expect(audio.paused, true);
    expect(player.current, first);
    await player.play();
    expect(audio.loadedTrackId, first['id']);
    expect(audio.paused, false);
    audio.changes.add('error');
    expect(audio.paused, true);
    await player.play();
    expect(audio.loadedTrackId, first['id']);
    expect(audio.paused, false);
  });
  test('search prepares only five unique results in order', () async {
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([
      first,
      first,
      second,
      for (var i = 3; i <= 8; i++) {'id': 'recording:$i'}
    ]);
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, [
      'recording:first',
      'recording:second',
      'recording:3',
      'recording:4',
      'recording:5'
    ]);
  });
  test('new search replaces waiting work without concurrent preparation',
      () async {
    final api = FakeApi()..delayedResolve = Completer<Uri>();
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([first, second]);
    player.preloadSearch([
      {'id': 'recording:new'}
    ]);
    expect(api.prepared, ['recording:first']);
    api.delayedResolve!.complete(api.streamUri(first));
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, ['recording:first', 'recording:new']);
  });
  test('selection bypasses pending preparation and drops remaining search work',
      () async {
    final api = FakeApi()
      ..delayedResolve = Completer<Uri>()
      ..delayedRelated =
          Completer<({List<Map<String, dynamic>> tracks, bool hasMore})>();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    player.preloadSearch([first, second]);
    await player.start({'id': 'recording:selected'});
    expect(audio.plays, 1);
    api.delayedResolve!.complete(api.streamUri(first));
    await Future<void>.delayed(Duration.zero);
    expect(api.prepared, ['recording:first']);
    api.delayedRelated!
        .complete((tracks: <Map<String, dynamic>>[], hasMore: false));
  });
  test('first Play reaches the media element before any HTTP await', () async {
    final api = FakeApi()..delayedResolve = Completer<Uri>();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    final pending = player.start(first);
    expect(audio.plays, 1);
    expect(Uri.decodeComponent(audio.uri!.path),
        '/playback/stream/recording:first');
    expect(audio.uri!.queryParameters['resolve'], '1');
    await pending;
    api.delayedResolve!.complete(api.streamUri(second));
  });
  test('related queue advances automatically without replacing the media port',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    await Future<void>.delayed(Duration.zero);
    expect(player.queue.length, 2);
    audio.changes.add('ended');
    await Future<void>.delayed(Duration.zero);
    expect(player.index, 1);
    expect(audio.plays, 2);
    expect(audio.uri.toString(), contains('recording:second'));
  });
  test('discovery session persists across reload and related requests',
      () async {
    final store = MemoryStore();
    final api = FakeApi();
    final player = WebPlayer(api, FakeAudio(), store);
    await player.start(first);
    await Future<void>.delayed(Duration.zero);
    final session = api.relatedSession;
    expect(session, isNotEmpty);
    expect(api.relatedRecent, contains('recording:first'));
    final saved = jsonDecode(store.value!) as Map;
    expect(saved['discoverySession'], session);
    expect(saved['recentPlayed'], ['recording:first']);
    player.dispose();
    final restoredApi = FakeApi();
    final restored = WebPlayer(restoredApi, FakeAudio(), store);
    addTearDown(restored.dispose);
    expect(jsonDecode(store.value!)['discoverySession'], session);
    await restored.replenish();
    expect(restoredApi.relatedSession, session);
    expect(restoredApi.relatedRecent, ['recording:first']);
  });
  test(
      'reload restores queue and position without autoplay; explicit Play seeks',
      () async {
    final store = MemoryStore();
    final original = WebPlayer(FakeApi(), FakeAudio(), store);
    await original.start(first);
    original.seek(23);
    original.dispose();
    final audio = FakeAudio();
    final restored = WebPlayer(FakeApi(), audio, store);
    addTearDown(restored.dispose);
    expect(restored.current!['id'], first['id']);
    expect(restored.position, 23);
    expect(restored.duration, 100);
    expect(audio.plays, 0);
    await restored.play();
    audio.changes.add('loadedmetadata');
    expect(audio.position, 23);
  });
  test('reload preserves progress while audio metadata is unavailable',
      () async {
    final store = MemoryStore()
      ..value = jsonEncode({
        'version': 1,
        'queue': [first],
        'current': first['id'],
        'title': 'Saved queue',
        'position': 100,
        'duration': 222,
        'hasMore': false,
      });
    final audio = FakeAudio()
      ..metadataAvailable = false
      ..duration = 0;
    final player = WebPlayer(FakeApi(), audio, store);
    addTearDown(player.dispose);
    expect(player.position, 100);
    expect(player.duration, 222);
    expect(audio.uri, isNull);
    await player.play();
    player.save(); // Backgrounding during loading must not persist 0/0.
    expect(jsonDecode(store.value!)['position'], 100);
    expect(jsonDecode(store.value!)['duration'], 222);
    audio.duration = 222;
    audio.changes.add('loadedmetadata');
    expect(audio.position, 100);
    player.seek(45);
    player.save();
    expect(jsonDecode(store.value!)['position'], 45);
    await player.start(second);
    expect(player.position, 0);
  });
  test('pause stays paused when an outstanding media start completes',
      () async {
    final api = FakeApi();
    final audio = FakeAudio()..delayedPlay = Completer<void>();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    final pending = player.start(first);
    player.pause();
    audio.delayedPlay!.complete();
    await pending;
    expect(audio.paused, isTrue);
    expect(player.loading, isFalse);
  });
  test('late recommendations cannot overwrite a new session', () async {
    final api = FakeApi()..delayedRelated = Completer();
    final old = api.delayedRelated!;
    final player = WebPlayer(api, FakeAudio(), MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    api.delayedRelated = Completer();
    await player.start(second);
    old.complete((tracks: [first], hasMore: false));
    await Future<void>.delayed(Duration.zero);
    expect(player.queue, [second]);
    api.delayedRelated!
        .complete((tracks: <Map<String, dynamic>>[], hasMore: false));
    await Future<void>.delayed(Duration.zero);
  });
  test('autoplay rejection leaves a retryable queue', () async {
    final audio = FakeAudio()..blocked = true;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    expect(player.error, isNotNull);
    final failedSession = audio.uri!.queryParameters['playback_session'];
    audio.blocked = false;
    await player.play();
    expect(player.error, isNull);
    expect(audio.plays, 1);
    expect(
        audio.uri!.queryParameters['playback_session'], isNot(failedSession));
  });
  test('browser startup interruption retains selected source for gesture retry',
      () async {
    final audio = FakeAudio()
      ..startFailure = const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'permission');
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    final preparedUri = audio.uri;
    expect(player.startupInterrupted, isTrue);
    expect(player.current, first);
    expect(audio.loadedTrackId, first['id']);
    expect(player.error, contains('Tap Play'));
    audio.startFailure = null;
    await player.play();
    expect(player.startupInterrupted, isFalse);
    expect(player.error, isNull);
    expect(audio.uri, preparedUri);
    expect(audio.plays, 1);
  });
  test('hidden startup media abort retains source but network error does not',
      () async {
    for (final event in ['media-aborted', 'error']) {
      final audio = FakeAudio()..delayedPlay = Completer<void>();
      final player = WebPlayer(FakeApi(), audio, MemoryStore());
      final pending = player.start(first);
      await Future<void>.delayed(Duration.zero);
      final source = audio.uri;
      player.lifecycle('visibilitychange', visible: false);
      audio.changes.add(event);
      expect(player.startupInterrupted, event == 'media-aborted');
      expect(player.diagnostics['loadedTrack'],
          event == 'media-aborted' ? first['id'] : isNull);
      audio.delayedPlay!.completeError(const PlaybackStartException(
          PlaybackStartFailure.interrupted, 'abort'));
      await pending;
      audio.delayedPlay = null;
      await player.play();
      expect(player.error, isNull);
      if (event == 'media-aborted') expect(audio.uri, source);
      player.dispose();
    }
  });
  test('resume also records hidden startup interruption', () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    player.pause();
    audio.delayedPlay = Completer<void>();
    final pending = player.play();
    expect(player.loading, isTrue);
    player.lifecycle('visibilitychange', visible: false);
    audio.delayedPlay!.completeError(const PlaybackStartException(
        PlaybackStartFailure.interrupted, 'abort'));
    await pending;
    expect(player.startupInterrupted, isTrue);
    expect(player.loading, isFalse);
    expect(player.diagnostics['loadedTrack'], first['id']);
  });
  test('unclassified startup error remains failed and clears the loaded source',
      () async {
    final audio = FakeAudio()..blocked = true;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    expect(player.startupInterrupted, isFalse);
    expect(player.error, contains('could not start'));
    expect(player.current, first);
    expect(player.diagnostics['loadedTrack'], isNull);
  });
  test('API keeps canonical identity and rejects an unexpected stream origin',
      () async {
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      expect(request.url.path, '/playback/resolve');
      expect(jsonDecode(request.body)['track_key'], 'recording:first');
      return http.Response(
          jsonEncode({'url': 'https://other.test/stream'}), 200);
    }));
    addTearDown(api.client.close);
    await expectLater(api.resolve(first), throwsStateError);
  });
  test('lookahead uses existing preparation with one track', () async {
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      expect(request.url.path, '/prepare_session');
      final body = jsonDecode(request.body);
      expect(body['track_keys'], ['recording:first']);
      expect(body['lookahead'], 1);
      expect(body['active_queue'], true);
      return http.Response(
          jsonEncode({
            'prepared': {'recording:first': {}}
          }),
          200);
    }));
    addTearDown(api.client.close);
    expect(await api.prepare(first, activeQueue: true), api.streamUri(first));
  });
  test(
      'same playing track replaces queue without reload or listening increment',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 37;
    final uri = audio.uri;
    final epoch = player.listeningEpoch;
    final plays = audio.plays;
    await player.startQueue([second, first], 1, 'New collection');
    expect(player.index, 1);
    expect(player.title, 'New collection');
    expect(audio.position, 37);
    expect(audio.uri, uri);
    expect(audio.plays, plays);
    expect(player.listeningEpoch, epoch);
    await player.start(first);
    expect(audio.uri, uri);
    expect(audio.position, 37);
    expect(audio.plays, plays);
  });
  test(
      'native network failure retries the pinned resource without resetting the listening epoch',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 23;
    final uri = audio.uri, epoch = player.listeningEpoch;
    audio.changes.add('media-network-error');
    await Future<void>.delayed(Duration.zero);
    expect(audio.reloads, 1);
    expect(audio.uri, uri);
    expect(audio.position, 23);
    expect(player.listeningEpoch, epoch);
    expect(player.diagnostics['loadedTrack'], first['id']);
  });
  test(
      'hidden native network failure retries once without waiting for foreground',
      () async {
    final audio = FakeAudio();
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 23;
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error');
    expect(audio.reloads, 1);
    await Future<void>.delayed(Duration.zero);
    audio.changes.add('media-network-error');
    expect(audio.reloads, 1); // No repeated hidden reload loop.
    expect(player.diagnostics['loadedTrack'], first['id']);
    expect(player.diagnostics['playRequested'], isTrue);
    player.lifecycle('visibilitychange', visible: true);
    await Future<void>.delayed(Duration.zero);
    expect(audio.position, 23);
    expect(player.current, first);
  });
  test('late verified cache replaces a failed resource while hidden', () async {
    final api = FakeApi();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    final epoch = player.listeningEpoch;
    final uri = Uri.parse('https://music.test/playback/stream/recording%3Afirst'
        '?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef');
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': uri.toString(),
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch,
      }
    ]);
    expect(audio.uri, isNot(uri)); // Healthy playback is never switched.
    audio.position = 23;
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error');
    expect(audio.uri, uri); // load and native play happen synchronously.
    expect(audio.plays, 2);
    await Future<void>.delayed(Duration.zero);
    expect(audio.position, 23);
    expect(player.listeningEpoch, epoch);
    expect(audio.declaredMime, 'audio/webm');
    expect(player.diagnostics['recoveryAttempts'], 1);
    expect(
        audio.recordDetails.lastWhere((r) => r['event'] == 'resume-recovery'),
        containsPair('choice', 'cached-replacement'));
    audio.changes.add('media-network-error');
    expect(audio.plays, 2); // No second hidden retry.
    player.pause();
    player.lifecycle('visibilitychange', visible: true);
    expect(audio.plays, 2);
  });

  test('hidden cached recovery respects browser permission rejection',
      () async {
    final api = FakeApi();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': 'https://music.test/playback/stream/recording%3Afirst'
            '?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef',
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch,
      }
    ]);
    player.lifecycle('visibilitychange', visible: false);
    audio.startFailure = const PlaybackStartException(
        PlaybackStartFailure.interrupted, 'permission');
    audio.changes.add('media-network-error');
    await Future<void>.delayed(Duration.zero);
    expect(player.diagnostics['playRequested'], isFalse);
    expect(player.error, contains('permission'));
    player.lifecycle('visibilitychange', visible: true);
    expect(audio.plays, 1);
  });
  test('foreground recovery uses cache learned after a hidden error', () async {
    final api = FakeApi();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.position = 23;
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error');
    await Future<void>.delayed(Duration.zero);
    audio.changes
        .add('media-network-error'); // The bounded hidden retry also failed.
    final uri = Uri.parse('https://music.test/playback/stream/recording%3Afirst'
        '?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef');
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': uri.toString(),
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch,
      }
    ]);
    player.lifecycle('visibilitychange', visible: true);
    expect(audio.uri, uri);
    await Future<void>.delayed(Duration.zero);
    expect(audio.position, 23);
    expect(player.diagnostics['recoveryAttempts'], 2);
  });
  test('hidden errors cannot exceed the automatic recovery budget', () async {
    final api = FakeApi();
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': 'https://music.test/playback/stream/recording%3Afirst'
            '?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef',
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch,
      }
    ]);
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error'); // Cached replacement: retry 1.
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 2);
    audio.changes.add('media-network-error'); // Defer same cached resource.
    player.lifecycle('visibilitychange', visible: true); // Retry 2.
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 3);
    expect(player.diagnostics['recoveryAttempts'], 2);
    player.lifecycle('visibilitychange', visible: false);
    audio.changes.add('media-network-error');
    player.lifecycle('visibilitychange', visible: true);
    await Future<void>.delayed(Duration.zero);
    expect(audio.plays, 3);
    expect(audio.reloads, 0);
    expect(player.diagnostics['playRequested'], isFalse);
    await player.play();
    expect(audio.plays, 4);
    expect(player.diagnostics['recoveryAttempts'], 0);
  });
  for (final reason in ['resume-stalled', 'playback-stalled-timeout']) {
    test('confirmed $reason selects a late cached resource', () async {
      final api = FakeApi();
      final audio = FakeAudio();
      final player = WebPlayer(api, audio, MemoryStore());
      addTearDown(player.dispose);
      await player.start(first);
      final uri = Uri.parse(
          'https://music.test/playback/stream/recording%3Afirst'
          '?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef');
      api.restorePreparedMedia([
        {
          'id': first['id'],
          'uri': uri.toString(),
          'mime': 'audio/webm',
          'until': DateTime.now()
              .add(const Duration(hours: 1))
              .millisecondsSinceEpoch,
        }
      ]);
      final original = audio.uri;
      audio.position = 23;
      audio.changes.add('waiting');
      audio.changes.add('stalled');
      audio.position = 24;
      audio.changes.add('timeupdate');
      expect(audio.uri, original);
      expect(audio.plays, 1);
      audio.changes.add(reason);
      expect(audio.uri, uri);
      await Future<void>.delayed(Duration.zero);
      expect(audio.position, 24);
      expect(player.diagnostics['recoveryAttempts'], 1);
    });
  }
  test(
      'Opus descriptor retains selected duration without changing catalogue metadata',
      () async {
    final uri = Uri.parse(
        'https://music.test/playback/stream/generated01?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef');
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient(
            (_) async => http.Response('{"tracks":[],"has_more":false}', 200)));
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': uri.toString(),
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch,
        'duration': 197.84
      }
    ]);
    final audio = FakeAudio()..metadataAvailable = false;
    final track = {...first, 'duration': 209};
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(track);
    expect(audio.uri, uri);
    expect(audio.declaredMime, 'audio/webm');
    expect(player.duration, 197.84);
    expect(player.current!['duration'], 209);
    expect(api.savedPreparedMedia().single['duration'], 197.84);
    audio.duration = 197.91;
    audio.changes.add('loadedmetadata');
    expect(player.duration, 197.91);
    expect(
        audio.recordDetails.lastWhere(
            (r) => r['event'] == 'source-duration')['catalogueDuration'],
        209);
    audio.duration = 198.01;
    audio.changes.add('durationchange');
    expect(player.duration, 198.01);
    expect(player.current!['duration'], 209);
  });
  test('server-confirmed changed cache discards pinned hint before retry',
      () async {
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient(
            (_) async => http.Response('{"tracks":[],"has_more":false}', 200)));
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri':
            'https://music.test/playback/stream/generated01?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef',
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch
      }
    ]);
    final audio = FakeAudio();
    final player = WebPlayer(api, audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.changes.add('error');
    audio.sourceFailureCode = 'playback_source_expired';
    audio.changes.add('source-failure');
    expect(api.preparedMedia(first['id'] as String), isNull);
    await player.play();
    expect(audio.uri!.queryParameters['cache_version'], isNull);
    expect(audio.uri!.queryParameters['retry_source'], '1');
  });

  test('failed background preparation retains a verified cached resource',
      () async {
    final uri = Uri.parse(
        'https://music.test/playback/stream/generated01?cache_variant=opus-v1&cache_version=v1-0123456789abcdef0123456789abcdef');
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response(
            '{"prepared":{},"failed":{"recording:first":{"code":"source_blocked"}}}',
            200)));
    addTearDown(api.client.close);
    api.restorePreparedMedia([
      {
        'id': first['id'],
        'uri': uri.toString(),
        'mime': 'audio/webm',
        'until':
            DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch
      }
    ]);
    await expectLater(api.prepare(first, cacheFull: true), throwsStateError);
    expect(api.preparedMedia(first['id'] as String)!.uri, uri);
    api.forgetPreparedMedia(first['id'] as String);
    expect(api.preparedMedia(first['id'] as String), isNull);
  });
  test(
      'provider block replaces interruption wording and ignores another source',
      () async {
    final audio = FakeAudio()
      ..startFailure =
          const PlaybackStartException(PlaybackStartFailure.failed, 'network');
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    await player.start(first);
    audio.sourceFailureCode = 'source_blocked';
    audio.loadedTrackId = second['id'] as String;
    audio.changes.add('source-failure');
    expect(player.error, isNot(contains('provider')));
    audio.loadedTrackId = first['id'] as String;
    audio.changes.add('source-failure');
    expect(player.error, contains('provider is blocking'));
    expect(player.startupInterrupted, isFalse);
    expect(player.phase, PlaybackPhase.failed);
    expect(player.diagnostics['playRequested'], isFalse);
  });

  test('provider failure during startup fences native rejection and recovery',
      () async {
    final completion = Completer<void>();
    final audio = FakeAudio()
      ..delayedPlay = completion
      ..reportedPhase = PlaybackPhase.buffering;
    final player = WebPlayer(FakeApi(), audio, MemoryStore());
    addTearDown(player.dispose);
    final starting = player.start(first);
    await Future<void>.delayed(Duration.zero);
    expect(player.loading, true);
    expect(player.error, isNull);
    audio.sourceFailureCode = 'source_blocked';
    audio.changes.add('source-failure');
    expect(player.error, contains('provider is blocking'));
    expect(player.loading, false);
    expect(player.diagnostics['playRequested'], false);
    expect(audio.paused, true);
    audio.changes.add('media-source-error');
    audio.changes.add('error');
    completion.completeError(
        const PlaybackStartException(PlaybackStartFailure.failed, 'network'));
    await starting;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    expect(player.error, contains('provider is blocking'));
    expect(audio.plays, 1);
    expect(audio.reloads, 0);
  });
}
