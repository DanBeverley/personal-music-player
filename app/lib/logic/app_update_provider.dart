import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

const String appUpdateManifestUrl = String.fromEnvironment(
  'APP_UPDATE_MANIFEST_URL',
  defaultValue:
      'https://github.com/DanBeverley/personal-music-player/releases/latest/download/update.json',
);

const int appUpdateManifestSchemaVersion = 1;
const String appUpdatePackageName = 'com.danbeverley.ebb';
const int _maximumManifestBytes = 256 * 1024;
const Duration automaticUpdateCheckInterval = Duration(hours: 24);
const String _lastSuccessfulCheckKey = 'app_update.last_successful_check_ms';
const String _ignoredVersionCodeKey = 'app_update.ignored_version_code';
const MethodChannel _appUpdateChannel = MethodChannel('ebb/app_update');

enum AppUpdatePhase {
  idle,
  checking,
  available,
  upToDate,
  downloading,
  permissionRequired,
  readyToInstall,
  installing,
  failed,
}

class AppUpdateAsset {
  const AppUpdateAsset({
    required this.abi,
    required this.url,
    required this.sha256,
    required this.sizeBytes,
    required this.name,
  });

  final String abi;
  final String url;
  final String sha256;
  final int sizeBytes;
  final String name;

  factory AppUpdateAsset.fromJson(Map<String, dynamic> json) {
    final url = (json['url'] ?? json['apkUrl'] ?? '').toString().trim();
    final name = (json['name'] ?? Uri.tryParse(url)?.pathSegments.last ?? '')
        .toString()
        .trim();
    return AppUpdateAsset(
      abi: (json['abi'] ?? '').toString().trim(),
      url: url,
      sha256: (json['sha256'] ?? '').toString().trim().toLowerCase(),
      sizeBytes: (json['sizeBytes'] as num?)?.toInt() ?? 0,
      name: name.isEmpty ? 'neatie-update.apk' : name,
    );
  }

  void validate() {
    final parsedUrl = Uri.tryParse(url);
    if (parsedUrl == null ||
        parsedUrl.scheme != 'https' ||
        parsedUrl.host.isEmpty) {
      throw const FormatException('Update APK URL must use HTTPS.');
    }
    if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(sha256)) {
      throw const FormatException('Update APK SHA-256 is invalid.');
    }
    if (sizeBytes <= 0) {
      throw const FormatException('Update APK size must be greater than zero.');
    }
    if (!name.toLowerCase().endsWith('.apk')) {
      throw const FormatException('Update asset is not an APK.');
    }
  }
}

class AppUpdateManifest {
  const AppUpdateManifest({
    required this.schemaVersion,
    required this.packageName,
    required this.versionName,
    required this.versionCode,
    required this.releaseNotes,
    required this.mandatory,
    required this.assets,
  });

  final int schemaVersion;
  final String packageName;
  final String versionName;
  final int versionCode;
  final String releaseNotes;
  final bool mandatory;
  final List<AppUpdateAsset> assets;

  factory AppUpdateManifest.fromJson(Map<String, dynamic> json) {
    final rawAssets = json['assets'];
    final parsedAssets = <AppUpdateAsset>[];
    if (rawAssets is List) {
      for (final value in rawAssets) {
        if (value is Map) {
          parsedAssets.add(
            AppUpdateAsset.fromJson(Map<String, dynamic>.from(value)),
          );
        }
      }
    } else if ((json['apkUrl'] ?? '').toString().trim().isNotEmpty) {
      parsedAssets.add(AppUpdateAsset.fromJson(json));
    }

    final manifest = AppUpdateManifest(
      schemaVersion: (json['schemaVersion'] as num?)?.toInt() ?? 0,
      packageName: (json['packageName'] ?? '').toString().trim(),
      versionName: (json['versionName'] ?? '').toString().trim(),
      versionCode: (json['versionCode'] as num?)?.toInt() ?? 0,
      releaseNotes: (json['releaseNotes'] ?? '').toString().trim(),
      mandatory: json['mandatory'] == true,
      assets: List<AppUpdateAsset>.unmodifiable(parsedAssets),
    );
    manifest.validate();
    return manifest;
  }

  void validate() {
    if (schemaVersion != appUpdateManifestSchemaVersion) {
      throw const FormatException('Update manifest schema is unsupported.');
    }
    if (packageName != appUpdatePackageName) {
      throw const FormatException('Update manifest package is invalid.');
    }
    if (versionName.isEmpty || versionCode <= 0) {
      throw const FormatException('Update manifest version is invalid.');
    }
    if (assets.isEmpty) {
      throw const FormatException('Update manifest has no APK assets.');
    }
    for (final asset in assets) {
      asset.validate();
    }
  }
}

