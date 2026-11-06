import 'dart:convert';
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import '../lib/web/web_library.dart';

SupabaseClient _client(http.Client httpClient) => SupabaseClient(
      'https://library.test',
      'anon-key',
      accessToken: () async => 'test-token',
      httpClient: _RequestAwareClient(httpClient),
    );

// PostgREST reads response.request; MockClient does not populate it itself.
class _RequestAwareClient extends http.BaseClient {
  final http.Client inner;
  _RequestAwareClient(this.inner);
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final response = await inner.send(request);
    return http.StreamedResponse(response.stream, response.statusCode,
        headers: response.headers, request: request);
  }
}

http.Response _json(Object body, {int status = 200}) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

class _PlaylistServer {
  Map<String, dynamic> state = {
    'id': 'shared',
    'name': 'Shared',
    'revision': 0,
    'tracks': <Map>[]
  };
  final requests = <http.Request>[];
  bool missing = false;
  Future<http.Response> send(http.Request request) async {
    requests.add(request);
    if (request.url.path.endsWith('/library_tracks')) return _json([]);
    if (request.url.path.endsWith('/neatie_playlist_library'))
      return _json([state]);
    expect(request.url.path, '/rest/v1/rpc/neatie_edit_playlist');
    if (missing)
      return _json({'code': 'PGRST202', 'message': 'not found'}, status: 404);
    final body = jsonDecode(request.body) as Map;
    expect(body.containsKey('user_id'), false);
    if (body['p_action'] == 'create') {
      expect(body['p_expected_revision'], isNull);
      state = {
        'id': body['p_playlist_id'],
        'name': body['p_payload']['name'],
        'created_at': '2026-11-02T01:02:03Z',
        'revision': 0,
        'tracks': <Map>[
          if (body['p_payload']['track_id'] != null)
            {
              'id': 1,
              'position': 0,
              'track_id': body['p_payload']['track_id'],
              'track_data': body['p_payload']['track_data']
            }
        ]
      };
      return _json(state);
    }
    if (body['p_expected_revision'] != state['revision']) {
      return _json({'code': '40001', 'message': 'playlist_conflict'},
          status: 409);
    }
    final payload = body['p_payload'] as Map;
    if (body['p_action'] == 'rename') state['name'] = payload['name'];
    if (body['p_action'] == 'append') {
      final rows = state['tracks'] as List;
      rows.add({
        'id': rows.length + 1,
        'position': rows.length,
        'track_id': payload['track_id'],
        'track_data': payload['track_data']
      });
    }
    state['revision'] = (state['revision'] as int) + 1;
    return _json(state);
  }
}

