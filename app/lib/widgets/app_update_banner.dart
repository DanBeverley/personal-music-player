import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../logic/app_update_provider.dart';

class AppUpdateBanner extends ConsumerWidget {
  const AppUpdateBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(appUpdateProvider);
    final manifest = state.manifest;
    if (!state.hasUpdate || manifest == null) {
      return const SizedBox.shrink();
    }

    final controller = ref.read(appUpdateProvider.notifier);
    final permissionRequired = state.phase == AppUpdatePhase.permissionRequired;
    final status = switch (state.phase) {
      AppUpdatePhase.downloading =>
        'Downloading ${(state.downloadProgress * 100).clamp(0, 100).round()}%',
      AppUpdatePhase.installing => 'Opening installer…',
      AppUpdatePhase.readyToInstall => 'Confirm the update in Android',
      AppUpdatePhase.failed =>
        state.error.isEmpty ? 'Update failed' : state.error,
      _ => 'Version ${manifest.versionName} is available',
    };

    return SafeArea(
      minimum: const EdgeInsets.fromLTRB(12, 8, 12, 0),
      child: Material(
        color: const Color(0xFF202020),
        elevation: 14,
        borderRadius: BorderRadius.circular(16),
        clipBehavior: Clip.antiAlias,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
              child: Row(
                children: [
                  const Icon(Icons.system_update_alt_rounded, size: 21),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          manifest.mandatory
                              ? 'Required update'
                              : 'Neatie update',
                          style: const TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          status,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.68),
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (!manifest.mandatory && !state.isBusy)
                    IconButton(
                      tooltip: 'Remind me on the next version',
                      onPressed: controller.dismissAvailableUpdate,
                      icon: const Icon(Icons.close_rounded, size: 20),
                    ),
                  const SizedBox(width: 2),
                  FilledButton(
                    onPressed: state.isBusy
                        ? null
                        : permissionRequired
                            ? controller.retryInstallAfterPermission
                            : controller.downloadAndInstall,
                    child: Text(permissionRequired ? 'Continue' : 'Update'),
                  ),
                ],
              ),
            ),
            if (state.phase == AppUpdatePhase.downloading)
              LinearProgressIndicator(
                value:
                    state.downloadProgress > 0 ? state.downloadProgress : null,
                minHeight: 3,
              ),
          ],
        ),
      ),
    );
  }
}
