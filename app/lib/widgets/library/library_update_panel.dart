import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../logic/app_update_provider.dart';
import '../../ui/neatie_components.dart';

class LibraryUpdatePanel extends ConsumerWidget {
  final double radiusLarge;

  const LibraryUpdatePanel({
    super.key,
    required this.radiusLarge,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final updateState = ref.watch(appUpdateProvider);
    final manifest = updateState.manifest;
    final selectedAsset = updateState.selectedAsset;
    final canDownload = updateState.phase == AppUpdatePhase.available ||
        updateState.phase == AppUpdatePhase.failed && updateState.hasUpdate;
    final canRetryPermission =
        updateState.phase == AppUpdatePhase.permissionRequired;

    return NeatieSurface(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      radius: radiusLarge,
      color: Colors.white.withValues(alpha: 0.035),
      blur: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(
                Icons.system_update_alt_rounded,
                color: Colors.white,
                size: 20,
              ),
              const SizedBox(width: 10),
              const Expanded(
                child: Text(
                  'App updates',
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              if (updateState.isBusy)
                const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            _statusText(updateState),
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.68),
              fontSize: 13,
              height: 1.45,
            ),
          ),
          if (manifest?.releaseNotes.isNotEmpty == true) ...[
            const SizedBox(height: 10),
            Text(
              manifest!.releaseNotes,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.52),
                fontSize: 12,
                height: 1.4,
              ),
            ),
          ],
          if (updateState.phase == AppUpdatePhase.downloading) ...[
            const SizedBox(height: 12),
            LinearProgressIndicator(
              value: updateState.downloadProgress > 0
                  ? updateState.downloadProgress
                  : null,
              minHeight: 5,
              borderRadius: BorderRadius.circular(999),
            ),
          ],
          if (selectedAsset != null) ...[
            const SizedBox(height: 10),
            Text(
              'Selected APK: ${selectedAsset.abi.isEmpty ? selectedAsset.name : selectedAsset.abi}',
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.45),
                fontSize: 12,
              ),
            ),
          ],
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: updateState.isBusy
                      ? null
                      : () =>
                          ref.read(appUpdateProvider.notifier).checkForUpdate(),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Colors.white,
                    side: BorderSide(
                      color: Colors.white.withValues(alpha: 0.14),
                    ),
                  ),
                  child: const Text('Check'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton(
                  onPressed: updateState.isBusy
                      ? null
                      : canRetryPermission
                          ? () => ref
                              .read(appUpdateProvider.notifier)
                              .retryInstallAfterPermission()
                          : canDownload
                              ? () => ref
                                  .read(appUpdateProvider.notifier)
                                  .downloadAndInstall()
                              : null,
                  style: FilledButton.styleFrom(
                    backgroundColor: const Color(0xFF1B1B1B),
                    foregroundColor: Colors.white,
                  ),
                  child: Text(canRetryPermission ? 'Install' : 'Update'),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  String _statusText(AppUpdateState state) {
    switch (state.phase) {
      case AppUpdatePhase.idle:
        return 'Check GitHub Releases for a newer Neatie build.';
      case AppUpdatePhase.checking:
        return 'Checking the latest GitHub release...';
      case AppUpdatePhase.available:
        return 'Version ${state.manifest?.versionName ?? ''} is ready to download.';
      case AppUpdatePhase.upToDate:
        return 'You are up to date on ${state.currentVersionName}.';
      case AppUpdatePhase.downloading:
        final percent = (state.downloadProgress * 100).clamp(0, 100).round();
        return 'Downloading update... $percent%';
      case AppUpdatePhase.permissionRequired:
        return 'Android needs permission to install updates from Neatie. Allow it, then return here and tap Install.';
      case AppUpdatePhase.readyToInstall:
        return 'Android installer opened. Confirm the install to finish updating.';
      case AppUpdatePhase.installing:
        return 'Opening Android installer...';
      case AppUpdatePhase.failed:
        return state.error.isEmpty ? 'Update check failed.' : state.error;
    }
  }
}