class AppVersionInfo {
  const AppVersionInfo({
    required this.versionName,
    required this.versionCode,
    required this.supportedAbis,
  });

  final String versionName;
  final int versionCode;
  final List<String> supportedAbis;
}

AppUpdateAsset? selectAppUpdateAsset(
  List<AppUpdateAsset> assets,
  List<String> supportedAbis,
) {
  if (assets.isEmpty) return null;
  for (final rawAbi in supportedAbis) {
    final abi = rawAbi.trim().toLowerCase();
    for (final asset in assets) {
      final assetAbi = asset.abi.trim().toLowerCase();
      final assetName = asset.name.toLowerCase();
      if (assetAbi == abi || assetName.contains(abi)) {
        return asset;
      }
    }
  }
  return assets.length == 1 ? assets.first : null;
}

bool isAutomaticUpdateCheckDue({
  required int lastSuccessfulCheckMillis,
  required DateTime now,
  Duration interval = automaticUpdateCheckInterval,
}) {
  if (lastSuccessfulCheckMillis <= 0) return true;
  final lastCheck = DateTime.fromMillisecondsSinceEpoch(
    lastSuccessfulCheckMillis,
    isUtc: true,
  );
  return now.toUtc().difference(lastCheck) >= interval;
}

class AppUpdateState {
  const AppUpdateState({
    this.phase = AppUpdatePhase.idle,
    this.currentVersionName = '',
    this.currentVersionCode = 0,
    this.manifest,
    this.selectedAsset,
    this.downloadProgress = 0,
    this.downloadedApkPath = '',
    this.error = '',
  });

  final AppUpdatePhase phase;
  final String currentVersionName;
  final int currentVersionCode;
  final AppUpdateManifest? manifest;
  final AppUpdateAsset? selectedAsset;
  final double downloadProgress;
  final String downloadedApkPath;
  final String error;

  bool get hasUpdate => manifest != null && selectedAsset != null;
  bool get isBusy =>
      phase == AppUpdatePhase.checking ||
      phase == AppUpdatePhase.downloading ||
      phase == AppUpdatePhase.installing;

  AppUpdateState copyWith({
    AppUpdatePhase? phase,
    String? currentVersionName,
    int? currentVersionCode,
    AppUpdateManifest? manifest,
    bool clearManifest = false,
    AppUpdateAsset? selectedAsset,
    bool clearSelectedAsset = false,
    double? downloadProgress,
    String? downloadedApkPath,
    String? error,
  }) {
    return AppUpdateState(
      phase: phase ?? this.phase,
      currentVersionName: currentVersionName ?? this.currentVersionName,
      currentVersionCode: currentVersionCode ?? this.currentVersionCode,
      manifest: clearManifest ? null : manifest ?? this.manifest,
      selectedAsset:
          clearSelectedAsset ? null : selectedAsset ?? this.selectedAsset,
      downloadProgress: downloadProgress ?? this.downloadProgress,
      downloadedApkPath: downloadedApkPath ?? this.downloadedApkPath,
      error: error ?? this.error,
    );
  }
}

class AppUpdateController extends StateNotifier<AppUpdateState> {
  AppUpdateController({http.Client? client})
      : _client = client ?? http.Client(),
        super(const AppUpdateState());

  final http.Client _client;

