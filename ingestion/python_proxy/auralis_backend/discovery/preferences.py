from __future__ import annotations

from typing import Any, Dict
import json
import threading
import time


PREFERENCES_NAMESPACE = "recommendation_preferences"
PREFERENCES_MODEL = "recommendation-preferences"
TASTE_MODES = {"neatie", "blended", "listenbrainz_first"}
_PREFERENCES_CACHE: Dict[tuple[str, str], Dict[str, Any]] = {}
_PREFERENCES_CACHE_LOCK = threading.RLock()


def _scope(value: Any) -> str:
    return str(value or "guest").strip() or "guest"


def _cache_key(server: Any, user_scope_id: str) -> tuple[str, str]:
    raw_server = getattr(server, "raw", server)
    return (
        str(getattr(raw_server, "RECOMMENDATION_STORE_DB_PATH", "")),
        _scope(user_scope_id),
    )


def default_preferences(user_scope_id: str) -> Dict[str, Any]:
    return {
        "user_scope_id": _scope(user_scope_id),
        "taste_mode": "neatie",
        "effective_taste_mode": "neatie",
        "listenbrainz_username": "",
        "listenbrainz_status": "not_linked",
        "updated_at": 0.0,
    }


def load_recommendation_preferences(server: Any, user_scope_id: str) -> Dict[str, Any]:
    from ..recommend.store_runtime import open_recommendation_store_connection

    scope = _scope(user_scope_id)
    cache_key = _cache_key(server, scope)
    with _PREFERENCES_CACHE_LOCK:
        cached = _PREFERENCES_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return default_preferences(scope)
    try:
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [PREFERENCES_NAMESPACE, scope],
        ).fetchone()
        if row is None:
            result = default_preferences(scope)
        else:
            payload = json.loads(row["payload_json"] or "{}")
            result = (
                {**default_preferences(scope), **payload, "user_scope_id": scope}
                if isinstance(payload, dict)
                else default_preferences(scope)
            )
        with _PREFERENCES_CACHE_LOCK:
            _PREFERENCES_CACHE[cache_key] = dict(result)
        return result
    except Exception:
        return default_preferences(scope)
    finally:
        connection.close()


def _listenbrainz_status(server: Any, username: str) -> str:
    if not username:
        return "not_linked"
    try:
        from .structured_providers import ListenBrainzClient

        payload = ListenBrainzClient(server).get(
            f"https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording",
            params={"count": 1, "offset": 0},
        )
        recommendations = (payload.get("payload") or {}).get("mbids") or []
        return "ready" if recommendations else "no_recommendations"
    except Exception:
        return "unavailable"


def save_recommendation_preferences(
    server: Any,
    *,
    user_scope_id: str,
    taste_mode: str,
    listenbrainz_username: str = "",
) -> Dict[str, Any]:
    from ..recommend.store_runtime import open_recommendation_store_connection

    scope = _scope(user_scope_id)
    mode = str(taste_mode or "neatie").strip().casefold()
    if mode not in TASTE_MODES:
        raise ValueError("invalid_taste_mode")
    username = str(listenbrainz_username or "").strip()
    status = _listenbrainz_status(server, username)
    effective_mode = (
        mode
        if mode == "neatie" or (username and status == "ready")
        else "neatie"
    )
    payload = {
        "user_scope_id": scope,
        "taste_mode": mode,
        "effective_taste_mode": effective_mode,
        "listenbrainz_username": username,
        "listenbrainz_status": status,
        "updated_at": time.time(),
    }
    connection = open_recommendation_store_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                PREFERENCES_NAMESPACE,
                scope,
                PREFERENCES_MODEL,
                json.dumps(payload, ensure_ascii=False),
                payload["updated_at"],
            ],
        )
        connection.commit()
    finally:
        connection.close()
    with _PREFERENCES_CACHE_LOCK:
        _PREFERENCES_CACHE[_cache_key(server, scope)] = dict(payload)
    return payload
