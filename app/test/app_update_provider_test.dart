import 'package:auralis_engine/logic/app_update_provider.dart';
import 'package:flutter_test/flutter_test.dart';

const _sha256 =
    '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';

Map<String, dynamic> _assetJson(String abi) => <String, dynamic>{
      'abi': abi,
      'name': 'app-$abi-release.apk',
      'url': 'https://github.com/example/neatie/releases/download/v1.2.3/'
          'app-$abi-release.apk',
      'sha256': _sha256,
      'sizeBytes': 1024,
    };

Map<String, dynamic> _manifestJson({
  List<Map<String, dynamic>>? assets,
}) =>
    <String, dynamic>{
      'schemaVersion': 1,
      'packageName': 'com.danbeverley.ebb',
      'versionName': '1.2.3',
      'versionCode': 12,
      'releaseNotes': 'Playback fixes',
      'mandatory': false,
      'assets': assets ??
          <Map<String, dynamic>>[
            _assetJson('arm64-v8a'),
            _assetJson('armeabi-v7a'),
            _assetJson('x86_64'),
          ],
    };

void main() {
  group('AppUpdateManifest', () {
    test('parses a strict split-APK manifest', () {
      final manifest = AppUpdateManifest.fromJson(_manifestJson());

      expect(manifest.versionName, '1.2.3');
      expect(manifest.versionCode, 12);
      expect(manifest.assets, hasLength(3));
      expect(manifest.assets.first.abi, 'arm64-v8a');
    });

    test('rejects insecure asset URLs', () {
      final asset = _assetJson('arm64-v8a')
        ..['url'] = 'http://example.com/app.apk';

      expect(
        () => AppUpdateManifest.fromJson(<String, dynamic>{
          ..._manifestJson(),
          'assets': <Map<String, dynamic>>[asset],
        }),
        throwsFormatException,
      );
    });

    test('rejects missing checksum, size, or assets', () {
      final invalidAsset = _assetJson('arm64-v8a')
        ..['sha256'] = ''
        ..['sizeBytes'] = 0;

      expect(
        () => AppUpdateManifest.fromJson(<String, dynamic>{
          ..._manifestJson(),
          'assets': <Map<String, dynamic>>[invalidAsset],
        }),
        throwsFormatException,
      );
      expect(
        () => AppUpdateManifest.fromJson(<String, dynamic>{
          ..._manifestJson(),
          'assets': const <dynamic>[],
        }),
        throwsFormatException,
      );
    });

    test('rejects the wrong schema or Android package', () {
      expect(
        () => AppUpdateManifest.fromJson(
          _manifestJson()..['schemaVersion'] = 2,
        ),
        throwsFormatException,
      );
      expect(
        () => AppUpdateManifest.fromJson(
          _manifestJson()..['packageName'] = 'com.example.impostor',
        ),
        throwsFormatException,
      );
    });
  });

  group('selectAppUpdateAsset', () {
    final assets = <AppUpdateAsset>[
      AppUpdateAsset.fromJson(_assetJson('armeabi-v7a')),
      AppUpdateAsset.fromJson(_assetJson('arm64-v8a')),
      AppUpdateAsset.fromJson(_assetJson('x86_64')),
    ];

    test('uses Android ABI preference order', () {
      final selected = selectAppUpdateAsset(
        assets,
        const <String>['arm64-v8a', 'armeabi-v7a'],
      );

      expect(selected?.abi, 'arm64-v8a');
    });

    test('returns null when a split manifest has no compatible ABI', () {
      expect(selectAppUpdateAsset(assets, const <String>['riscv64']), isNull);
    });

    test('accepts a single universal APK', () {
      final universal = AppUpdateAsset.fromJson(
        _assetJson('')..['name'] = 'neatie-universal.apk',
      );

      expect(
        selectAppUpdateAsset(
            <AppUpdateAsset>[universal], const <String>['arm64-v8a']),
        same(universal),
      );
    });
  });

  group('automatic update cadence', () {
    final now = DateTime.utc(2026, 8, 19, 12);

    test('checks when no successful timestamp exists', () {
      expect(
        isAutomaticUpdateCheckDue(
          lastSuccessfulCheckMillis: 0,
          now: now,
        ),
        isTrue,
      );
    });

    test('suppresses checks inside 24 hours and permits them afterward', () {
      expect(
        isAutomaticUpdateCheckDue(
          lastSuccessfulCheckMillis:
              now.subtract(const Duration(hours: 23)).millisecondsSinceEpoch,
          now: now,
        ),
        isFalse,
      );
      expect(
        isAutomaticUpdateCheckDue(
          lastSuccessfulCheckMillis:
              now.subtract(const Duration(hours: 24)).millisecondsSinceEpoch,
          now: now,
        ),
        isTrue,
      );
    });
  });
}
