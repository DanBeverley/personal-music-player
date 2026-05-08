import 'dart:async';

import 'package:flutter/material.dart';

import '../../logic/song_match_provider.dart';
import '../../ui/app_theme_tokens.dart';

Future<void> showSongMatchOptionsSheet({
  required BuildContext context,
  required SongMatchState songMatchState,
  required bool preferPendingShared,
  required VoidCallback onUsePendingShared,
  required VoidCallback onRecordSnippet,
  required VoidCallback onChooseFile,
}) {
  final pendingShared = songMatchState.pendingSharedMedia;
  return showModalBottomSheet<void>(
    context: context,
    backgroundColor: Colors.transparent,
    isScrollControlled: true,
    builder: (sheetContext) {
      return SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
          child: Container(
            decoration: BoxDecoration(
              color: const Color(0xFF111315),
              borderRadius: const BorderRadius.vertical(
                top: Radius.circular(28),
                bottom: Radius.circular(24),
              ),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  margin: const EdgeInsets.only(top: 10, bottom: 10),
                  width: 48,
                  height: 4,
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.22),
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(18, 0, 18, 14),
                  child: Row(
                    children: [
                      Container(
                        width: 42,
                        height: 42,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          borderRadius: BorderRadius.circular(appRadiusMedium),
                        ),
                        child: const Icon(
                          Icons.graphic_eq_rounded,
                          color: Colors.white,
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            const Text(
                              'Match Song',
                              style: TextStyle(
                                color: Colors.white,
                                fontSize: 18,
                                fontWeight: FontWeight.w800,
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'Record a snippet or import audio/video to identify the track.',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.66),
                                fontSize: 12,
                                height: 1.35,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                if (preferPendingShared && pendingShared != null)
                  SongMatchOptionTile(
                    icon: Icons.share_rounded,
                    title: 'Use shared file',
                    subtitle: pendingShared.displayName.isEmpty
                        ? 'Use the shared media waiting in EBB.'
                        : pendingShared.displayName,
                    onTap: () {
                      Navigator.of(sheetContext).pop();
                      onUsePendingShared();
                    },
                  ),
                SongMatchOptionTile(
                  icon: Icons.mic_rounded,
                  title: 'Record snippet',
                  subtitle: 'Capture a few seconds from the microphone.',
                  onTap: () {
                    Navigator.of(sheetContext).pop();
                    onRecordSnippet();
                  },
                ),
                SongMatchOptionTile(
                  icon: Icons.audio_file_rounded,
                  title: 'Choose file',
                  subtitle: 'Pick an audio or video file from this device.',
                  onTap: () {
                    Navigator.of(sheetContext).pop();
                    onChooseFile();
                  },
                ),
                if (pendingShared != null && !preferPendingShared)
                  SongMatchOptionTile(
                    icon: Icons.share_rounded,
                    title: 'Use shared file',
                    subtitle: pendingShared.displayName.isEmpty
                        ? 'Use the shared media waiting in EBB.'
                        : pendingShared.displayName,
                    onTap: () {
                      Navigator.of(sheetContext).pop();
                      onUsePendingShared();
                    },
                  ),
                const SizedBox(height: 10),
              ],
            ),
          ),
        ),
      );
    },
  );
}