  Future<void> checkForUpdate({bool automatic = false}) async {
    if (automatic && !Platform.isAndroid) return;
    if (state.isBusy) return;

    late final SharedPreferences preferences;
    try {
      preferences = await SharedPreferences.getInstance();
    } catch (error) {
      if (!automatic) {
        state = state.copyWith(
          phase: AppUpdatePhase.failed,
          error: 'Update preferences are unavailable: $error',
        );
      }
      return;
    }
    if (automatic &&
        !isAutomaticUpdateCheckDue(
          lastSuccessfulCheckMillis:
              preferences.getInt(_lastSuccessfulCheckKey) ?? 0,
          now: DateTime.now(),
        )) {
      return;
    }

    state = state.copyWith(
      phase: AppUpdatePhase.checking,
      error: '',
      downloadProgress: 0,
      downloadedApkPath: '',
      clearManifest: true,
      clearSelectedAsset: true,
    );
    try {
      final versionInfo = await _loadVersionInfo();
      final manifest = await _fetchManifest();
      await preferences.setInt(
        _lastSuccessfulCheckKey,
        DateTime.now().toUtc().millisecondsSinceEpoch,
      );
      final selectedAsset = selectAppUpdateAsset(
        manifest.assets,
        versionInfo.supportedAbis,
      );
      if (manifest.versionCode <= versionInfo.versionCode) {
        state = state.copyWith(
          phase: AppUpdatePhase.upToDate,
          currentVersionName: versionInfo.versionName,
          currentVersionCode: versionInfo.versionCode,
          manifest: manifest,
          clearSelectedAsset: true,
        );
        return;
      }
      if (automatic &&
          preferences.getInt(_ignoredVersionCodeKey) == manifest.versionCode) {
        state = state.copyWith(
          phase: AppUpdatePhase.idle,
          currentVersionName: versionInfo.versionName,
          currentVersionCode: versionInfo.versionCode,
          clearManifest: true,
          clearSelectedAsset: true,
        );
        return;
      }
      if (selectedAsset == null) {
        throw StateError(
          'Update ${manifest.versionName} is available, but no APK matches this device ABI.',
        );
      }
      state = state.copyWith(
        phase: AppUpdatePhase.available,
        currentVersionName: versionInfo.versionName,
        currentVersionCode: versionInfo.versionCode,
        manifest: manifest,
        selectedAsset: selectedAsset,
      );
    } catch (error) {
      state = state.copyWith(
        phase: automatic ? AppUpdatePhase.idle : AppUpdatePhase.failed,
        error: automatic ? '' : error.toString(),
        clearManifest: true,
        clearSelectedAsset: true,
      );
    }
  }

  Future<void> dismissAvailableUpdate() async {
    final versionCode = state.manifest?.versionCode ?? 0;
    if (versionCode > 0) {
      try {
        final preferences = await SharedPreferences.getInstance();
        await preferences.setInt(_ignoredVersionCodeKey, versionCode);
      } catch (_) {
        // Dismissing the banner must still work if preferences are unavailable.
      }
    }
    state = state.copyWith(
      phase: AppUpdatePhase.idle,
      clearManifest: true,
      clearSelectedAsset: true,
      downloadProgress: 0,
      downloadedApkPath: '',
      error: '',
    );
  }

  Future<void> downloadAndInstall() async {
    if (state.manifest == null || state.selectedAsset == null) {
      await checkForUpdate();
      if (!state.hasUpdate) return;
    }
    final activeAsset = state.selectedAsset;
    if (activeAsset == null) return;
    try {
      state = state.copyWith(
        phase: AppUpdatePhase.downloading,
        downloadProgress: 0,
        error: '',
      );
      final apkPath = await _downloadAndVerifyAsset(activeAsset);
      final canInstall = await _canRequestPackageInstalls();
      if (!canInstall) {
        state = state.copyWith(
          phase: AppUpdatePhase.permissionRequired,
          downloadedApkPath: apkPath,
          downloadProgress: 1,
        );
        await _appUpdateChannel.invokeMethod<void>(
          'openInstallPermissionSettings',
        );
        return;
      }
      await _launchInstaller(apkPath);
    } catch (error) {
      state = state.copyWith(
        phase: AppUpdatePhase.failed,
        error: error.toString(),
      );
    }
  }

  Future<void> retryInstallAfterPermission() async {
    final apkPath = state.downloadedApkPath;
    final asset = state.selectedAsset;
    if (apkPath.trim().isEmpty || asset == null) {
      await downloadAndInstall();
      return;
    }
    try {
      final canInstall = await _canRequestPackageInstalls();
      if (!canInstall) {
        await _appUpdateChannel.invokeMethod<void>(
          'openInstallPermissionSettings',
        );
        return;
      }
      await _validateDownloadedApk(File(apkPath), asset);
      await _launchInstaller(apkPath);
    } catch (error) {
      state = state.copyWith(
        phase: AppUpdatePhase.failed,
        error: error.toString(),
      );
    }
  }

  Future<void> resumeInstallAfterPermission() async {
    if (state.phase != AppUpdatePhase.permissionRequired) return;
    try {
      if (!await _canRequestPackageInstalls()) return;
      await retryInstallAfterPermission();
    } catch (_) {
      // Returning from Android settings must never interrupt app resume.
    }
  }

  Future<void> _launchInstaller(String apkPath) async {
    state = state.copyWith(
      phase: AppUpdatePhase.installing,
      downloadedApkPath: apkPath,
      downloadProgress: 1,
      error: '',
    );
    final launched = await _appUpdateChannel.invokeMethod<bool>(
          'installApk',
          {'path': apkPath},
        ) ??
        false;
    if (!launched) {
      throw StateError('Android package installer could not be opened.');
    }
    state = state.copyWith(phase: AppUpdatePhase.readyToInstall);
  }

