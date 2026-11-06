import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:auralis_engine/web/chart_region.dart';
import 'web_player_test.dart' show MemoryStore;

void main() {
  test('country lookup uses standalone unauthenticated request and discards IP',
      () async {
    final requests = <http.Request>[];
    final client = MockClient((request) async {
      requests.add(request);
      return http.Response('{"ip":"private-fixture-ip","country":"jp"}', 200);
    });
    addTearDown(client.close);
    expect(await detectChartCountry(client: client), 'JP');
    expect(requests.single.url.toString(), 'https://api.country.is/');
    expect(requests.single.headers.containsKey('authorization'), false);
    expect(requests.single.headers.containsKey('cookie'), false);
  });
  test('manual region survives reload and skips automatic lookup', () async {
    final store = MemoryStore();
    var calls = 0;
    final region = ChartRegion(
        store: store,
        detect: () async {
          calls++;
          return 'US';
        });
    await region.choose('JP');
    region.dispose();
    final restored = ChartRegion(
        store: store,
        detect: () async {
          calls++;
          return 'US';
        });
    await restored.initialize();
    expect(restored.country, 'JP');
    expect(restored.selection, 'JP');
    expect(calls, 0);
    expect(jsonDecode(store.value!)['country'], 'JP');
    restored.dispose();
  });
  test('late automatic response cannot undo an explicit choice', () async {
    final reply = Completer<String?>();
    final region = ChartRegion(detect: () => reply.future);
    final initial = region.initialize();
    expect(region.detecting, true);
    await region.choose('GB');
    reply.complete('VN');
    await initial;
    expect(region.country, 'GB');
    expect(region.selection, 'GB');
    region.dispose();
  });
  test('automatic mode reruns once per session and permits later override',
      () async {
    var calls = 0;
    final store = MemoryStore();
    final region = ChartRegion(
        store: store,
        detect: () async {
          calls++;
          return 'DE';
        });
    await region.initialize();
    await region.initialize();
    expect(region.country, 'DE');
    expect(calls, 1);
    await region.choose('US');
    await region.choose('AUTO');
    expect(region.country, 'DE');
    expect(calls, 2);
    expect(jsonDecode(store.value!)['selection'], 'AUTO');
    region.dispose();
  });
  test('unsupported and failed detection use Global without a wrong region',
      () async {
    for (final code in ['XX', null]) {
      final region = ChartRegion(detect: () async => code);
      await region.initialize();
      expect(region.country, 'GLOBAL');
      expect(region.detecting, false);
      expect(region.outcome, code == null ? 'unavailable' : 'unsupported');
      region.dispose();
    }
  });
  test('blocked storage and malformed country responses do not break browsing',
      () async {
    for (final body in [
      'invalid',
      '{"country":"bad"}',
      '[]',
      '"${'x' * 5000}"'
    ]) {
      final client = MockClient((_) async => http.Response(body, 200));
      expect(await detectChartCountry(client: client), null);
      client.close();
    }
    final region =
        ChartRegion(store: _BlockedStore(), detect: () async => 'AU');
    await region.initialize();
    await region.choose('CA');
    expect(region.country, 'CA');
    expect(region.description, contains('storage unavailable'));
    region.dispose();
  });
  test('disposed settings ignore late detection and invalid saved values',
      () async {
    final store = MemoryStore()
      ..value = '{"version":1,"selection":"ZZ","country":"ZZ"}';
    final reply = Completer<String?>();
    final region = ChartRegion(store: store, detect: () => reply.future);
    expect(region.country, 'GLOBAL');
    final future = region.initialize();
    region.dispose();
    reply.complete('US');
    await future;
    expect(region.country, 'GLOBAL');
  });
  testWidgets('unresponsive automatic lookup has a bounded wait',
      (tester) async {
    final reply = Completer<String?>();
    final region = ChartRegion(detect: () => reply.future);
    final pending = region.initialize();
    await tester.pump(const Duration(seconds: 5));
    await pending;
    expect(region.detecting, false);
    expect(region.country, 'GLOBAL');
    reply.complete('JP');
    await tester.pump();
    expect(region.country, 'GLOBAL');
    region.dispose();
  });
}

class _BlockedStore extends MemoryStore {
  @override
  String? read() => throw StateError('Storage blocked');
  @override
  void write(String value) => throw StateError('Storage blocked');
}
