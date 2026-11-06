import 'dart:async';
import 'dart:convert';

import 'package:auralis_engine/web/catalog_api.dart';
import 'package:auralis_engine/web/catalog_detail.dart';
import 'package:auralis_engine/web/music_api.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:auralis_engine/web/search_history.dart';
import 'web_player_test.dart' show MemoryStore;

void main() {
  test(
      'artist overview shares an in-flight fetch and caches within one account',
      () async {
    var requests = 0;
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      requests++;
      expect(request.url.queryParameters, {'view': 'overview'});
      return http.Response('{"name":"Fixture Artist"}', 200);
    }));
    addTearDown(api.client.close);
    final catalog = CatalogApi(api);
    await Future.wait([catalog.artist('same'), catalog.artist('same')]);
    await catalog.artist('same');
    expect(requests, 1);
    api.userScopeId = 'different-account';
    await catalog.artist('same');
    expect(requests, 2);
  });

  test(
      'metadata polling can refresh every visible artist within the existing search limit',
      () async {
    final bodies = <Map>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      bodies.add(jsonDecode(request.body) as Map);
      return http.Response('{}', 200);
    }));
    addTearDown(api.client.close);
    await CatalogApi(api).search('artists');
    await CatalogApi(api)
        .search('artists', limit: 48, continuation: {'revision': 2});
    await CatalogApi(api).search('artists', limit: 1000);
    expect(bodies.map((body) => body['limit']), [24, 48, 100]);
  });
  test('queue status accepts only scoped immutable cache descriptors',
      () async {
    final response = Completer<http.Response>();
    Map<String, dynamic>? body;
    final api =
        MusicApi(Uri.parse('https://music.test'), client: MockClient((request) {
      body = jsonDecode(request.body) as Map<String, dynamic>;
      return response.future;
    }));
    addTearDown(api.client.close);
    final pending = api.cacheQueue('generated', 4, [
      {'id': 'generated'}
    ]);
    await Future<void>.delayed(Duration.zero);
    expect(body?['queue_cache_metadata'], true);
    api.userScopeId = 'other-scope';
    response.complete(http.Response(
        jsonEncode({
          'prepared': {
            'generated': {
              'cache_tier': 'r2_full',
              'mime_type': 'audio/webm',
              'playback_path':
                  '/playback/stream/generated?cache_variant=opus-v1&cache_version=v1-${'a' * 32}',
              'expires_at': DateTime.now()
                      .add(const Duration(hours: 1))
                      .millisecondsSinceEpoch /
                  1000
            }
          }
        }),
        200));
    await pending;
    expect(api.preparedMedia('generated'), isNull);
  });
  test(
      'search diagnostics retain bounded stage timings and real suggestion counts',
      () async {
    final events = <Map<String, dynamic>>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async => http.Response(
            jsonEncode(request.url.path == '/suggest'
                ? {
                    'suggestions': [
                      {'text': 'One'},
                      {'text': 'Two'}
                    ],
                  }
                : {
                    'request_id': 'server-proof',
                    'tracks': [
                      {'id': 'one'}
                    ],
                    'diagnostics': {
                      'request_ms': 180,
                      'search_snapshot_hit': false,
                      'search_outcome': 'results',
                      'stage_timings_ms': {
                        'retrieval': 150,
                        'artist_surface_completion': 0,
                        'private': 'secret'
                      },
                      'retrieval': {
                        'local_index_ms': 20,
                        'timed_out_sources': ['albums.fast'],
                        'query': 'private query'
                      },
                    },
                  }),
            200)))
      ..onDiagnostic = (event, details) {
        if (event == 'search-response' || event == 'suggest-response')
          events.add(details);
      };
    addTearDown(api.client.close);
    await api.post('/search', {'result_type': 'tracks'});
    await api.post('/suggest', {});
    expect(events.first['serverRequestId'], 'server-proof');
    expect(events.first['retrievalMs'], 150);
    expect(events.first['providerTimeoutCount'], 1);
    expect(events.first['searchSurface'], 'tracks');
    expect(events.last['resultCount'], 2);
    expect(jsonEncode(events), isNot(contains('private query')));
    expect(jsonEncode(events), isNot(contains('secret')));
  });
  test(
      'cancelling search aborts its transport without cancelling playback requests',
      () async {
    final requests = <http.AbortableRequest>[];
    final responses = <Completer<http.StreamedResponse>>[];
    final aborted = <String>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: _RequestProbeClient((request) {
      final abortable = request as http.AbortableRequest;
      requests.add(abortable);
      final response = Completer<http.StreamedResponse>();
      responses.add(response);
      abortable.abortTrigger!.then((_) {
        aborted.add(request.url.path);
        if (!response.isCompleted)
          response.completeError(http.RequestAbortedException(request.url));
      });
      return response.future;
    }));
    addTearDown(api.client.close);
    final cancel = Completer<void>();
    final search = CatalogApi(api)
        .search('Older input', preview: true, cancel: cancel.future);
    final playback = api.post('/prepare_session', {});
    final failure =
        expectLater(search, throwsA(isA<http.RequestAbortedException>()));
    cancel.complete();
    await failure;
    expect(aborted, ['/search']);
    responses.last
        .complete(http.StreamedResponse(Stream.value(utf8.encode('{}')), 200));
    expect(await playback, isEmpty);
    expect(requests.length, 2);
  });
  test('Home sends bounded evidence once and strips stream/session metadata',
      () async {
    late Map body;
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      body = jsonDecode(request.body) as Map;
      return http.Response('{}', 200);
    }))
      ..homeScopeId = 'guest:web:isolated';
    addTearDown(api.client.close);
    final tracks = List.generate(
        32,
        (i) => {
              'id': 'track:$i',
              'title': 'Song $i',
              'artist': 'Artist',
              'play_count': 3,
              'last_played_at': 10,
              'canonical_recording_id': 'recording:$i',
              'extra': List.filled(1000, 'unneeded'),
              'playback': {
                'provider': 'youtube',
                'source_id': 'source:$i',
                'url': 'private-url'
              }
            });
    await CatalogApi(api).home(launchToken: 'stable-launch', signals: {
      'recent_track_snapshots': tracks,
      'last_played_tracks': tracks,
      'top_track_snapshots': tracks,
      'client_signal_tier': 'personalized'
    });
    expect(body['user_scope_id'], 'guest:web:isolated');
    expect(body['recent_track_snapshots'], hasLength(16));
    expect(body['top_track_snapshots'], hasLength(16));
    expect(body.containsKey('last_played_tracks'), false);
    expect(jsonEncode(body), isNot(contains('private-url')));
    expect(jsonEncode(body), isNot(contains('unneeded')));
    expect(body['recent_track_snapshots'][0]['canonical_recording_id'],
        'recording:0');
    expect(utf8.encode(jsonEncode(body)).length, lessThan(10000));
  });

  test(
      'timed-out requests abort and network failures have stable private-safe categories',
      () async {
    final pending = Completer<http.StreamedResponse>();
    final events = <Map<String, dynamic>>[];
    http.AbortableRequest? captured;
    var networkFailure = false;
    final api = MusicApi(Uri.parse('https://music.test'),
        client: _RequestProbeClient((request) async {
      if (networkFailure)
        throw http.ClientException('private token https://private.test');
      captured = request as http.AbortableRequest;
      return pending.future;
    }))
      ..online = (() => true)
      ..onDiagnostic = ((event, data) => events.add({'event': event, ...data}));
    addTearDown(api.client.close);
    await expectLater(
        api.get('/history_seed', timeout: const Duration(milliseconds: 1)),
        throwsA(isA<TimeoutException>()));
    expect(events.single['failureCode'], 'request_timeout');
    expect(events.single['requestId'], isNotEmpty);
    expect(events.single['online'], true);
    var aborted = false;
    captured!.abortTrigger!.then((_) => aborted = true);
    await Future<void>.delayed(Duration.zero);
    expect(aborted, true);
    pending
        .complete(http.StreamedResponse(Stream.value(utf8.encode('{}')), 200));
    networkFailure = true;
    await expectLater(
        api.get('/history_seed'), throwsA(isA<http.ClientException>()));
    expect(events.last['failureCode'], 'network_error');
    expect(events.last['errorType'], 'ClientException');
    expect(jsonEncode(events), isNot(contains('private token')));
    expect(jsonEncode(events), isNot(contains('private.test')));
  });
  test('live preview and suggestions keep the canonical scoped contracts',
      () async {
    final requests = <http.Request>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      requests.add(request);
      return http.Response(
          jsonEncode(request.url.path == '/suggest'
              ? {
                  'suggestions': [
                    {'text': 'Purple Rain', 'type': 'query'}
                  ]
                }
              : {}),
          200);
    }))
      ..userScopeId = 'account:listener';
    addTearDown(api.client.close);
    final catalog = CatalogApi(api);
    await catalog.search('  Purple Rain  ', preview: true);
    final body = jsonDecode(requests.first.body) as Map;
    expect(requests.first.url.path, '/search');
    expect(body['query'], 'Purple Rain');
    expect(body['search_preview'], true);
    expect(body['limit'], 8);
    expect(body['defer_side_surfaces'], true);
    expect(body['auto_correct'], false);
    expect(body['user_scope_id'], 'account:listener');
    final suggestions = await catalog.suggest(' Purple Ra ');
    expect(requests.last.url.path, '/suggest');
    expect(jsonDecode(requests.last.body), {
      'query': 'Purple Ra',
      'limit': 5,
      'user_scope_id': 'account:listener',
      'context_surface': 'suggest',
      'recent_queries': [],
      'recent_tracks': [],
      'force_refresh': false,
    });
    expect(suggestions.single['text'], 'Purple Rain');
  });
  test('search history survives reload, is bounded and rejects another scope',
      () {
    final store = MemoryStore();
    final history = SearchHistory('account:a', store: store);
    for (var i = 0; i < 25; i++)
      history.record('Query $i',
          at: DateTime.fromMillisecondsSinceEpoch(100000 + i));
    history.record('Picked song', track: {
      'id': 'recording:pick',
      'title': 'Picked',
      'playback': {
        'provider': 'youtube',
        'source_id': 'source',
        'url': 'private-url',
        'headers': {'token': 'secret'}
      }
    });
    history.record('Picked song');
    final restored = SearchHistory('account:a', store: store);
    expect(restored.entries.length, 20);
    expect(restored.entries.first['track']['title'], 'Picked');
    expect(store.value, isNot(contains('private-url')));
    expect(store.value, isNot(contains('secret')));
    expect(SearchHistory('account:b', store: store).entries, isEmpty);
    restored.merge([
      {'query': 'Cloud selected', 'selected_at': 300},
      {
        'query': 'Cloud selected',
        'selected_at': 299,
        'track': {'id': 'recording:cloud', 'title': 'Cloud pick'}
      }
    ]);
    expect(restored.entries.first['track']['title'], 'Picked');
    expect(
        restored.entries
            .where((entry) => entry['query'] == 'Cloud selected')
            .single['track']['title'],
        'Cloud pick');
  });
  test('removed and cleared recent searches stay hidden after cloud refresh',
      () {
    final store = MemoryStore();
    final history = SearchHistory('a', store: store);
    final old = DateTime.fromMillisecondsSinceEpoch(100000);
    history.record('Cloud query', at: old);
    history.remove('Cloud query');
    final restored = SearchHistory('a', store: store);
    restored.merge([
      {'query': 'Cloud query', 'selected_at': 101}
    ]);
    expect(restored.entries, isEmpty);
    restored.record('New query', at: old);
    restored.clear(at: DateTime.fromMillisecondsSinceEpoch(200000));
    restored.merge([
      {'query': 'New query', 'selected_at': 100}
    ]);
    expect(restored.entries, isEmpty);
    restored.record('New query',
        at: DateTime.fromMillisecondsSinceEpoch(300000));
    expect(restored.queries, ['New query']);
  });
  test('lyrics preserves canonical identity and Android metadata hints',
      () async {
    late http.Request request;
    final events = <Map<String, dynamic>>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((r) async {
      request = r;
      return http.Response('{"has_lyrics":true,"lines":[]}', 200);
    }))
      ..onDiagnostic = ((event, data) => events.add({'event': event, ...data}));
    addTearDown(api.client.close);
    await CatalogApi(api).lyrics({
      'id': 'recording:a:b',
      'title': 'Song',
      'artist': 'Artist',
      'album': 'Album',
      'duration': 125,
      'playback': {'provider': 'youtube', 'source_id': 'source'}
    }, refresh: true);
    expect(Uri.decodeComponent(request.url.path), '/lyrics/recording:a:b');
    expect(request.url.toString(), isNot(contains('%253A')));
    expect(request.url.queryParameters, {
      'title': 'Song',
      'artist': 'Artist',
      'album': 'Album',
      'duration': '125',
      'source_id': 'source',
      'force_refresh': 'true'
    });
    api.userScopeId = 'account:private-listener';
    await CatalogApi(api).recentSearchPicks();
    expect(events.map((e) => e['path']), ['/lyrics/*', '/recent_search_picks']);
    for (final private in [
      'recording:a',
      'Song',
      'Artist',
      'source',
      'private-listener'
    ]) {
      expect(jsonEncode(events), isNot(contains(private)));
    }
  });
  test(
      'preparation reports fixed failures and server-confirmed cache tiers only',
      () async {
    final events = <Map<String, dynamic>>[];
    var failed = true;
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response(
            jsonEncode(failed
                ? {
                    'prepared': {},
                    'failed_ids': ['track'],
                    'failed': {
                      'track': {
                        'code': 'source_blocked',
                        'diagnostics': {
                          'stage': 'media_preflight',
                          'exception_type': 'HTTPError',
                          'http_status': 403
                        },
                        'message': 'private provider response'
                      }
                    }
                  }
                : {
                    'prepared': {
                      'track': {'cache_tier': 'r2_full'}
                    },
                    'failed_ids': []
                  }),
            200)))
      ..onDiagnostic = ((event, data) => events.add({'event': event, ...data}));
    await expectLater(api.prepare({'id': 'track'}), throwsStateError);
    expect(events.last['failureCode'], 'source_blocked');
    expect(events.last['failureStage'], 'media_preflight');
    expect(events.last['failureType'], 'HTTPError');
    expect(events.last['failureHttpStatus'], 403);
    expect(events.last['cacheTier'], 'unprepared');
    failed = false;
    await api.prepare({'id': 'track'});
    expect(events.last['cacheTier'], 'r2_full');
    expect(events.last['fullyCached'], isTrue);
    expect(jsonEncode(events), isNot(contains('private provider')));
  });
  test('detail timings omit entity IDs and retain HTTP failures', () async {
    final events = <Map<String, dynamic>>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((request) async {
      expect(request.headers['Authorization'], 'Bearer private-token');
      return http.Response(
          request.url.path.startsWith('/artist/') ? '{}' : 'failed',
          request.url.path.startsWith('/artist/') ? 200 : 503);
    }))
      ..accessToken = (() => 'private-token')
      ..onDiagnostic = ((event, data) => events.add({'event': event, ...data}));
    await CatalogApi(api).artist('private-artist-id');
    await expectLater(
        CatalogApi(api).album('private-album-id'), throwsStateError);
    expect(events.map((r) => r['path']), ['/artist/*', '/album/*']);
    expect(
        events.every((r) => r['requestPurpose'] == 'detail' && r['ms'] is int),
        true);
    expect(events.last['status'], 503);
    expect(jsonEncode(events), isNot(contains('private')));
  });

  test('request purposes distinguish Home preparation, polling and normal feed',
      () async {
    final purposes = <String>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) async => http.Response('{}', 200)))
      ..onDiagnostic = ((event, data) {
        if (event == 'api-response')
          purposes.add(data['requestPurpose'] as String);
      });
    final catalog = CatalogApi(api);
    await catalog.home();
    await catalog.home(prepare: true);
    await catalog.homeQueueStatus();
    expect(purposes, ['full_feed', 'background_prepare', 'queue_status']);
  });

  test('late detail diagnostics cannot enter another account', () async {
    final pending = Completer<http.Response>();
    final events = <String>[];
    final api = MusicApi(Uri.parse('https://music.test'),
        client: MockClient((_) => pending.future))
      ..userScopeId = 'first'
      ..onDiagnostic = ((event, _) => events.add(event));
    final result = CatalogApi(api).artist('one');
    api.userScopeId = 'second';
    pending.complete(http.Response('{}', 200));
    await result;
    expect(events, isEmpty);
  });
  group('CatalogApi', () {
    test(
        'launch, preparation and listening events use existing scoped contracts',
        () async {
      final bodies = <Map<String, dynamic>>[];
      final paths = <String>[];
      final api = MusicApi(Uri.parse('https://music.test'),
          client: MockClient((request) async {
        paths.add(request.url.path);
        bodies.add(Map<String, dynamic>.from(jsonDecode(request.body) as Map));
        return http.Response('{"stored":true}', 200);
      }))
        ..userScopeId = 'account:a';
      addTearDown(api.client.close);
      final catalog = CatalogApi(api);
      await catalog.home(launchToken: 'launch:one');
      await catalog.home(prepare: true, launchToken: 'ignored');
      await catalog.history();
      await catalog.recordPlay({'id': 'track:a', 'title': 'Work'},
          scope: 'account:a', occurredAt: 123);
      await catalog
          .recordPlay({'id': 'track:b'}, scope: 'account:b', occurredAt: 124);
      expect(bodies[0]['promote_ready_on_launch'], true);
      expect(bodies[0]['launch_token'], 'launch:one');
      expect(bodies[1]['prepare_next_session'], true);
      expect(bodies[1].containsKey('promote_ready_on_launch'), false);
      expect(bodies[1].containsKey('launch_token'), false);
      expect(paths,
          ['/recommend', '/recommend', '/history_seed', '/interaction_event']);
      expect(
          bodies.every((body) => body['user_scope_id'] == 'account:a'), true);
      expect(bodies.last['event_type'], 'play');
    });
    test('uses feed-only Home scope while keeping Search account scope',
        () async {
      final requests = <http.Request>[];
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          requests.add(request);
          return http.Response('{}', 200);
        }),
      )..userScopeId = 'guest';
      api.homeScopeId = 'guest:web:12345678-1234-4234-9234-123456789abc';
      addTearDown(api.client.close);

      final catalog = CatalogApi(api);
      await catalog.home();
      await catalog.homeQueueStatus();
      await catalog.homeRow('session', 'row', 20);
      await catalog.search('track');

      expect(jsonDecode(requests[0].body)['user_scope_id'], api.homeScopeId);
      expect(jsonDecode(requests[1].body)['user_scope_id'], api.homeScopeId);
      expect(jsonDecode(requests[2].body)['user_scope_id'], api.homeScopeId);
      expect(jsonDecode(requests[3].body)['user_scope_id'], 'guest');

      api.userScopeId = 'account-1';
      // A stale guest override can never take precedence over an account.
      await catalog.home();
      expect(jsonDecode(requests[4].body)['user_scope_id'], 'account-1');
    });

    test('catalog detail is available to catalog navigation', () {
      expect(CatalogDetail, isNotNull);
    });

    test('search preserves entity payloads and sends surface continuation',
        () async {
      late http.Request request;
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((next) async {
          request = next;
          return http.Response(
            jsonEncode({
              'tracks': [
                {
                  'id': 'recording:one',
                  'title': 'One',
                  'channel': 'Artist',
                  'canonical_evidence': {'recording': true},
                }
              ],
              'top_result': {
                'entity_type': 'track',
                'item': {
                  'id': 'recording:top',
                  'title': 'Top',
                  'channel': 'Artist',
                },
              },
              'lead_artist': {'id': 'artist:lead', 'name': 'Artist'},
              'artists': [
                {'id': 'artist:one', 'name': 'Artist', 'detail_ready': false}
              ],
              'albums': [
                {'id': 'album:one', 'title': 'Album', 'playable': false}
              ],
              'query_correction': {'query': 'one'},
              'pagination': {
                'albums': {'next_offset': 12, 'has_more': true}
              },
            }),
            200,
          );
        }),
      );
      addTearDown(api.client.close);

      final result = await CatalogApi(api).search(
        'one',
        autoCorrect: false,
        offset: 8,
        continuation: {'result_type': 'albums', 'cursor': 'snapshot:one'},
      );

      expect(request.url.path, '/search');
      expect(jsonDecode(request.body), {
        'result_type': 'albums',
        'cursor': 'snapshot:one',
        'query': 'one',
        'auto_correct': false,
        'search_mode': 'exact',
        'limit': 24,
        'user_scope_id': api.userScopeId,
        'context_surface': 'submitted_search',
        'defer_side_surfaces': false,
        'offset': 8,
      });
      expect(result['artists'], [
        {'id': 'artist:one', 'name': 'Artist', 'detail_ready': false}
      ]);
      expect(result['albums'], [
        {'id': 'album:one', 'title': 'Album', 'playable': false}
      ]);
      expect((result['tracks'] as List).single['canonical_evidence'],
          {'recording': true});
      expect((result['tracks'] as List).single['track_key'], 'recording:one');
      expect((result['top_result'] as Map)['entity_type'], 'track');
      expect(((result['top_result'] as Map)['item'] as Map)['track_key'],
          'recording:top');
      expect(result['lead_artist'], {'id': 'artist:lead', 'name': 'Artist'});
      expect(result['pagination'], {
        'albums': {'next_offset': 12, 'has_more': true}
      });
    });

    test('search mode follows canonical query classifier', () async {
      final requests = <Map<String, dynamic>>[];
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          requests
              .add(Map<String, dynamic>.from(jsonDecode(request.body) as Map));
          return http.Response('{}', 200);
        }),
      );
      addTearDown(api.client.close);

      await CatalogApi(api).search('  artist Radiohead  ');
      await CatalogApi(api).search('songs like dreamy indie pop');

      expect(requests.map((body) => body['search_mode']), ['entity', 'taste']);
      expect(requests.first['query'], 'artist Radiohead');
    });

    test(
        'selection feedback sends one scoped minimal event with original query',
        () async {
      final requests = <http.Request>[];
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          requests.add(request);
          return http.Response('{"status":"success"}', 200);
        }),
      )..userScopeId = 'user-1';
      addTearDown(api.client.close);

      api.setManualSearchContext('  everlong live  ');
      await api.recordExplicitSearchSelection({
        'id': 'video-1',
        'title': 'Everlong',
        'channel': 'Foo Fighters',
        'canonical_recording_id': 'mbid-1',
        'thumbnail': 'omit-me',
      });

      expect(requests, hasLength(1));
      expect(requests.single.url.path, '/search_interaction');
      expect(jsonDecode(requests.single.body), {
        'query': 'everlong live',
        'user_scope_id': 'user-1',
        'source': 'web_search_selection',
        'result_count': 0,
        'metadata': {
          'selected_entity_type': 'track',
          'selected_item': {
            'id': 'video-1',
            'canonical_recording_id': 'mbid-1',
            'title': 'Everlong',
            'artist': 'Foo Fighters',
          },
        },
      });
    });

    test('details use encoded canonical routes and retain raw payload',
        () async {
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.path, '/album/release%2Fone');
          return http.Response(
            jsonEncode({'id': 'release/one', 'tracks': [], 'ready': false}),
            200,
          );
        }),
      );
      addTearDown(api.client.close);

      expect(await CatalogApi(api).album('release/one'), {
        'id': 'release/one',
        'tracks': [],
        'ready': false,
      });
    });

    test('artist uses its canonical detail route', () async {
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.path, '/artist/artist%3Aone');
          return http.Response(
            jsonEncode({'id': 'artist:one', 'name': 'Artist'}),
            200,
          );
        }),
      );
      addTearDown(api.client.close);

      expect(await CatalogApi(api).artist('artist:one'), {
        'id': 'artist:one',
        'name': 'Artist',
      });
    });

    test('home delegates refresh and leaves preparing state intact', () async {
      late Map<String, dynamic> body;
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          body = Map<String, dynamic>.from(jsonDecode(request.body) as Map);
          return http.Response(
            jsonEncode({
              'status': 'success',
              'feed_action': 'preparing_initial',
              'rows': [],
              'diagnostics': {'artifact_status': 'preparing'},
            }),
            200,
          );
        }),
      );
      addTearDown(api.client.close);

      final result = await CatalogApi(api).home(refresh: true, signals: {
        'recent_track_ids': ['track:one'],
        'untrusted_extra': true,
      });

      expect(body, {
        'query': '',
        'user_scope_id': api.userScopeId,
        'force_refresh': true,
        'feed_refresh_wait_ms': 8000,
        'recent_track_ids': ['track:one'],
      });
      expect(result['feed_action'], 'preparing_initial');
      expect(result['rows'], isEmpty);
      expect(result['diagnostics'], {'artifact_status': 'preparing'});
    });

    test('home queue status is passive and clamps negative revision', () async {
      late Map<String, dynamic> body;
      final api = MusicApi(
        Uri.parse('https://music.test'),
        client: MockClient((request) async {
          body = Map<String, dynamic>.from(jsonDecode(request.body) as Map);
          return http.Response(
              jsonEncode({
                'diagnostics': {'queue_revision': 5}
              }),
              200);
        }),
      );
      addTearDown(api.client.close);

      expect(
          (await CatalogApi(api).homeQueueStatus(revision: -4))['diagnostics'],
          {'queue_revision': 5});
      expect(body, {
        'query': '',
        'limit': 0,
        'offset': 0,
        'user_scope_id': api.userScopeId,
        'feed_queue_status_only': true,
        'feed_queue_revision': 0,
        'feed_queue_wait_ms': 9000,
      });
    });
  });
}

class _RequestProbeClient extends http.BaseClient {
  _RequestProbeClient(this.handler);
  final Future<http.StreamedResponse> Function(http.BaseRequest) handler;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) =>
      handler(request);
}