  Future<AppVersionInfo> _loadVersionInfo() async {
    final rawVersion = await _appUpdateChannel.invokeMapMethod<String, dynamic>(
      'getVersionInfo',
    );
    final rawAbis = await _appUpdateChannel.invokeListMethod<dynamic>(
      'getSupportedAbis',
    );
    return AppVersionInfo(
      versionName: (rawVersion?['versionName'] ?? '').toString(),
      versionCode: (rawVersion?['versionCode'] as num?)?.toInt() ?? 0,
      supportedAbis: (rawAbis ?? const <dynamic>[])
          .map((value) => value.toString().trim())
          .where((value) => value.isNotEmpty)
          .toList(growable: false),
    );
  }

  Future<AppUpdateManifest> _fetchManifest() async {
    final response = await _client
        .get(Uri.parse(appUpdateManifestUrl))
        .timeout(const Duration(seconds: 12));
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError('Update check failed: HTTP ${response.statusCode}.');
    }
    if (response.bodyBytes.length > _maximumManifestBytes) {
      throw const FormatException('Update manifest is too large.');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Update manifest is not a JSON object.');
    }
    return AppUpdateManifest.fromJson(Map<String, dynamic>.from(decoded));
  }

  Future<String> _downloadAndVerifyAsset(AppUpdateAsset asset) async {
    final directory = await getTemporaryDirectory();
    final updatesDir = Directory('${directory.path}/updates');
    await updatesDir.create(recursive: true);
    await _deleteStaleDownloads(updatesDir);

    final safeName = asset.name.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    final finalFile = File('${updatesDir.path}/$safeName');
    final partialFile = File('${finalFile.path}.part');
    if (partialFile.existsSync()) await partialFile.delete();

    final request = http.Request('GET', Uri.parse(asset.url));
    final response = await _client.send(request).timeout(
          const Duration(seconds: 20),
        );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError('APK download failed: HTTP ${response.statusCode}.');
    }
    final responseLength = response.contentLength ?? 0;
    if (responseLength > 0 && responseLength != asset.sizeBytes) {
      throw StateError('APK download size does not match the manifest.');
    }

    var received = 0;
    final sink = partialFile.openWrite();
    try {
      await for (final chunk
          in response.stream.timeout(const Duration(seconds: 30))) {
        received += chunk.length;
        if (received > asset.sizeBytes) {
          throw StateError('APK download exceeded the manifest size.');
        }
        sink.add(chunk);
        state = state.copyWith(
          downloadProgress: (received / asset.sizeBytes).clamp(0, 1).toDouble(),
        );
      }
      await sink.flush();
    } catch (_) {
      await sink.close();
      if (partialFile.existsSync()) await partialFile.delete();
      rethrow;
    }
    await sink.close();

    try {
      await _validateDownloadedApk(partialFile, asset);
      if (finalFile.existsSync()) await finalFile.delete();
      return (await partialFile.rename(finalFile.path)).path;
    } catch (_) {
      if (partialFile.existsSync()) await partialFile.delete();
      rethrow;
    }
  }

  Future<void> _validateDownloadedApk(
    File file,
    AppUpdateAsset asset,
  ) async {
    if (!file.existsSync() || await file.length() != asset.sizeBytes) {
      throw StateError('Downloaded APK size mismatch.');
    }
    final digest = await sha256.bind(file.openRead()).first;
    if (digest.toString().toLowerCase() != asset.sha256) {
      throw StateError('Downloaded APK checksum mismatch.');
    }
  }

  Future<void> _deleteStaleDownloads(Directory directory) async {
    await for (final entry in directory.list()) {
      if (entry is File &&
          (entry.path.endsWith('.apk') || entry.path.endsWith('.part'))) {
        try {
          await entry.delete();
        } catch (_) {
          // Stale update cleanup is best-effort.
        }
      }
    }
  }

  Future<bool> _canRequestPackageInstalls() async {
    return await _appUpdateChannel.invokeMethod<bool>(
          'canRequestPackageInstalls',
        ) ??
        false;
  }

  @override
  void dispose() {
    _client.close();
    super.dispose();
  }
}

final appUpdateProvider =
    StateNotifierProvider<AppUpdateController, AppUpdateState>(
  (ref) => AppUpdateController(),
);