void main() {
  test(
      'create with a song sends one edit, keeps creation date and strips queue occurrence fields',
      () async {
    final server = _PlaylistServer();
    final library = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(library.dispose);
    final id = await library.createPlaylist('  Night drive  ', initialTrack: {
      'id': 'song',
      'title': 'Song',
      'playlist_entry_id': 'old',
      'playlist_position': 3
    });
    expect(server.requests, hasLength(1));
    final body = jsonDecode(server.requests.single.body) as Map;
    expect(body['p_action'], 'create');
    expect(body['p_payload']['name'], 'Night drive');
    expect(
        (body['p_payload']['track_data'] as Map)
            .containsKey('playlist_entry_id'),
        false);
    expect(library.playlists.single['id'], id);
    expect(library.playlists.single['created_at'], '2026-11-02T01:02:03Z');
    expect((library.playlists.single['tracks'] as List).single['id'], 'song');
    await library.refresh();
    expect(library.playlists.single['created_at'], '2026-11-02T01:02:03Z');
  });
  test(
      'likes update immediately, serialize rapid intent and avoid library reads',
      () async {
    final first = Completer<http.Response>();
    final requests = <http.Request>[];
    final library = WebLibrary(_client(MockClient((request) async {
      requests.add(request);
      if (requests.length == 1) return first.future;
      return _json([], status: 200);
    })), 'user-1');
    addTearDown(library.dispose);
    final track = {'id': 'one', 'title': 'One'};
    final a = library.setLiked(track, true);
    expect(library.likes.single['id'], 'one');
    final b = library.setLiked(track, false);
    expect(library.likes, isEmpty);
    await Future<void>.delayed(Duration.zero);
    expect(requests.map((r) => r.method), ['POST']);
    first.complete(_json([], status: 201));
    await Future.wait([a, b]);
    expect(requests.map((r) => r.method), ['POST', 'DELETE']);
    expect(library.likes, isEmpty);
  });

  test('latest failed write rolls back to the last acknowledged state',
      () async {
    final first = Completer<http.Response>();
    var writes = 0;
    final library = WebLibrary(_client(MockClient((request) async {
      writes++;
      if (writes == 1) return first.future;
      return _json({'message': 'failed'}, status: 500);
    })), 'user-1');
    addTearDown(library.dispose);
    final track = {'id': 'one', 'title': 'One'};
    final a = library.setLiked(track, true);
    final b = library.setLiked(track, false);
    final failed = expectLater(b, throwsA(anything));
    first.complete(_json([], status: 201));
    await a;
    await failed;
    expect(library.likes.single['id'], 'one');
    expect(library.error, isNotNull);
  });

  test('a later intent succeeds after an earlier write fails', () async {
    final first = Completer<http.Response>();
    final second = Completer<http.Response>();
    var writes = 0;
    final library = WebLibrary(_client(MockClient((request) async {
      writes++;
      if (writes == 1) return first.future;
      return second.future;
    })), 'user-1');
    addTearDown(library.dispose);
    final track = {'id': 'one', 'title': 'One'};
    final a = library.setLiked(track, true);
    final b = library.setLiked(track, false);
    first.complete(_json({'message': 'failed'}, status: 500));
    await a;
    expect(library.error, isNull);
    second.complete(_json([]));
    await b;
    expect(library.likes, isEmpty);
    expect(library.error, isNull);
    expect(writes, 2);
  });

  test('queued like retains captured authorization across token refresh',
      () async {
    final first = Completer<http.Response>();
    final requests = <http.Request>[];
    var token = 'old-valid-token';
    final library = WebLibrary(_client(MockClient((request) async {
      requests.add(request);
      if (requests.length == 1) return first.future;
      return _json([]);
    })), 'user-1', accessToken: () => token, scopeIsCurrent: () => true);
    addTearDown(library.dispose);
    final a = library.setLiked({'id': 'one'}, true);
    final b = library.setLiked({'id': 'two'}, true);
    await Future<void>.delayed(Duration.zero);
    token = 'refreshed-token';
    first.complete(_json([], status: 201));
    await Future.wait([a, b]);
    expect(requests.length, 2);
    expect(requests.last.headers['Authorization'], 'Bearer old-valid-token');
    expect(library.likes.length, 2);
    expect(library.error, isNull);
  });

  test('refresh started during a like cannot replace its acknowledged intent',
      () async {
    final write = Completer<http.Response>();
    final read = Completer<http.Response>();
    final library = WebLibrary(_client(MockClient((request) async {
      if (request.method != 'GET') return write.future;
      if (request.url.path.endsWith('/library_tracks')) return read.future;
      return _json([]);
    })), 'user-1');
    addTearDown(library.dispose);
    final saving = library.setLiked({'id': 'one', 'title': 'One'}, true);
    final refreshing = library.refresh();
    await Future<void>.delayed(Duration.zero);
    write.complete(_json([], status: 201));
    await saving;
    read.complete(_json([]));
    await refreshing;
    expect(library.likes.single['id'], 'one');
  });

  test('disposed account cannot issue queued writes after returning to its id',
      () async {
    final first = Completer<http.Response>();
    var requests = 0;
    var current = true;
    final library = WebLibrary(_client(MockClient((request) async {
      requests++;
      return first.future;
    })), 'user-1', scopeIsCurrent: () => current);
    final a = library.setLiked({'id': 'one'}, true);
    final b = library.setLiked({'id': 'two'}, true);
    final failed = expectLater(b, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    current = false;
    library.dispose();
    current = true;
    first.complete(_json([], status: 201));
    await a;
    await failed;
    expect(requests, 1);
  });
  test(
      'cloud create sends one atomic request and publishes the committed playlist',
      () async {
    final server = _PlaylistServer();
    final library = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.createPlaylist('  Road songs  ');
    expect(server.requests.length, 1);
    expect(library.playlists.single['name'], 'Road songs');
    expect(library.playlists.single['revision'], 0);
    expect(library.playlists.single['id'], startsWith('web-'));
    expect(library.playlists.single['tracks'], isEmpty);
  });
  test('refresh started during a save cannot replace its committed snapshot',
      () async {
    final server = _PlaylistServer();
    final ack = Completer<http.Response>(),
        oldRead = Completer<http.Response>();
    var delay = false;
    final library = WebLibrary(_client(MockClient((request) async {
      if (delay && request.url.path.endsWith('/neatie_playlist_library'))
        return oldRead.future;
      final result = await server.send(request);
      if (delay && request.url.path.endsWith('/neatie_edit_playlist')) {
        await ack.future;
      }
      return result;
    })), 'user-1', playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    final before = jsonEncode([server.state]);
    delay = true;
    final writing = library.renamePlaylist('shared', 'Committed');
    await Future<void>.delayed(Duration.zero);
    final reading = library.refresh();
    await Future<void>.delayed(Duration.zero);
    ack.complete(_json({}));
    await writing;
    oldRead.complete(http.Response(before, 200,
        headers: {'content-type': 'application/json'}));
    await reading;
    expect(library.playlists.single['name'], 'Committed');
    expect(library.playlists.single['revision'], 1);
  });
  test(
      'stale cloud appender refreshes and requires explicit retry without silent overwrite',
      () async {
    final server = _PlaylistServer();
    final first = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    final second = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(first.dispose);
    addTearDown(second.dispose);
    await Future.wait([first.refresh(), second.refresh()]);
    await first.addTrack('shared', {'id': 'one', 'title': 'One'});
    await expectLater(second.addTrack('shared', {'id': 'two', 'title': 'Two'}),
        throwsStateError);
    expect(second.error, contains('another device'));
    expect((second.playlists.single['tracks'] as List).single['id'], 'one');
    await second.addTrack('shared', {'id': 'two', 'title': 'Two'});
    final tracks = second.playlists.single['tracks'] as List;
    expect(tracks.map((t) => t['id']), ['one', 'two']);
    expect(tracks.map((t) => t['playlist_entry_id']), ['1', '2']);
    expect(
        server.requests.where((r) => r.url.path.endsWith('/playlist_tracks')),
        isEmpty);
  });

  test('stale rename is rejected instead of last-write-wins', () async {
    final server = _PlaylistServer();
    final first = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    final second = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(first.dispose);
    addTearDown(second.dispose);
    await Future.wait([first.refresh(), second.refresh()]);
    await first.renamePlaylist('shared', 'First edit');
    await expectLater(
        second.renamePlaylist('shared', 'Stale edit'), throwsStateError);
    expect(server.state['name'], 'First edit');
    expect(second.playlists.single['name'], 'First edit');
    expect(
        server.requests
            .every((r) => r.method != 'PATCH' && r.method != 'DELETE'),
        true);
  });
  test('recent queries use the same account-scoped cloud records as Android',
      () async {
    final library = WebLibrary(_client(MockClient((request) async {
      expect(request.url.path, '/rest/v1/search_events');
      expect(request.url.queryParameters['user_id'], 'eq.user-1');
      return _json([
        {
          'query': 'Purple Rain',
          'result_count': 3,
          'created_at': '2026-10-10T00:00:00Z'
        },
        {
          'query': 'Empty',
          'result_count': 0,
          'created_at': '2026-10-10T00:00:00Z'
        }
      ]);
    })), 'user-1');
    addTearDown(library.dispose);
    expect(await library.recentSearchQueries(), [
      {'query': 'Purple Rain', 'selected_at': '2026-10-10T00:00:00Z'}
    ]);
  });
  test('library reads overlap and publish no partial result', () async {
    final pending = <String, Completer<http.Response>>{};
    final library = WebLibrary(_client(MockClient((request) {
      final response = Completer<http.Response>();
      pending[request.url.path] = response;
      return response.future;
    })), 'user-1');
    addTearDown(library.dispose);
    final refresh = library.refresh();
    await Future<void>.delayed(Duration.zero);
    expect(pending.length, 3);
    expect(library.loading, true);
    pending['/rest/v1/library_tracks']!.complete(_json([
      {
        'track_id': 'recording:one',
        'track_data': {'title': 'One'}
      }
    ]));
    await Future<void>.delayed(Duration.zero);
    expect(library.likes, isEmpty);
    pending['/rest/v1/playlists']!.complete(_json([]));
    pending['/rest/v1/playlist_tracks']!.complete(_json([]));
    await refresh;
    expect(library.loading, false);
    expect(library.likes.single['id'], 'recording:one');
  });
  test('refresh maps only the account-scoped records into web library values',
      () async {
    final requests = <http.Request>[];
    final library = WebLibrary(
      _client(MockClient((request) async {
        requests.add(request);
        expect(request.url.queryParameters['user_id'], 'eq.user-1');
        if (request.url.path.endsWith('/library_tracks')) {
          return _json([
            {
              'track_id': 'recording:liked',
              'track_data': {
                'title': 'Liked song',
                'artist': 'Artist',
                'videoId': 'physical-liked',
              },
            }
          ]);
        }
        if (request.url.path.endsWith('/playlists')) {
          return _json([
            {'id': 'playlist-1', 'name': 'Road songs'}
          ]);
        }
        if (request.url.path.endsWith('/playlist_tracks')) {
          return _json([
            {
              'playlist_id': 'playlist-1',
              'position': 0,
              'track_id': 'recording:playlist',
              'track_data': {
                'title': 'Playlist song',
                'artist': 'Artist',
                'videoId': 'physical-playlist',
              },
            }
          ]);
        }
        fail('Unexpected request: ${request.method} ${request.url}');
      })),
      'user-1',
    );

    await library.refresh();

    expect(library.loading, isFalse);
    expect(library.error, isNull);
    expect(library.likes.single['id'], 'recording:liked');
    expect(library.playlists.single['id'], 'playlist-1');
    expect(library.playlists.single['name'], 'Road songs');
    final playlistTracks =
        library.playlists.single['tracks'] as List<Map<String, dynamic>>;
    expect(playlistTracks.single['id'], 'recording:playlist');
    expect(requests.map((request) => request.url.path), [
      '/rest/v1/library_tracks',
      '/rest/v1/playlists',
      '/rest/v1/playlist_tracks',
    ]);
    library.dispose();
  });

  test('like writes one canonical record and then reloads the same user scope',
      () async {
    final requests = <http.Request>[];
    final library = WebLibrary(
      _client(MockClient((request) async {
        requests.add(request);
        if (request.method == 'POST' &&
            request.url.path.endsWith('/library_tracks')) {
          return _json([], status: 201);
        }
        if (request.method == 'GET') return _json([]);
        fail('Unexpected request: ${request.method} ${request.url}');
      })),
      'user-1',
    );

    await library.setLiked(
      {'track_key': 'recording:one', 'title': 'One', 'artist': 'Artist'},
      true,
    );

    final write = requests.first;
    final body = jsonDecode(write.body) as Map<String, dynamic>;
    expect(write.url.queryParameters['on_conflict'], 'user_id,track_id');
    expect(body['user_id'], 'user-1');
    expect(body['track_id'], 'recording:one');
    expect((body['track_data'] as Map<String, dynamic>)['track_key'],
        'recording:one');
    expect(
      requests.skip(1).every(
            (request) => request.url.queryParameters['user_id'] == 'eq.user-1',
          ),
      isTrue,
    );
    library.dispose();
  });

  test('playlist writes are blocked before they can issue a request', () async {
    var requestCount = 0;
    final library = WebLibrary(
      _client(MockClient((request) async {
        requestCount++;
        return _json([]);
      })),
      'user-1',
    );

    for (final action in [
      () => library.createPlaylist('Road songs'),
      () => library.renamePlaylist('playlist', 'Name'),
      () => library.deletePlaylist('playlist'),
      () => library.addTrack('playlist', {'id': 'track', 'title': 'Track'}),
      () => library.removeTrack('playlist', 'track'),
    ]) {
      await expectLater(action(), throwsStateError);
    }

    expect(requestCount, 0);
    expect(library.error, contains('not enabled'));
    library.dispose();
  });

  test('missing transaction RPC fails closed without direct-write fallback',
      () async {
    final server = _PlaylistServer();
    final library = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    server.missing = true;
    await expectLater(
        library.addTrack('shared', {'id': 'one'}), throwsStateError);
    expect(library.error, contains('upgrade is not installed'));
    expect(server.state['tracks'], isEmpty);
    expect(
        server.requests
            .where((r) => r.method != 'GET')
            .every((r) => r.url.path.contains('/rpc/')),
        true);
  });

  test('stale UI revision is sent even after a newer refresh', () async {
    final server = _PlaylistServer();
    final library = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    await library.renamePlaylist('shared', 'New');
    await expectLater(
        library.renamePlaylist('shared', 'Old menu', expectedRevision: 0),
        throwsStateError);
    expect(server.state['name'], 'New');
  });

  test(
      'queued mutation is fenced on account change and captured auth stays scoped',
      () async {
    final server = _PlaylistServer();
    var current = true, token = 'account-a';
    final library = WebLibrary(_client(MockClient(server.send)), 'user-1',
        playlistWritesEnabled: true,
        accessToken: () => token,
        scopeIsCurrent: () => current);
    addTearDown(library.dispose);
    await library.refresh();
    await library.renamePlaylist('shared', 'Scoped');
    final write = server.requests.last;
    expect(write.headers['Authorization'], 'Bearer account-a');
    current = false;
    token = 'account-b';
    final count = server.requests.length;
    await expectLater(
        library.renamePlaylist('shared', 'Wrong account'), throwsStateError);
    expect(server.requests.length, count);
    expect(server.state['name'], 'Scoped');
  });

  test(
      'late account read cannot publish after disposal or alter another library',
      () async {
    final delayed = Completer<http.Response>();
    final started = Completer<void>();
    final old = WebLibrary(_client(MockClient((request) async {
      if (request.url.path.endsWith('/library_tracks')) {
        started.complete();
        return delayed.future;
      }
      return _json([]);
    })), 'account-a', playlistWritesEnabled: true);
    final next = WebLibrary(
        _client(MockClient((request) async => _json([]))), 'account-b',
        playlistWritesEnabled: true);
    addTearDown(next.dispose);
    var notifications = 0;
    old.addListener(() => notifications++);
    final reading = old.refresh();
    await started.future;
    old.dispose();
    await next.refresh();
    final before = notifications;
    delayed.complete(_json([
      {
        'track_id': 'private-a',
        'track_data': {'title': 'Account A only'}
      }
    ]));
    await reading;
    expect(notifications, before);
    expect(old.likes, isEmpty);
    expect(next.likes, isEmpty);
    expect(next.error, isNull);
  });

  test('account switch during a save rejects queued work and ignores late ack',
      () async {
    final server = _PlaylistServer();
    final delayed = Completer<http.Response>();
    final started = Completer<void>();
    var current = true;
    final library = WebLibrary(_client(MockClient((request) async {
      if (request.url.path.endsWith('/neatie_edit_playlist')) {
        final result = await server.send(request);
        started.complete();
        await delayed.future;
        return result;
      }
      return server.send(request);
    })), 'account-a',
        playlistWritesEnabled: true,
        accessToken: () => 'account-a-token',
        scopeIsCurrent: () => current);
    addTearDown(library.dispose);
    await library.refresh();
    final writing = library.renamePlaylist('shared', 'Committed for A');
    await started.future;
    final queued = library.addTrack('shared', {'id': 'queued'});
    final rejected = expectLater(queued, throwsStateError);
    current = false;
    delayed.complete(_json({}));
    await writing;
    await rejected;
    expect(library.playlists.single['name'], 'Shared');
    final writes = server.requests
        .where((request) => request.url.path.endsWith('/neatie_edit_playlist'));
    expect(writes, hasLength(1));
    expect(writes.single.headers['Authorization'], 'Bearer account-a-token');
    expect(server.state['tracks'], isEmpty);
  });

  test('failed library refresh keeps the complete previous snapshot', () async {
    final server = _PlaylistServer();
    var fail = false;
    final library = WebLibrary(_client(MockClient((request) async {
      if (fail && request.url.path.endsWith('/neatie_playlist_library')) {
        return _json({'message': 'fixture unavailable'}, status: 503);
      }
      if (request.url.path.endsWith('/library_tracks')) {
        return _json([
          {
            'track_id': fail ? 'partial' : 'saved',
            'track_data': {'title': 'Like'}
          }
        ]);
      }
      return server.send(request);
    })), 'account-a', playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    final before = jsonEncode([library.likes, library.playlists]);
    fail = true;
    await library.refresh();
    expect(jsonEncode([library.likes, library.playlists]), before);
    expect(library.loading, false);
    expect(library.error, isNotNull);
    fail = false;
    await library.refresh();
    expect(library.error, isNull);
  });

  test('definite failed append keeps snapshot and explicit retry adds once',
      () async {
    final server = _PlaylistServer();
    var fail = true;
    final library = WebLibrary(_client(MockClient((request) async {
      if (fail && request.url.path.endsWith('/neatie_edit_playlist')) {
        return _json({'message': 'fixture failure before commit'}, status: 503);
      }
      return server.send(request);
    })), 'account-a', playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    await expectLater(
        library.addTrack('shared', {'id': 'one'}), throwsA(anything));
    expect(library.playlists.single['tracks'], isEmpty);
    expect(server.state['revision'], 0);
    fail = false;
    await library.addTrack('shared', {'id': 'one'});
    expect((library.playlists.single['tracks'] as List).single['id'], 'one');
    expect(server.state['revision'], 1);
    expect(library.error, isNull);
  });

  test('lost append acknowledgement reconciles on retry without adding twice',
      () async {
    final server = _PlaylistServer();
    var loseAcknowledgement = true;
    final library = WebLibrary(_client(MockClient((request) async {
      final response = await server.send(request);
      if (loseAcknowledgement &&
          request.url.path.endsWith('/neatie_edit_playlist')) {
        loseAcknowledgement = false;
        return _json({'message': 'fixture lost commit response'}, status: 503);
      }
      return response;
    })), 'account-a', playlistWritesEnabled: true);
    addTearDown(library.dispose);
    await library.refresh();
    await expectLater(
        library.addTrack('shared', {'id': 'one'}), throwsA(anything));
    expect(server.state['tracks'], hasLength(1));
    expect(library.playlists.single['tracks'], isEmpty);
    await expectLater(
        library.addTrack('shared', {'id': 'one'}), throwsStateError);
    expect(server.state['tracks'], hasLength(1));
    expect((library.playlists.single['tracks'] as List).single['id'], 'one');
    expect(library.error, contains('another device'));
    expect(library.playlists.single['revision'], 1);
  });
}
