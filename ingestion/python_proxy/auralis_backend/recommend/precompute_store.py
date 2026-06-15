from __future__ import annotations

from hashlib import sha1
from typing import Any, Dict
import time

from ..storage.session_store import get_session_store


_PRECOMPUTE_TTL_SECONDS = 900
_PRECOMPUTE_MAX_AGE_SECONDS = 21600


def configure_precompute_store(
    *,
    ttl_seconds: int,
    max_age_seconds: int,
    **_legacy_home_options: Any,
) -> None:
    global _PRECOMPUTE_TTL_SECONDS
    global _PRECOMPUTE_MAX_AGE_SECONDS
    _PRECOMPUTE_TTL_SECONDS = int(ttl_seconds)
    _PRECOMPUTE_MAX_AGE_SECONDS = int(max_age_seconds)


def _search_cache_key(user_scope_id: str, query: str) -> str:
    digest = sha1(query.strip().lower().encode("utf-8")).hexdigest()
    return f"auralis:precompute:search:{user_scope_id}:{digest}"


def _search_profile_cache_key(profile_key: str, query: str) -> str:
    digest = sha1(
        f"{profile_key}|{query.strip().lower()}".encode("utf-8"),
    ).hexdigest()
    return f"auralis:precompute:search_profile:{digest}"


def _store_get(key: str, *, server: Any | None = None) -> Dict[str, Any] | None:
    del server
    try:
        payload = get_session_store().get(key)
    except Exception:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _store_set(
    key: str,
    payload: Dict[str, Any],
    ttl_seconds: int,
    *,
    server: Any | None = None,
) -> None:
    del server
    try:
        get_session_store().set(key, dict(payload), max(int(ttl_seconds), 1))
    except Exception:
        return


def _store_delete(key: str, *, server: Any | None = None) -> None:
    del server
    try:
        get_session_store().delete(key)
    except Exception:
        return


def _is_fresh(snapshot: Dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    expires_at = float(snapshot.get("expires_at") or 0.0)
    if expires_at > 0:
        return expires_at > time.time()
    generated_at = float(snapshot.get("generated_at") or 0.0)
    return generated_at > 0 and (time.time() - generated_at) <= _PRECOMPUTE_TTL_SECONDS


def _is_stale(snapshot: Dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return True
    generated_at = float(snapshot.get("generated_at") or 0.0)
    return generated_at <= 0 or (time.time() - generated_at) > _PRECOMPUTE_MAX_AGE_SECONDS


def get_search_snapshot(
    user_scope_id: str,
    query: str,
    *,
    server: Any | None = None,
    stats_increment=None,
) -> Dict[str, Any] | None:
    snapshot = _store_get(_search_cache_key(user_scope_id, query), server=server)
    if snapshot is not None and not _is_stale(snapshot):
        if callable(stats_increment):
            stats_increment("search_profile_snapshot_hits")
        return snapshot
    if callable(stats_increment):
        stats_increment("search_profile_snapshot_misses")
    return None


def get_search_snapshot_for_profile(
    profile_key: str,
    query: str,
    *,
    server: Any | None = None,
    stats_increment=None,
) -> Dict[str, Any] | None:
    snapshot = _store_get(
        _search_profile_cache_key(profile_key, query),
        server=server,
    )
    if snapshot is not None and not _is_stale(snapshot):
        if callable(stats_increment):
            stats_increment("search_profile_snapshot_hits")
        return snapshot
    if callable(stats_increment):
        stats_increment("search_profile_snapshot_misses")
    return None


def invalidate_user(user_scope_id: str, *, server: Any | None = None) -> None:
    del user_scope_id, server
    # Search snapshots are TTL-bound and query-keyed; interaction invalidation
    # is intentionally lazy to avoid scanning the shared session store.


def invalidate_user_query(
    user_scope_id: str,
    query: str,
    *,
    server: Any | None = None,
) -> None:
    _store_delete(_search_cache_key(user_scope_id, query), server=server)