Future<void> showSongMatchAlternativesSheet({
  required BuildContext context,
  required List<Map<String, dynamic>> alternatives,
  required Future<void> Function(Map<String, dynamic> track) onSelectAlternative,
}) {
  return showModalBottomSheet<void>(
    context: context,
    backgroundColor: Colors.transparent,
    isScrollControlled: true,
    builder: (sheetContext) {
      return SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
          child: Container(
            decoration: BoxDecoration(
              color: const Color(0xFF111315),
              borderRadius: const BorderRadius.vertical(
                top: Radius.circular(28),
                bottom: Radius.circular(24),
              ),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  margin: const EdgeInsets.only(top: 10, bottom: 10),
                  width: 48,
                  height: 4,
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.22),
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
                const Padding(
                  padding: EdgeInsets.fromLTRB(18, 0, 18, 12),
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      'Possible matches',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 18,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
                ),
                Flexible(
                  child: ListView.builder(
                    shrinkWrap: true,
                    itemCount: alternatives.length,
                    itemBuilder: (context, index) {
                      final entry = alternatives[index];
                      final resolvedTrack = entry['resolved_track'] is Map
                          ? Map<String, dynamic>.from(
                              entry['resolved_track'] as Map,
                            )
                          : null;
                      final recognizedMetadata =
                          entry['recognized_metadata'] is Map
                              ? Map<String, dynamic>.from(
                                  entry['recognized_metadata'] as Map,
                                )
                              : const <String, dynamic>{};
                      final title = resolvedTrack?['title']
                                  ?.toString()
                                  .trim()
                                  .isNotEmpty ==
                              true
                          ? resolvedTrack!['title'].toString().trim()
                          : (recognizedMetadata['title'] ?? 'Unknown track')
                              .toString();
                      final artist = resolvedTrack?['channel']
                                  ?.toString()
                                  .trim()
                                  .isNotEmpty ==
                              true
                          ? resolvedTrack!['channel'].toString().trim()
                          : (recognizedMetadata['artist'] ?? 'Unknown artist')
                              .toString();
                      return ListTile(
                        leading: Container(
                          width: 40,
                          height: 40,
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.06),
                            borderRadius:
                                BorderRadius.circular(appRadiusMedium),
                          ),
                          child: const Icon(
                            Icons.music_note_rounded,
                            color: Colors.white70,
                          ),
                        ),
                        title: Text(
                          title,
                          style: const TextStyle(color: Colors.white),
                        ),
                        subtitle: Text(
                          artist,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.6),
                          ),
                        ),
                        trailing: Text(
                          '${(entry['confidence'] as num?)?.toStringAsFixed(0) ?? '0'}%',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.6),
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        onTap: resolvedTrack == null
                            ? null
                            : () {
                                Navigator.of(sheetContext).pop();
                                WidgetsBinding.instance.addPostFrameCallback((_) {
                                  unawaited(
                                    onSelectAlternative(resolvedTrack),
                                  );
                                });
                              },
                      );
                    },
                  ),
                ),
                const SizedBox(height: 12),
              ],
            ),
          ),
        ),
      );
    },
  );
}

class SongMatchLauncher extends StatelessWidget {
  const SongMatchLauncher({
    super.key,
    required this.songMatchState,
    required this.onOpen,
    required this.onCancelRecording,
    required this.onStopRecording,
  });

  final SongMatchState songMatchState;
  final VoidCallback onOpen;
  final VoidCallback onCancelRecording;
  final VoidCallback onStopRecording;

  @override
  Widget build(BuildContext context) {
    final pendingShared = songMatchState.pendingSharedMedia;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(appRadiusLarge),
        border: Border.all(
          color: Colors.white.withValues(alpha: 0.08),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.06),
                  borderRadius: BorderRadius.circular(appRadiusMedium),
                ),
                child: const Icon(
                  Icons.graphic_eq_rounded,
                  color: Colors.white,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Match Song',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 15,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      pendingShared != null
                          ? 'Shared media is ready to identify.'
                          : 'Record a snippet or upload media to identify a track.',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.66),
                        fontSize: 12,
                        height: 1.35,
                      ),
                    ),
                  ],
                ),
              ),
              FilledButton(
                onPressed: songMatchState.isBusy ? null : onOpen,
                style: FilledButton.styleFrom(
                  backgroundColor: Colors.white.withValues(alpha: 0.1),
                  foregroundColor: Colors.white,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 10,
                  ),
                ),
                child: const Text('Open'),
              ),
            ],
          ),
          if (songMatchState.phase == SongMatchPhase.recording) ...[
            const SizedBox(height: 14),
            Row(
              children: [
                const Icon(Icons.mic_rounded, color: Colors.redAccent),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    songMatchState.statusMessage,
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.82),
                      fontSize: 12,
                    ),
                  ),
                ),
                TextButton(
                  onPressed: onCancelRecording,
                  child: const Text('Cancel'),
                ),
                FilledButton(
                  onPressed: onStopRecording,
                  child: const Text('Stop'),
                ),
              ],
            ),
          ] else if (songMatchState.isBusy) ...[
            const SizedBox(height: 14),
            Row(
              children: [
                const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    songMatchState.statusMessage,
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.82),
                      fontSize: 12,
                    ),
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class SongMatchOptionTile extends StatelessWidget {
  const SongMatchOptionTile({
    super.key,
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      onTap: onTap,
      leading: Container(
        width: 42,
        height: 42,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.06),
          borderRadius: BorderRadius.circular(appRadiusMedium),
        ),
        child: Icon(icon, color: Colors.white),
      ),
      title: Text(
        title,
        style: const TextStyle(
          color: Colors.white,
          fontWeight: FontWeight.w600,
        ),
      ),
      subtitle: Text(
        subtitle,
        style: TextStyle(
          color: Colors.white.withValues(alpha: 0.62),
          fontSize: 12,
          height: 1.35,
        ),
      ),
      trailing: const Icon(
        Icons.chevron_right_rounded,
        color: Colors.white70,
      ),
    );
  }
}
