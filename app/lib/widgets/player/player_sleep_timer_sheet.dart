import 'package:flutter/material.dart';

import '../../logic/audio_provider.dart';

String _formatPlayerSleepTimerLabel(int remainingSeconds) {
  if (remainingSeconds <= 0) return 'Off';
  final hours = remainingSeconds ~/ 3600;
  final minutes = (remainingSeconds % 3600) ~/ 60;
  if (hours > 0) {
    return minutes > 0 ? '${hours}h ${minutes}m' : '${hours}h';
  }
  return '${minutes}m';
}

Future<void> showPlayerSleepTimerSheet(
  BuildContext context,
  AudioPlayerNotifier audioNotifier,
  PlayerState playerState, {
  required Color surfaceColor,
  required Color accentColor,
  required double radiusLarge,
}) async {
  final initialMinutes = playerState.sleepTimerRemainingSeconds > 0
      ? (playerState.sleepTimerRemainingSeconds / 60).clamp(5, 180).round()
      : 30;

  await showModalBottomSheet<void>(
    context: context,
    backgroundColor: surfaceColor,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(radiusLarge)),
    ),
    builder: (sheetContext) {
      double sliderMinutes = initialMinutes.toDouble();
      return StatefulBuilder(
        builder: (context, setSheetState) {
          final formattedValue = _formatPlayerSleepTimerLabel(
            Duration(minutes: sliderMinutes.round()).inSeconds,
          );
          return SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Sleep timer',
                    style: TextStyle(
                      color: Colors.white,
                      fontSize: 18,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    playerState.sleepTimerRemainingSeconds > 0
                        ? 'Currently ends in ${_formatPlayerSleepTimerLabel(playerState.sleepTimerRemainingSeconds)}.'
                        : 'Stop playback after a set time.',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.58),
                      fontSize: 13,
                    ),
                  ),
                  const SizedBox(height: 24),
                  Row(
                    children: [
                      Text(
                        'Off',
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.48),
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const Spacer(),
                      Text(
                        formattedValue,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 16,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ],
                  ),
                  SliderTheme(
                    data: SliderTheme.of(context).copyWith(
                      activeTrackColor: accentColor,
                      inactiveTrackColor: Colors.white.withValues(alpha: 0.12),
                      thumbColor: Colors.white,
                      overlayColor: Colors.white.withValues(alpha: 0.08),
                      trackHeight: 3,
                    ),
                    child: Slider(
                      min: 5,
                      max: 180,
                      divisions: 35,
                      value: sliderMinutes,
                      onChanged: (value) {
                        setSheetState(() => sliderMinutes = value);
                      },
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: TextButton(
                          onPressed: () {
                            audioNotifier.setSleepTimer(null);
                            Navigator.of(sheetContext).pop();
                          },
                          child: const Text(
                            'Turn Off',
                            style: TextStyle(color: Colors.white70),
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: TextButton(
                          onPressed: () {
                            audioNotifier.setSleepTimer(
                              Duration(minutes: sliderMinutes.round()),
                            );
                            Navigator.of(sheetContext).pop();
                          },
                          child: const Text(
                            'Set Timer',
                            style: TextStyle(color: Colors.white),
                          ),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          );
        },
      );
    },
  );
}
