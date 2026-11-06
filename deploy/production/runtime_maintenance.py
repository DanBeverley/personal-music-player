"""Daily bounded retention; never stops serving processes or touches audio."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def run(runtime: Path, backups: Path) -> dict:
    marker = runtime / 'background-demand.touch'
    # Recent/unknown demand is a deliberate no-op, not a retention failure.
    if not marker.is_file() or time.time() - marker.stat().st_mtime < 3600:
        return {'skipped': 'recent_or_unknown_demand'}
    receipts = sorted(backups.glob('neatie-runtime-*.tar.gz.verified.json'), reverse=True)
    if not receipts:
        return {'failed': 'no_verified_backup', 'exit_code': 1}
    command = [sys.executable, str(Path(__file__).with_name('runtime_retention.py')),
               '--db', str(runtime / 'recommendation_store.sqlite'), '--apply', '--online',
               '--max-rows', '500', '--budget-seconds', '60', '--verified-backup', str(receipts[0])]
    for attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=75)
        except subprocess.TimeoutExpired:
            return {'failed': 'retention_timeout', 'exit_code': 1}
        if not result.returncode:
            return json.loads(result.stdout)
        # A fresh transaction can reuse verified reference projections even
        # when the first expansion lost its WAL snapshot to a heartbeat.
        if not any(error in result.stderr for error in ('database is locked', 'interrupted', 'retention_budget_exceeded')):
            break
    return {'failed': 'retention_refused_or_failed', 'exit_code': result.returncode}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path('/var/lib/neatie/runtime'))
    parser.add_argument('--backups', type=Path, default=Path('/var/backups/neatie'))
    args = parser.parse_args()
    outcome = run(args.runtime, args.backups)
    print(json.dumps(outcome, sort_keys=True), flush=True)
    raise SystemExit(int(outcome.get('exit_code') or 0))
