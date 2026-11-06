from __future__ import annotations

from threading import Lock
from typing import Any, Dict, List
import os
import time

from .taste_runtime import warm_profile_feature_artifacts


_PRECOMPUTE_ENABLED = (
    os.environ.get("AURALIS_PRECOMPUTE_ENABLED", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)
_PROFILE_FEATURE_WARMUP_COOLDOWN_SECONDS = max(
    60,
    int(os.environ.get("AURALIS_PROFILE_FEATURE_WARMUP_COOLDOWN_SECONDS", "900")),
)
_WARMUP_INFLIGHT_STALE_SECONDS = max(
    60,
    int(os.environ.get("AURALIS_WARMUP_INFLIGHT_STALE_SECONDS", "600")),
)

_warmup_lock = Lock()
_inflight_warmups: set[str] = set()
_inflight_warmup_started_at: Dict[str, float] = {}
_recent_profile_feature_warmups: Dict[str, float] = {}


def _log_warmup_event(
    warmup_kind: str,
    warmup_key: str,
    stage: str,
    **fields: Any,
) -> None:
    suffix = " ".join(
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None and str(value) != ""
    )
    print(
        "[EBB:recommend][warmup] "
        f"kind={warmup_kind} key={warmup_key} stage={stage}"
        f"{(' ' + suffix) if suffix else ''}",
        flush=True,
    )


def _precompute_executor(server: Any):
    return getattr(server, "precompute_executor", None) or getattr(
        server,
        "recommendation_executor",
    )


def _begin_warmup(warmup_key: str) -> tuple[bool, bool]:
    if not _PRECOMPUTE_ENABLED:
        return False, False
    stale_reset = False
    with _warmup_lock:
        now = time.monotonic()
        if warmup_key in _inflight_warmups:
            started_at = float(_inflight_warmup_started_at.get(warmup_key) or 0.0)
            if started_at > 0.0 and (now - started_at) >= _WARMUP_INFLIGHT_STALE_SECONDS:
                _inflight_warmups.discard(warmup_key)
                _inflight_warmup_started_at.pop(warmup_key, None)
                stale_reset = True
            else:
                return False, False
        _inflight_warmups.add(warmup_key)
        _inflight_warmup_started_at[warmup_key] = now
    return True, stale_reset


def _finish_warmup(warmup_key: str) -> None:
    with _warmup_lock:
        _inflight_warmups.discard(warmup_key)
        _inflight_warmup_started_at.pop(warmup_key, None)


def _mark_profile_feature_warmup_started(warmup_key: str) -> bool:
    with _warmup_lock:
        now = time.monotonic()
        last_started_at = float(_recent_profile_feature_warmups.get(warmup_key) or 0.0)
        if last_started_at > 0.0 and (
            now - last_started_at
        ) < _PROFILE_FEATURE_WARMUP_COOLDOWN_SECONDS:
            return False
        _recent_profile_feature_warmups[warmup_key] = now
        if len(_recent_profile_feature_warmups) > 1024:
            cutoff = now - (_PROFILE_FEATURE_WARMUP_COOLDOWN_SECONDS * 4)
            stale_keys = [
                key
                for key, started_at in _recent_profile_feature_warmups.items()
                if float(started_at or 0.0) < cutoff
            ]
            for stale_key in stale_keys[:512]:
                _recent_profile_feature_warmups.pop(stale_key, None)
    return True


def schedule_profile_feature_warmup(
    *,
    server: Any,
    warmup_key: str,
    profile: Dict[str, Any],
    extra_tracks: List[Dict[str, Any]] | None = None,
    extra_artists: List[Dict[str, Any]] | None = None,
    extra_albums: List[Dict[str, Any]] | None = None,
) -> bool:
    started, _stale_reset = _begin_warmup(warmup_key)
    if not started:
        return False
    if not _mark_profile_feature_warmup_started(warmup_key):
        _finish_warmup(warmup_key)
        return False

    def _warm() -> None:
        try:
            warm_profile_feature_artifacts(
                server,
                profile,
                extra_tracks=list(extra_tracks or []),
                extra_artists=list(extra_artists or []),
                extra_albums=list(extra_albums or []),
            )
        except Exception:
            return
        finally:
            _finish_warmup(warmup_key)

    try:
        _precompute_executor(server).submit(_warm)
        return True
    except Exception:
        _finish_warmup(warmup_key)
        return False
