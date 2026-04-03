from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Any, Dict, Optional
import time

from ..storage.session_store import get_session_store


_FEED_SESSIONS: Dict[str, Dict[str, Any]] = {}
_FEED_LOCK = Lock()


def feed_session_key(server: Any, session_id: str) -> str:
    return f"reco:feed:{server._recommendation_trim_text(session_id)}"


def store_feed_session(server: Any, session: Dict[str, Any]) -> None:
    if not isinstance(session, dict):
        return
    session_id = server._recommendation_trim_text(session.get("session_id"))
    if not session_id:
        return
    ttl_seconds = max(int(server.RECOMMENDATION_FEED_SESSION_TTL_SECONDS or 0), 60)
    payload = deepcopy(session)
    with _FEED_LOCK:
        _FEED_SESSIONS[session_id] = payload
    try:
        get_session_store().set(
            feed_session_key(server, session_id),
            payload,
            ttl_seconds,
        )
    except Exception:
        return


def load_feed_session(server: Any, session_id: str) -> Optional[Dict[str, Any]]:
    normalized_session_id = server._recommendation_trim_text(session_id)
    if not normalized_session_id:
        return None
    with _FEED_LOCK:
        in_memory = _FEED_SESSIONS.get(normalized_session_id)
    if isinstance(in_memory, dict):
        return deepcopy(in_memory)
    try:
        payload = get_session_store().get(
            feed_session_key(server, normalized_session_id)
        )
    except Exception:
        payload = None
    if isinstance(payload, dict):
        with _FEED_LOCK:
            _FEED_SESSIONS[normalized_session_id] = deepcopy(payload)
        return deepcopy(payload)
    return None


def prune_feed_cache(server: Any) -> None:
    now = time.time()
    stale_session_ids = []
    with _FEED_LOCK:
        for session_id, payload in list(_FEED_SESSIONS.items()):
            if float((payload or {}).get("expires_at") or 0.0) > now:
                continue
            stale_session_ids.append(session_id)
            _FEED_SESSIONS.pop(session_id, None)
    for session_id in stale_session_ids:
        try:
            get_session_store().delete(feed_session_key(server, session_id))
        except Exception:
            continue


def clear_feed_sessions(server: Any) -> None:
    with _FEED_LOCK:
        stale_session_ids = list(_FEED_SESSIONS.keys())
        _FEED_SESSIONS.clear()
    for session_id in stale_session_ids:
        try:
            get_session_store().delete(feed_session_key(server, session_id))
        except Exception:
            continue
