from __future__ import annotations

from threading import Lock
from typing import Any, Dict


_runtime_lock = Lock()
_runtime_stats: Dict[str, Any] = {
    "last_cycle_started_at": 0.0,
    "last_cycle_completed_at": 0.0,
    "last_cycle_status": "idle",
    "last_cycle_error": "",
    "home_profiles_warmed": 0,
    "search_profiles_warmed": 0,
    "home_profile_cache_hits": 0,
    "search_profile_cache_hits": 0,
    "home_profile_snapshot_hits": 0,
    "home_profile_snapshot_misses": 0,
    "search_profile_snapshot_hits": 0,
    "search_profile_snapshot_misses": 0,
    "home_snapshots_built": 0,
    "search_snapshots_built": 0,
    "home_launch_artifacts_built": 0,
    "home_heavy_artifacts_built": 0,
    "home_launch_artifact_hits": 0,
    "home_heavy_artifact_hits": 0,
    "home_cache_hits": 0,
    "search_cache_hits": 0,
    "home_cache_misses": 0,
    "search_cache_misses": 0,
    "last_cycle_result": {},
}


def stats_increment(name: str, amount: int = 1) -> None:
    with _runtime_lock:
        _runtime_stats[name] = int(_runtime_stats.get(name) or 0) + int(amount)


def stats_set(**kwargs: Any) -> None:
    with _runtime_lock:
        for key, value in kwargs.items():
            _runtime_stats[key] = value


def stats_snapshot() -> Dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime_stats)
