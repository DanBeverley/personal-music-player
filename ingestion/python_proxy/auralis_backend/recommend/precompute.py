from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Tuple
import os
import time

from ..contracts import SearchV3Request
from ..domain.features import build_search_profile
from ..domain.retrieval import retrieve_search_candidates, retrieve_search_candidates_fast
from ..search.pipeline import (
    rank_album_candidates,
    rank_artist_candidates,
    rank_track_candidates,
    summarize_ranked_results,
)
from ..search.query_mode import resolve_search_mode
from ..search.runtime import search_query_intent
from .precompute_stats import (
    stats_increment as _stats_increment,
    stats_set as _stats_set,
    stats_snapshot as _stats_snapshot,
)
from .precompute_store import (
    _is_fresh,
    _search_cache_key,
    _search_profile_cache_key,
    _store_get,
    _store_set,
    configure_precompute_store,
    get_search_snapshot as _store_get_search_snapshot,
    get_search_snapshot_for_profile as _store_get_search_snapshot_for_profile,
    invalidate_user,
    invalidate_user_query,
)
from .store_runtime import open_recommendation_store_connection, resolve_server
from .warmup_runtime import schedule_profile_feature_warmup


_PRECOMPUTE_ENABLED = (
    os.environ.get("AURALIS_PRECOMPUTE_ENABLED", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)
_PRECOMPUTE_SEARCH_USERS_LIMIT = max(
    2,
    int(os.environ.get("AURALIS_PRECOMPUTE_SEARCH_USERS_LIMIT", "20")),
)
_PRECOMPUTE_SEARCH_QUERIES_PER_USER = max(
    1,
    int(os.environ.get("AURALIS_PRECOMPUTE_SEARCH_QUERIES_PER_USER", "3")),
)
_PRECOMPUTE_TTL_SECONDS = max(
    60,
    int(os.environ.get("AURALIS_PRECOMPUTE_TTL_SECONDS", "900")),
)
_PRECOMPUTE_MAX_AGE_SECONDS = max(
    _PRECOMPUTE_TTL_SECONDS,
    int(os.environ.get("AURALIS_PRECOMPUTE_MAX_AGE_SECONDS", "21600")),
)
_PRECOMPUTE_SEARCH_LIMIT = max(
    12,
    int(os.environ.get("AURALIS_PRECOMPUTE_SEARCH_LIMIT", "26")),
)
_PRECOMPUTE_SEARCH_TRACK_CAP = max(
    18,
    int(os.environ.get("AURALIS_PRECOMPUTE_SEARCH_TRACK_CAP", "72")),
)
_PRECOMPUTE_SEARCH_ENTITY_CAP = max(
    8,
    int(os.environ.get("AURALIS_PRECOMPUTE_SEARCH_ENTITY_CAP", "32")),
)

configure_precompute_store(
    ttl_seconds=_PRECOMPUTE_TTL_SECONDS,
    max_age_seconds=_PRECOMPUTE_MAX_AGE_SECONDS,
)

get_search_snapshot = partial(
    _store_get_search_snapshot,
    stats_increment=_stats_increment,
)
get_search_snapshot_for_profile = partial(
    _store_get_search_snapshot_for_profile,
    stats_increment=_stats_increment,
)


def _profile_feature_versions(profile: Dict[str, Any] | None) -> Dict[str, str]:
    prof = dict(profile or {})
    return {
        "catalog_feature_version": prof.get("catalog_feature_version") or "",
        "taste_profile_version": prof.get("taste_profile_version") or "",
        "scene_graph_version": prof.get("scene_graph_version") or "",
        "feature_source": prof.get("feature_source") or "",
    }


def _trim_search_candidates(
    candidate_map: Dict[str, Dict[str, Any]],
    cap: int,
) -> Dict[str, Dict[str, Any]]:
    ranked: List[Tuple[float, str, Dict[str, Any]]] = []
    for entity_id, entry in (candidate_map or {}).items():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        score = max(source_scores.values(), default=0.0) + (
            0.12 * max(len(source_scores) - 1, 0)
        )
        ranked.append(
            (
                float(score),
                entity_id,
                {"payload": dict(payload), "source_scores": source_scores},
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return {
        entity_id: entry
        for _score, entity_id, entry in ranked[:cap]
    }


def _retrieval_payload_tracks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(candidate_payload)
        for entry in dict(payload.get("track_candidates") or {}).values()
        if isinstance(
            candidate_payload := (entry or {}).get("payload"),
            dict,
        )
    ]


def _active_user_scopes(server: Any, *, limit: int) -> List[str]:
    connection = open_recommendation_store_connection(server)
    ranked: Dict[str, float] = {}
    now = time.time()
    try:
        rows = connection.execute(
            """
            SELECT user_scope_id, COUNT(*) AS c, MAX(occurred_at) AS last_at
            FROM recommendation_search_events
            GROUP BY user_scope_id
            ORDER BY last_at DESC
            LIMIT ?
            """,
            [max(limit * 3, 24)],
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        user_scope_id = server._assistant_safe_scope_id(
            row["user_scope_id"] or "guest",
        )
        if not user_scope_id:
            continue
        count_score = min(float(row["c"] or 0), 240.0) * 0.05
        recency_hours = max(
            (now - float(row["last_at"] or 0.0)) / 3600.0,
            0.0,
        )
        ranked[user_scope_id] = count_score + max(
            0.0,
            4.5 - min(recency_hours, 96.0) * 0.05,
        )
    return [
        user_scope_id
        for user_scope_id, _score in sorted(
            ranked.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:max(1, limit)]
    ]


def _top_queries_for_user(
    server: Any,
    *,
    user_scope_id: str,
    limit: int,
) -> List[str]:
    connection = open_recommendation_store_connection(server)
    try:
        rows = connection.execute(
            """
            SELECT query, COUNT(*) AS c, MAX(occurred_at) AS last_at
            FROM recommendation_search_events
            WHERE user_scope_id = ?
            GROUP BY query
            ORDER BY c DESC, last_at DESC
            LIMIT ?
            """,
            [user_scope_id, max(limit * 2, 8)],
        ).fetchall()
    finally:
        connection.close()
    queries: List[str] = []
    for row in rows:
        query = server._recommendation_trim_text(row["query"])
        if query and query.lower() not in {item.lower() for item in queries}:
            queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def build_search_snapshot(
    *,
    server: Any,
    user_scope_id: str,
    query: str,
    force: bool = False,
    legacy_req: Any | None = None,
    profile: Dict[str, Any] | None = None,
    search_mode: str = "",
) -> Dict[str, Any] | None:
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = server._recommendation_trim_text(query)
    if not normalized_query:
        return None
    key = _search_cache_key(normalized_scope, normalized_query)
    if not force:
        cached = _store_get(key)
        if _is_fresh(cached):
            _stats_increment("search_cache_hits")
            return dict(cached or {})
    _stats_increment("search_cache_misses")
    req = SearchV3Request(
        query=normalized_query,
        user_scope_id=normalized_scope,
        context_surface="search",
        search_mode=search_mode,
        limit=_PRECOMPUTE_SEARCH_LIMIT,
        force_refresh=bool(force),
    )
    if legacy_req is None or not isinstance(profile, dict) or not profile:
        legacy_req, profile = build_search_profile(req)
    effective_mode = resolve_search_mode(
        normalized_query,
        normalize_text_fn=server._normalize_text,
        intent_hint=search_query_intent(normalized_query, server=server),
        explicit_mode=search_mode,
    )
    setattr(req, "search_mode", effective_mode)
    if legacy_req is not None:
        setattr(legacy_req, "search_mode", effective_mode)
    retrieval = (
        retrieve_search_candidates
        if force or effective_mode in {"entity", "taste"}
        else retrieve_search_candidates_fast
    )
    payload = retrieval(
        legacy_req,
        profile,
        limit=_PRECOMPUTE_SEARCH_LIMIT,
        server=server,
    )
    trimmed = {
        "query_intent": payload.get("query_intent") or "mixed",
        "track_candidates": _trim_search_candidates(
            payload.get("track_candidates") or {},
            _PRECOMPUTE_SEARCH_TRACK_CAP,
        ),
        "artist_candidates": _trim_search_candidates(
            payload.get("artist_candidates") or {},
            _PRECOMPUTE_SEARCH_ENTITY_CAP,
        ),
        "album_candidates": _trim_search_candidates(
            payload.get("album_candidates") or {},
            _PRECOMPUTE_SEARCH_ENTITY_CAP,
        ),
        "anchor_tracks": list(payload.get("anchor_tracks") or [])[:4],
        "anchor_artist_names": list(payload.get("anchor_artist_names") or [])[:8],
        "normalized_anchor_artists": sorted(
            str(item)
            for item in payload.get("normalized_anchor_artists") or set()
            if str(item)
        ),
        "retriever_counts": dict(payload.get("retriever_counts") or {}),
        "retrieval_diagnostics": dict(payload.get("retrieval_diagnostics") or {}),
    }
    ranked_tracks = rank_track_candidates(
        server,
        req,
        profile,
        trimmed,
        limit=_PRECOMPUTE_SEARCH_LIMIT,
    )
    ranked_artists = rank_artist_candidates(server, req, profile, trimmed, limit=12)
    ranked_albums = rank_album_candidates(server, req, profile, trimmed, limit=12)
    schedule_profile_feature_warmup(
        server=server,
        warmup_key=f"profile_features:search:{normalized_scope}:{normalized_query.lower()}",
        profile=profile,
        extra_tracks=_retrieval_payload_tracks(trimmed),
        extra_artists=[
            entry.get("payload")
            for entry in trimmed["artist_candidates"].values()
            if isinstance(entry.get("payload"), dict)
        ],
        extra_albums=[
            entry.get("payload")
            for entry in trimmed["album_candidates"].values()
            if isinstance(entry.get("payload"), dict)
        ],
    )
    now = time.time()
    versions = _profile_feature_versions(profile)
    snapshot = {
        "user_scope_id": normalized_scope,
        "query": normalized_query,
        "generated_at": now,
        "expires_at": now + _PRECOMPUTE_TTL_SECONDS,
        "retrieval_payload": trimmed,
        "ranked_results": {
            "tracks": ranked_tracks[:_PRECOMPUTE_SEARCH_LIMIT],
            "artists": ranked_artists[:12],
            "albums": ranked_albums[:12],
            "ranking_summary": summarize_ranked_results(
                server,
                tracks=ranked_tracks[:_PRECOMPUTE_SEARCH_LIMIT],
                artists=ranked_artists[:8],
                albums=ranked_albums[:8],
            ),
        },
        "profile_summary": {
            "profile_key": profile.get("profile_key") or "",
            **versions,
        },
        "search_mode": effective_mode,
        "builder_mode": "search_nearline_precompute_v1",
        "feature_artifacts": versions,
    }
    _store_set(key, snapshot, _PRECOMPUTE_TTL_SECONDS)
    profile_key = server._recommendation_trim_text(profile.get("profile_key"))
    if profile_key:
        _store_set(
            _search_profile_cache_key(profile_key, normalized_query),
            snapshot,
            _PRECOMPUTE_TTL_SECONDS,
        )
    _stats_increment("search_snapshots_built")
    return snapshot


def run_precompute_cycle(
    *,
    server: Any | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    if not _PRECOMPUTE_ENABLED:
        return {"enabled": False, "search_built": 0, "search_queries": {}}
    srv = resolve_server(server)
    started_at = time.time()
    _stats_set(last_cycle_started_at=started_at, last_cycle_status="running")
    search_built = 0
    search_queries: Dict[str, List[str]] = {}
    try:
        users = _active_user_scopes(srv, limit=_PRECOMPUTE_SEARCH_USERS_LIMIT)
        for user_scope_id in users:
            queries = _top_queries_for_user(
                srv,
                user_scope_id=user_scope_id,
                limit=_PRECOMPUTE_SEARCH_QUERIES_PER_USER,
            )
            if not queries:
                continue
            search_queries[user_scope_id] = queries
            for query in queries:
                try:
                    if build_search_snapshot(
                        server=srv,
                        user_scope_id=user_scope_id,
                        query=query,
                        force=force,
                    ) is not None:
                        search_built += 1
                except Exception:
                    continue
        result = {
            "enabled": True,
            "home_built": 0,
            "search_built": search_built,
            "search_queries": search_queries,
            "duration_ms": int((time.time() - started_at) * 1000),
            "ttl_seconds": _PRECOMPUTE_TTL_SECONDS,
        }
        _stats_set(
            last_cycle_completed_at=time.time(),
            last_cycle_status="success",
            last_cycle_result=result,
        )
        return result
    except Exception as exc:
        result = {
            "enabled": True,
            "home_built": 0,
            "search_built": search_built,
            "search_queries": search_queries,
            "duration_ms": int((time.time() - started_at) * 1000),
            "error": str(exc)[:320],
        }
        _stats_set(
            last_cycle_completed_at=time.time(),
            last_cycle_status="failed",
            last_cycle_error=str(exc)[:320],
            last_cycle_result=result,
        )
        return result


def runtime_snapshot() -> Dict[str, Any]:
    snapshot = _stats_snapshot()
    snapshot.update(
        {
            "enabled": _PRECOMPUTE_ENABLED,
            "scope": "search_only",
            "ttl_seconds": _PRECOMPUTE_TTL_SECONDS,
            "max_age_seconds": _PRECOMPUTE_MAX_AGE_SECONDS,
            "search_users_limit": _PRECOMPUTE_SEARCH_USERS_LIMIT,
            "queries_per_user": _PRECOMPUTE_SEARCH_QUERIES_PER_USER,
        }
    )
    return snapshot
