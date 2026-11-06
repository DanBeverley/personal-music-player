from __future__ import annotations

import time
from typing import Any, Dict


def recommendation_runtime_health(server: Any) -> Dict[str, Any]:
    runtime_snapshot_fn = getattr(server, "_recommendation_runtime_snapshot", None)
    runtime = {}
    if callable(runtime_snapshot_fn):
        try:
            runtime = dict(runtime_snapshot_fn() or {})
        except Exception:
            runtime = {}

    external_worker_expected = bool(
        runtime.get("external_worker_expected")
        if runtime
        else getattr(server, "RECOMMENDATION_EXTERNAL_WORKER", False)
    )
    worker_status = str(runtime.get("worker_status") or "").strip().lower()
    worker_mode = str(runtime.get("worker_mode") or "").strip().lower()
    worker_last_heartbeat_at = float(runtime.get("worker_last_heartbeat_at") or 0.0)
    heartbeat_age_seconds = (
        max(0.0, time.time() - worker_last_heartbeat_at)
        if worker_last_heartbeat_at > 0
        else 0.0
    )
    max_heartbeat_age_seconds = max(
        60.0,
        float(getattr(server, "RECOMMENDATION_SYNC_INTERVAL_SECONDS", 300)) * 2.0,
    )
    worker_healthy = (
        not external_worker_expected
        or (
            worker_status == "running"
            and worker_last_heartbeat_at > 0
            and heartbeat_age_seconds <= max_heartbeat_age_seconds
        )
    )
    return {
        "external_worker_expected": external_worker_expected,
        "worker_mode": worker_mode,
        "worker_status": worker_status,
        "worker_last_heartbeat_at": worker_last_heartbeat_at,
        "worker_heartbeat_age_seconds": round(heartbeat_age_seconds, 3),
        "worker_max_heartbeat_age_seconds": round(max_heartbeat_age_seconds, 3),
        "worker_healthy": worker_healthy,
        "external_worker_unhealthy": external_worker_expected and not worker_healthy,
        "scheduler_enabled": bool(
            runtime.get("scheduler_enabled")
            or getattr(server, "RECOMMENDATION_ENABLE_SCHEDULER", False)
        ),
        "scheduler_last_error": str(runtime.get("last_scheduler_error") or "").strip(),
    }


def schedule_runtime_bootstrap(server: Any) -> bool:
    bootstrap_fn = getattr(server, "_start_recommendation_bootstrap_thread", None)
    if not callable(bootstrap_fn):
        return False
    try:
        return bool(bootstrap_fn())
    except Exception:
        return False
