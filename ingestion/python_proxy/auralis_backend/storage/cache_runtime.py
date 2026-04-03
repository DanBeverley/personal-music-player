from __future__ import annotations

import time
from threading import Lock
from typing import Any


stream_info_cache: dict[str, Any] = {}
stream_info_inflight: dict[str, Any] = {}
stream_info_lock = Lock()
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


def lookup_ttl_cache(cache_root, lock: Lock, namespace: str, key: str):
    now = time.time()
    with lock:
        namespace_cache = cache_root.get(namespace) or {}
        cached = namespace_cache.get(key)
        if cached and cached.get("expires_at", 0) > now:
            return cached.get("value")
        if cached:
            namespace_cache.pop(key, None)
    return None


def store_ttl_cache(cache_root, lock: Lock, namespace: str, key: str, value, ttl_seconds: int) -> None:
    with lock:
        namespace_cache = cache_root.setdefault(namespace, {})
        namespace_cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
        }


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
    with recommendation_track_details_lock:
        recommendation_track_details_cache[normalized_id] = {
            "track": dict(track or {}),
            "expires_at": time.time() + ttl_seconds,
        }
