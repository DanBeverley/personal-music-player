import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

const String appUpdateManifestUrl =
    'https://github.com/DanBeverley/personal-music-player/releases/latest/download/update.json';

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
      name: name.isEmpty ? 'ebb-update.apk' : name,
    );
  }
}

class AppUpdateManifest {
  const AppUpdateManifest({
    required this.versionName,
    required this.versionCode,
    required this.releaseNotes,
    required this.mandatory,
    required this.assets,
  });

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
          parsedAssets.add(AppUpdateAsset.fromJson(
            Map<String, dynamic>.from(value),
          ));
        }
      }
    } else if ((json['apkUrl'] ?? '').toString().trim().isNotEmpty) {
      parsedAssets.add(AppUpdateAsset.fromJson(json));
    }
    return AppUpdateManifest(
      versionName: (json['versionName'] ?? '').toString().trim(),
      versionCode: (json['versionCode'] as num?)?.toInt() ?? 0,
      releaseNotes: (json['releaseNotes'] ?? '').toString().trim(),
      mandatory: json['mandatory'] == true,
      assets: parsedAssets,
    );
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
  AppUpdateController() : super(const AppUpdateState());

  final http.Client _client = http.Client();

  Future<void> checkForUpdate() async {
    state = state.copyWith(
      phase: AppUpdatePhase.checking,
      error: '',
      downloadProgress: 0,
      downloadedApkPath: '',
    );
    try {
      final versionInfo = await _loadVersionInfo();
      final manifest = await _fetchManifest();
      final selectedAsset = _selectAsset(
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
        phase: AppUpdatePhase.failed,
        error: error.toString(),
        clearManifest: true,
        clearSelectedAsset: true,
      );
    }
  }

  Future<void> downloadAndInstall() async {
    final manifest = state.manifest;
    final asset = state.selectedAsset;
    if (manifest == null || asset == null) {
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
      final apkPath = await _downloadAsset(activeAsset);
      await _verifySha256(apkPath, activeAsset.sha256);
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
      state = state.copyWith(
        phase: AppUpdatePhase.installing,
        downloadedApkPath: apkPath,
        downloadProgress: 1,
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
    } catch (error) {
      state = state.copyWith(
        phase: AppUpdatePhase.failed,
        error: error.toString(),
      );
    }
  }

  Future<void> retryInstallAfterPermission() async {
    final apkPath = state.downloadedApkPath;
    if (apkPath.trim().isEmpty) {
      await downloadAndInstall();
      return;
    }
    final canInstall = await _canRequestPackageInstalls();
    if (!canInstall) {
      await _appUpdateChannel.invokeMethod<void>(
        'openInstallPermissionSettings',
      );
      return;
    }
    state = state.copyWith(phase: AppUpdatePhase.installing, error: '');
    final launched = await _appUpdateChannel.invokeMethod<bool>(
          'installApk',
          {'path': apkPath},
        ) ??
        false;
    state = state.copyWith(
      phase: launched ? AppUpdatePhase.readyToInstall : AppUpdatePhase.failed,
      error: launched ? '' : 'Android package installer could not be opened.',
    );
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
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw StateError('Update manifest is not a JSON object.');
    }
    return AppUpdateManifest.fromJson(Map<String, dynamic>.from(decoded));
  }

  AppUpdateAsset? _selectAsset(
    List<AppUpdateAsset> assets,
    List<String> supportedAbis,
  ) {
    if (assets.isEmpty) return null;
    final normalizedAssets = assets
        .where((asset) => asset.url.isNotEmpty)
        .toList(growable: false);
    for (final abi in supportedAbis) {
      for (final asset in normalizedAssets) {
        if (asset.abi == abi || asset.name.contains(abi)) {
          return asset;
        }
      }
    }
    return normalizedAssets.length == 1 ? normalizedAssets.first : null;
  }

  Future<String> _downloadAsset(AppUpdateAsset asset) async {
    final directory = await getTemporaryDirectory();
    final updatesDir = Directory('${directory.path}/updates');
    if (!updatesDir.existsSync()) {
      updatesDir.createSync(recursive: true);
    }
    final safeName = asset.name.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    final file = File('${updatesDir.path}/$safeName');
    final request = http.Request('GET', Uri.parse(asset.url));
    final response = await _client.send(request).timeout(
          const Duration(seconds: 20),
        );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError('APK download failed: HTTP ${response.statusCode}.');
    }
    final sink = file.openWrite();
    var received = 0;
    final responseLength = response.contentLength ?? 0;
    final total = responseLength > 0 ? responseLength : asset.sizeBytes;
    try {
      await for (final chunk in response.stream) {
        received += chunk.length;
        sink.add(chunk);
        if (total > 0) {
          state = state.copyWith(
            downloadProgress: (received / total).clamp(0, 1).toDouble(),
          );
        }
      }
    } finally {
      await sink.close();
    }
    return file.path;
  }

  Future<void> _verifySha256(String path, String expectedSha256) async {
    if (expectedSha256.isEmpty) {
      throw StateError('Update manifest is missing SHA-256 for this APK.');
    }
    final digest = await sha256.bind(File(path).openRead()).first;
    final actual = digest.toString().toLowerCase();
    if (actual != expectedSha256.toLowerCase()) {
      try {
        File(path).deleteSync();
      } catch (_) {}
      throw StateError('Downloaded APK checksum mismatch.');
    }
  }

  Future<bool> _canRequestPackageInstalls() async {
    return await _appUpdateChannel.invokeMethod<bool>(
          'canRequestPackageInstalls',
        ) ??
        false;
  }
}

final appUpdateProvider =
    StateNotifierProvider<AppUpdateController, AppUpdateState>(
  (ref) => AppUpdateController(),
);
