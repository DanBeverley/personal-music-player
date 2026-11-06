"""Optional-work idle policy, shared by API/worker through one operational marker.

No accounts, client heartbeats or playback control. Missing/unreadable state fails
active. Only speculative background work is gated; leased work finishes normally.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock, Thread
import time

_lock = Lock()
_last_write: dict[str, float] = {}


def _path(server) -> Path:
    return Path(server.RECOMMENDATION_STORE_DB_PATH).resolve().parent / "background-demand.touch"


def enabled(server) -> bool:
    return bool(getattr(server, "NEATIE_BACKGROUND_IDLE", False))


def initialize_demand(server) -> None:
    """Create first-boot state without treating a restart as user activity."""
    if not enabled(server):
        return
    try:
        _path(server).touch(exist_ok=False)
    except OSError:
        # An existing clock belongs to real demand. Unknown/unwritable state
        # retains is_idle's fail-active behavior rather than blocking work.
        pass


def note_demand(server) -> None:
    if not enabled(server):
        return
    path = _path(server)
    key = str(path)
    now = time.monotonic()
    with _lock:
        if now - _last_write.get(key, float('-inf')) < 5:
            return
        _last_write[key] = now
        while len(_last_write) > 16:
            _last_write.pop(next(iter(_last_write)))
    # No filesystem/SQLite wait in the request or audio first-byte path.
    def touch():
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
    Thread(target=touch, name="background-demand", daemon=True).start()


def is_idle(server, *, now: float | None = None) -> bool:
    if not enabled(server):
        return False
    try:
        modified = _path(server).stat().st_mtime
        grace = max(60, float(getattr(server, "NEATIE_BACKGROUND_IDLE_SECONDS", 1800)))
        age = (time.time() if now is None else now) - modified
        return age > grace
    except (OSError, ValueError, TypeError):
        return False


def user_request(path: str, method: str) -> bool:
    # Health probes, trace polls and static artwork cannot keep work awake.
    return method in {"GET", "POST", "PUT", "DELETE", "PATCH"} and (
        path in {"/search", "/suggest", "/recommend", "/related_tracks",
                 "/history_seed", "/interaction_event", "/search_interaction",
                 "/prepare_session", "/playback/resolve", "/charts", "/new_releases"}
        or path.startswith(("/playback/stream/", "/album/", "/artist/", "/library/"))
    )
