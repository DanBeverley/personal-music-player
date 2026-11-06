from __future__ import annotations

import time
import sys
from threading import Lock
from typing import Any


stream_info_cache: dict[str, Any] = {}
stream_info_inflight: dict[str, Any] = {}
stream_info_lock = Lock()
stream_failure_cache: dict[str, Any] = {}
stream_failure_lock = Lock()
stream_chunk_cache: dict[str, Any] = {}
stream_chunk_lock = Lock()
stream_chunk_inflight: dict[str, Any] = {}
stream_chunk_inflight_lock = Lock()

home_candidates_cache = {"expires_at": 0.0, "results": []}
home_candidates_lock = Lock()

recommendation_track_details_cache: dict[str, Any] = {}
recommendation_track_details_lock = Lock()

search_result_cache = {
    "tracks": {},
    "albums": {},
    "artists_direct": {},
    "artists": {},
    "playlists_direct": {},
    "recommended_artists": {},
    "suggestions": {},
}
search_result_cache_lock = Lock()

detail_result_cache = {
    "track": {},
    "album": {},
    "artist": {},
}
detail_result_cache_lock = Lock()

recommendation_embedding_cache = {
    "track": {},
    "artist": {},
    "text": {},
    "album": {},
}
recommendation_embedding_lock = Lock()

# These are disposable acceleration caches, never durable inventory owners.
TTL_CACHE_MAX_ENTRIES = 128
TTL_CACHE_MAX_BYTES = 32 * 1024 * 1024


def _prune_root(root: dict, now: float) -> None:
    """One byte budget per cache family, rather than one per namespace."""
    for entries in root.values():
        _prune_cache(entries, now)
    total = sum(row.get("size_bytes", 0) for entries in root.values() for row in entries.values())
    if total <= TTL_CACHE_MAX_BYTES:
        return
    oldest = sorted((row.get("accessed_at", 0), name, key)
                    for name, entries in root.items() for key, row in entries.items())
    for _, name, key in oldest:
        total -= root[name].pop(key).get("size_bytes", 0)
        if total <= TTL_CACHE_MAX_BYTES:
            break


def metadata_cache_stats() -> dict[str, int]:
    """Numeric retained estimates only; never keys, values or deep heap walks."""
    result = {}
    for name, root, lock in (("search", search_result_cache, search_result_cache_lock),
                             ("detail", detail_result_cache, detail_result_cache_lock),
                             ("embedding", recommendation_embedding_cache, recommendation_embedding_lock)):
        if not lock.acquire(blocking=False):
            continue
        try:
            result[name + "Entries"] = sum(map(len, root.values()))
            result[name + "EstimatedBytes"] = sum(row.get("size_bytes", 0) for entries in root.values() for row in entries.values())
        finally:
            lock.release()
    return result


def retained_size(value: Any) -> int:
    """Count Python objects once, including nested containers."""
    seen: set[int] = set()
    pending = [value]
    total = 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
    return total


def _prune_cache(entries: dict, now: float) -> None:
    for key in list(entries):
        if entries[key].get("expires_at", 0) <= now:
            entries.pop(key, None)
    total = sum(entry.get("size_bytes", 0) for entry in entries.values())
    while entries and (len(entries) > TTL_CACHE_MAX_ENTRIES or total > TTL_CACHE_MAX_BYTES):
        oldest = next(iter(entries))
        total -= entries.pop(oldest).get("size_bytes", 0)


def prune_expired_runtime_caches(now: float | None = None) -> None:
    """Release disposable metadata even when its namespace is never revisited.

    Audio bytes, pinned streams and in-flight work have separate lifetimes and
    are deliberately not part of this sweep.
    """
    now = time.time() if now is None else now
    for root, lock in ((search_result_cache, search_result_cache_lock),
                       (detail_result_cache, detail_result_cache_lock),
                       (recommendation_embedding_cache, recommendation_embedding_lock)):
        with lock:
            _prune_root(root, now)
    with recommendation_track_details_lock:
        _prune_cache(recommendation_track_details_cache, now)
    with home_candidates_lock:
        if home_candidates_cache.get("expires_at", 0) <= now:
            home_candidates_cache.update(expires_at=0.0, results=[])
    # Imports stay lazy: constructing a cache must not construct a server.
    from .session_store import prune_expired_memory_sessions
    from ..search.service import prune_search_memory
    prune_expired_memory_sessions(now)
    prune_search_memory(now)
    from ..memory_runtime import memory_tick
    memory_tick()


def lookup_ttl_cache(cache_root, lock: Lock, namespace: str, key: str):
    now = time.time()
    with lock:
        _prune_root(cache_root, now)
        namespace_cache = cache_root.get(namespace) or {}
        cached = namespace_cache.get(key)
        if cached and cached.get("expires_at", 0) > now:
            cached["accessed_at"] = time.monotonic()
            return cached.get("value")
        if cached:
            namespace_cache.pop(key, None)
    return None


def store_ttl_cache(cache_root, lock: Lock, namespace: str, key: str, value, ttl_seconds: int) -> None:
    size_bytes = retained_size(value)
    with lock:
        namespace_cache = cache_root.setdefault(namespace, {})
        namespace_cache.pop(key, None)
        _prune_cache(namespace_cache, time.time())
        if size_bytes > TTL_CACHE_MAX_BYTES or ttl_seconds <= 0:
            return
        namespace_cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
            "size_bytes": size_bytes,
            "accessed_at": time.monotonic(),
        }
        _prune_root(cache_root, time.time())


def clear_ttl_namespace(cache_root, lock: Lock, namespace: str) -> None:
    with lock:
        cache_root[namespace] = {}


def lookup_home_candidates(limit: int):
    now = time.time()
    with home_candidates_lock:
        cached = home_candidates_cache.get("results") or []
        if home_candidates_cache.get("expires_at", 0) > now and len(cached) >= max(5, limit):
            return list(cached[: limit + 10])
    return []


def store_home_candidates(results, *, ttl_seconds: int, limit: int) -> None:
    with home_candidates_lock:
        home_candidates_cache["results"] = list(results[: limit + 10])
        home_candidates_cache["expires_at"] = time.time() + ttl_seconds


def lookup_recommendation_track_detail(track_id: str):
    normalized_id = (track_id or "").strip()
    if not normalized_id:
        return None
    now = time.time()
    with recommendation_track_details_lock:
        _prune_cache(recommendation_track_details_cache, now)
        cached = recommendation_track_details_cache.get(normalized_id)
        if cached and cached.get("expires_at", 0) > now:
            return dict(cached.get("track") or {})
        if cached:
            recommendation_track_details_cache.pop(normalized_id, None)
    return None


def store_recommendation_track_detail(track_id: str, track: dict[str, Any], *, ttl_seconds: int) -> None:
    normalized_id = (track_id or "").strip()
    if not normalized_id:
        return
    size_bytes = retained_size(track)
    with recommendation_track_details_lock:
        recommendation_track_details_cache.pop(normalized_id, None)
        _prune_cache(recommendation_track_details_cache, time.time())
        if size_bytes > TTL_CACHE_MAX_BYTES or ttl_seconds <= 0:
            return
        recommendation_track_details_cache[normalized_id] = {
            "track": dict(track or {}),
            "expires_at": time.time() + ttl_seconds,
            "size_bytes": size_bytes,
        }
        _prune_cache(recommendation_track_details_cache, time.time())
