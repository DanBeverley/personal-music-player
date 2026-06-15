const int maxSleepTimerRemainingSeconds = 86400;

int sleepTimerRemainingSeconds(DateTime? endsAt, {DateTime? now}) {
  if (endsAt == null) {
    return 0;
  }
  final currentTime = now ?? DateTime.now();
  return endsAt
      .difference(currentTime)
      .inSeconds
      .clamp(0, maxSleepTimerRemainingSeconds);
}

bool sleepTimerHasExpired(DateTime? endsAt, {DateTime? now}) {
  return endsAt != null && sleepTimerRemainingSeconds(endsAt, now: now) <= 0;
}
