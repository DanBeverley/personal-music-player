from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Tuple
import json
import os
import time

from ..contracts import RecommendationHomeV3Request, SearchV3Request
from ..domain.features import build_home_profile, build_search_profile
from ..domain.retrieval import retrieve_search_candidates, retrieve_search_candidates_fast
from ..search.query_mode import resolve_search_mode
from ..search.runtime import search_query_intent
from ..search.pipeline import (
    rank_album_candidates,
    rank_artist_candidates,
    rank_track_candidates,
    summarize_ranked_results,
)
from .snapshot_builder import build_home_candidate_snapshot, trim_home_candidate_snapshot
from .artifact_runtime import (
    build_home_launch_artifacts as _artifact_build_home_launch_artifacts,
    store_home_serving_artifacts as _artifact_store_home_serving_artifacts,
)
from .row_runtime import build_rows_v41
from .precompute_stats import (
    stats_increment as _stats_increment,
    stats_set as _stats_set,
    stats_snapshot as _stats_snapshot,
)
from .precompute_store import (
    _home_cache_key,
    _home_profile_cache_key,
    _is_fresh,
    _search_cache_key,
    _search_profile_cache_key,
    _store_delete,
    _store_get,
    _store_set,
    configure_precompute_store,
    get_home_heavy_artifact as _store_get_home_heavy_artifact,
    get_home_heavy_artifact_for_profile as _store_get_home_heavy_artifact_for_profile,
    get_home_launch_artifact as _store_get_home_launch_artifact,
    get_home_launch_artifact_for_profile as _store_get_home_launch_artifact_for_profile,
    get_home_snapshot as _store_get_home_snapshot,
    get_home_snapshot_for_profile as _store_get_home_snapshot_for_profile,
    get_search_snapshot as _store_get_search_snapshot,
    get_search_snapshot_for_profile as _store_get_search_snapshot_for_profile,
    invalidate_home_snapshots,
    invalidate_user,
    invalidate_user_query,
)
from .store_runtime import open_recommendation_store_connection, resolve_server
from .warmup_runtime import schedule_profile_feature_warmup


_PRECOMPUTE_ENABLED = (os.environ.get("AURALIS_PRECOMPUTE_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"})
_PRECOMPUTE_HOME_USERS_LIMIT = max(
    2,
    int(os.environ.get("AURALIS_PRECOMPUTE_HOME_USERS_LIMIT", "24")),
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
_PRECOMPUTE_HOME_ARTIFACT_SERVE_MAX_AGE_SECONDS = max(
    _PRECOMPUTE_MAX_AGE_SECONDS,
    int(
        os.environ.get(
            "AURALIS_PRECOMPUTE_HOME_ARTIFACT_SERVE_MAX_AGE_SECONDS",
            "172800",
        )
    ),
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
_HOME_SNAPSHOT_VERSION = "home_candidate_snapshot_v1"
_HOME_ARTIFACT_VERSION = "home_launch_artifact_v4"
_HOME_HEAVY_ARTIFACT_VERSION = "home_heavy_rows_artifact_v3"
_HOME_HEAVY_ROW_KINDS = {"recommended_artists", "recommended_albums"}
_HOME_ARTIFACT_STORE_NAMESPACE = "precompute_home_artifact_v1"
_HOME_PRIMARY_ROW_KINDS = {
    "continue_listening",
    "because_you_played",
    "trending_for_you",
    "quiet_picks",
}
_HOME_THIN_PRIMARY_ROW_KINDS = {
    "continue_listening",
    "because_you_played",
}

configure_precompute_store(
    ttl_seconds=_PRECOMPUTE_TTL_SECONDS,
    max_age_seconds=_PRECOMPUTE_MAX_AGE_SECONDS,
    home_artifact_serve_max_age_seconds=_PRECOMPUTE_HOME_ARTIFACT_SERVE_MAX_AGE_SECONDS,
    home_snapshot_version=_HOME_SNAPSHOT_VERSION,
    home_artifact_version=_HOME_ARTIFACT_VERSION,
    home_heavy_artifact_version=_HOME_HEAVY_ARTIFACT_VERSION,
    artifact_store_namespace=_HOME_ARTIFACT_STORE_NAMESPACE,
)

get_home_launch_artifact = partial(
    _store_get_home_launch_artifact,
    stats_increment=_stats_increment,
)
get_home_launch_artifact_for_profile = partial(
    _store_get_home_launch_artifact_for_profile,
    stats_increment=_stats_increment,
)
get_home_heavy_artifact = partial(
    _store_get_home_heavy_artifact,
    stats_increment=_stats_increment,
)
get_home_heavy_artifact_for_profile = partial(
    _store_get_home_heavy_artifact_for_profile,
    stats_increment=_stats_increment,
)
get_home_snapshot = partial(
    _store_get_home_snapshot,
    stats_increment=_stats_increment,
)
get_home_snapshot_for_profile = partial(
    _store_get_home_snapshot_for_profile,
    stats_increment=_stats_increment,
)
get_search_snapshot = partial(
    _store_get_search_snapshot,
    stats_increment=_stats_increment,
)
get_search_snapshot_for_profile = partial(
    _store_get_search_snapshot_for_profile,
    stats_increment=_stats_increment,
)

def _profile_feature_versions(
    profile: Dict[str, Any] | None,
    *,
    feature_artifacts: Dict[str, Any] | None = None,
) -> Dict[str, str]:
    prof = dict(profile or {})
    artifacts = dict(feature_artifacts or {})
    return {
        "catalog_feature_version": prof.get("catalog_feature_version")
        or artifacts.get("catalog_feature_version")
        or "",
        "taste_profile_version": prof.get("taste_profile_version")
        or artifacts.get("taste_profile_version")
        or "",
        "scene_graph_version": prof.get("scene_graph_version")
        or artifacts.get("scene_graph_version")
        or "",
        "feature_source": prof.get("feature_source")
        or artifacts.get("feature_source")
        or "",
    }

def _trim_search_candidates(candidate_map: Dict[str, Dict[str, Any]], cap: int) -> Dict[str, Dict[str, Any]]:
    ranked: List[Tuple[float, str, Dict[str, Any]]] = []
    for entity_id, entry in (candidate_map or {}).items():
        payload = (entry or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        source_scores = dict((entry or {}).get("source_scores") or {})
        score = max(source_scores.values(), default=0.0) + (0.12 * max(len(source_scores) - 1, 0))
        ranked.append((float(score), entity_id, {"payload": dict(payload), "source_scores": source_scores}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    output: Dict[str, Dict[str, Any]] = {}
    for _score, entity_id, entry in ranked[:cap]:
        output[entity_id] = entry
    return output


def _snapshot_tracks(candidate_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    for pool in dict((candidate_snapshot or {}).get("pools") or {}).values():
        for candidate in list(pool or [])[:32]:
            if not isinstance(candidate, dict):
                continue
            track = candidate.get("track") if isinstance(candidate.get("track"), dict) else None
            if isinstance(track, dict):
                tracks.append(dict(track))
    return tracks


def _retrieval_payload_tracks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    for entry in dict(payload.get("track_candidates") or {}).values():
        candidate_payload = (entry or {}).get("payload")
        if isinstance(candidate_payload, dict):
            tracks.append(dict(candidate_payload))
    return tracks


def _active_user_scopes(server: Any, *, limit: int) -> List[str]:
    connection = open_recommendation_store_connection(server)
    ranked: Dict[str, float] = {}
    now = time.time()
    try:
        event_rows = connection.execute(
            """
            SELECT user_scope_id, COUNT(*) AS c, MAX(occurred_at) AS last_at
            FROM recommendation_events
            GROUP BY user_scope_id
            ORDER BY last_at DESC
            LIMIT ?
            """,
            [max(limit * 3, 24)],
        ).fetchall()
        search_rows = connection.execute(
            """
            SELECT user_scope_id, COUNT(*) AS c, MAX(occurred_at) AS last_at
            FROM recommendation_search_events
            GROUP BY user_scope_id
            ORDER BY last_at DESC
            LIMIT ?
            """,
            [max(limit * 3, 24)],
        ).fetchall()
        artifact_rows = connection.execute(
            """
            SELECT payload_json, updated_at
            FROM recommendation_feature_store
            WHERE namespace = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            [_HOME_ARTIFACT_STORE_NAMESPACE, max(limit * 6, 36)],
        ).fetchall()
    finally:
        connection.close()
    for row in list(event_rows) + list(search_rows):
        user_scope_id = server._assistant_safe_scope_id(row["user_scope_id"] or "guest")
        if not user_scope_id:
            continue
        count_score = min(float(row["c"] or 0), 240.0) * 0.05
        recency_hours = max((now - float(row["last_at"] or 0.0)) / 3600.0, 0.0)
        recency_score = max(0.0, 4.5 - min(recency_hours, 96.0) * 0.05)
        ranked[user_scope_id] = max(ranked.get(user_scope_id, 0.0), count_score + recency_score)
    for row in artifact_rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        user_scope_id = server._assistant_safe_scope_id(payload.get("user_scope_id") or "guest")
        if not user_scope_id:
            continue
        recency_hours = max((now - float(row["updated_at"] or 0.0)) / 3600.0, 0.0)
        recency_score = max(0.0, 3.5 - min(recency_hours, 168.0) * 0.02)
        ranked[user_scope_id] = max(ranked.get(user_scope_id, 0.0), 0.6 + recency_score)
    ordered = [
        user_scope_id
        for user_scope_id, _score in sorted(
            ranked.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    if "guest" not in ordered:
        ordered.append("guest")
    return ordered[:max(1, limit)]


def _top_queries_for_user(server: Any, *, user_scope_id: str, limit: int) -> List[str]:
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
        if not query:
            continue
        if query.lower() in {existing.lower() for existing in queries}:
            continue
        queries.append(query)
        if len(queries) >= limit:
            break
    return queries

def build_home_snapshot(
    *,
    server: Any,
    user_scope_id: str,
    force: bool = False,
    profile: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    key = _home_cache_key(normalized_scope)
    if not force:
        cached = _store_get(key)
        if _is_fresh(cached):
            _stats_increment("home_cache_hits")
            return dict(cached or {})
    _stats_increment("home_cache_misses")
    req = RecommendationHomeV3Request(
        query="",
        user_scope_id=normalized_scope,
        limit=18,
        force_refresh=bool(force),
    )
    if not isinstance(profile, dict) or not profile:
        _legacy_req, profile = build_home_profile(req)
    if bool((profile.get("profile_runtime") or {}).get("cache_hit")):
        _stats_increment("home_profile_cache_hits")
    else:
        _stats_increment("home_profiles_warmed")
    candidate_snapshot = trim_home_candidate_snapshot(
        server,
        build_home_candidate_snapshot(
            server=server,
            profile=profile,
        ),
    )
    schedule_profile_feature_warmup(
        server=server,
        warmup_key=f"profile_features:home:{normalized_scope}",
        profile=profile,
        extra_tracks=_snapshot_tracks(candidate_snapshot),
        extra_artists=list(candidate_snapshot.get("artists") or []),
        extra_albums=list(candidate_snapshot.get("albums") or []),
    )
    now = time.time()
    feature_versions = _profile_feature_versions(profile)
    snapshot = {
        "snapshot_version": _HOME_SNAPSHOT_VERSION,
        "user_scope_id": normalized_scope,
        "generated_at": now,
        "expires_at": now + _PRECOMPUTE_TTL_SECONDS,
        "candidate_snapshot": candidate_snapshot,
        "profile_summary": {
            "profile_key": profile.get("profile_key") or "",
            "recent_track_ids": list(profile.get("recent_track_ids") or [])[:12],
            "top_track_ids": list(profile.get("top_track_ids") or [])[:12],
            "recent_queries": list(profile.get("recent_queries") or [])[:8],
            "artist_hints": list(profile.get("artist_hints") or [])[:8],
            "model_id": ((profile.get("collaborative") or {}).get("model_id") or ""),
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": feature_versions.get("catalog_feature_version") or "",
            "taste_profile_version": feature_versions.get("taste_profile_version") or "",
            "scene_graph_version": feature_versions.get("scene_graph_version") or "",
            "feature_source": feature_versions.get("feature_source") or "",
        },
        "builder_mode": "nearline_candidate_precompute_v3",
        "feature_artifacts": feature_versions,
    }
    _store_set(key, snapshot, _PRECOMPUTE_TTL_SECONDS)
    profile_key = server._recommendation_trim_text(profile.get("profile_key"))
    if profile_key:
        _store_set(
            _home_profile_cache_key(profile_key),
            snapshot,
            _PRECOMPUTE_TTL_SECONDS,
        )
    _stats_increment("home_snapshots_built")
    return snapshot


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
    if bool((profile.get("profile_runtime") or {}).get("cache_hit")):
        _stats_increment("search_profile_cache_hits")
    else:
        _stats_increment("search_profiles_warmed")
    effective_search_mode = resolve_search_mode(
        normalized_query,
        normalize_text_fn=server._normalize_text,
        intent_hint=search_query_intent(normalized_query, server=server),
        explicit_mode=search_mode,
    )
    setattr(req, "search_mode", effective_search_mode)
    if legacy_req is not None:
        setattr(legacy_req, "search_mode", effective_search_mode)
    use_rich_retrieval = bool(force) or effective_search_mode in {"entity", "taste"}
    if use_rich_retrieval:
        payload = retrieve_search_candidates(
            legacy_req,
            profile,
            limit=_PRECOMPUTE_SEARCH_LIMIT,
            server=server,
        )
    else:
        payload = retrieve_search_candidates_fast(
            legacy_req,
            profile,
            limit=_PRECOMPUTE_SEARCH_LIMIT,
            server=server,
        )
    trimmed_payload = {
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
            for item in (payload.get("normalized_anchor_artists") or set())
            if str(item)
        ),
        "retriever_counts": dict(payload.get("retriever_counts") or {}),
        "retrieval_diagnostics": dict(payload.get("retrieval_diagnostics") or {}),
    }
    ranked_tracks = rank_track_candidates(
        server,
        req,
        profile,
        trimmed_payload,
        limit=_PRECOMPUTE_SEARCH_LIMIT,
    )
    ranked_artists = rank_artist_candidates(
        server,
        req,
        profile,
        trimmed_payload,
        limit=max(1, min(12, _PRECOMPUTE_SEARCH_LIMIT)),
    )
    ranked_albums = rank_album_candidates(
        server,
        req,
        profile,
        trimmed_payload,
        limit=max(1, min(12, _PRECOMPUTE_SEARCH_LIMIT)),
    )
    ranking_summary = summarize_ranked_results(
        server,
        tracks=ranked_tracks[:_PRECOMPUTE_SEARCH_LIMIT],
        artists=ranked_artists[: max(1, min(8, _PRECOMPUTE_SEARCH_LIMIT))],
        albums=ranked_albums[: max(1, min(8, _PRECOMPUTE_SEARCH_LIMIT))],
    )
    schedule_profile_feature_warmup(
        server=server,
        warmup_key=f"profile_features:search:{normalized_scope}:{normalized_query.lower()}",
        profile=profile,
        extra_tracks=_retrieval_payload_tracks(trimmed_payload),
        extra_artists=[
            (entry or {}).get("payload")
            for entry in dict(trimmed_payload.get("artist_candidates") or {}).values()
            if isinstance((entry or {}).get("payload"), dict)
        ],
        extra_albums=[
            (entry or {}).get("payload")
            for entry in dict(trimmed_payload.get("album_candidates") or {}).values()
            if isinstance((entry or {}).get("payload"), dict)
        ],
    )
    now = time.time()
    feature_versions = _profile_feature_versions(profile)
    snapshot = {
        "user_scope_id": normalized_scope,
        "query": normalized_query,
        "generated_at": now,
        "expires_at": now + _PRECOMPUTE_TTL_SECONDS,
        "retrieval_payload": trimmed_payload,
        "ranked_results": {
            "tracks": ranked_tracks[:_PRECOMPUTE_SEARCH_LIMIT],
            "artists": ranked_artists[: max(1, min(8, _PRECOMPUTE_SEARCH_LIMIT))],
            "albums": ranked_albums[: max(1, min(8, _PRECOMPUTE_SEARCH_LIMIT))],
            "ranking_summary": ranking_summary,
        },
        "profile_summary": {
            "profile_key": profile.get("profile_key") or "",
            "recent_track_ids": list(profile.get("recent_track_ids") or [])[:12],
            "top_track_ids": list(profile.get("top_track_ids") or [])[:12],
            "recent_queries": list(profile.get("recent_queries") or [])[:8],
            "artist_hints": list(profile.get("artist_hints") or [])[:8],
            "model_id": ((profile.get("collaborative") or {}).get("model_id") or ""),
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": feature_versions.get("catalog_feature_version") or "",
            "taste_profile_version": feature_versions.get("taste_profile_version") or "",
            "scene_graph_version": feature_versions.get("scene_graph_version") or "",
            "feature_source": feature_versions.get("feature_source") or "",
        },
        "search_mode": effective_search_mode,
        "builder_mode": "nearline_precompute_v2",
        "feature_artifacts": feature_versions,
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


def store_home_serving_artifacts(
    *,
    server: Any | None = None,
    user_scope_id: str,
    profile_key: str,
    rows: List[Dict[str, Any]],
    candidate_snapshot: Dict[str, Any] | None = None,
    diagnostics: Dict[str, Any] | None = None,
    row_diagnostics: Dict[str, Dict[str, Any]] | None = None,
    source_signature: str = "",
) -> Dict[str, Any]:
    return _artifact_store_home_serving_artifacts(
        server=server,
        user_scope_id=user_scope_id,
        profile_key=profile_key,
        rows=rows,
        candidate_snapshot=candidate_snapshot,
        diagnostics=diagnostics,
        row_diagnostics=row_diagnostics,
        source_signature=source_signature,
        ttl_seconds=_PRECOMPUTE_TTL_SECONDS,
        heavy_row_kinds=_HOME_HEAVY_ROW_KINDS,
        primary_row_kinds=_HOME_PRIMARY_ROW_KINDS,
        thin_primary_row_kinds=_HOME_THIN_PRIMARY_ROW_KINDS,
        stats_increment=_stats_increment,
    )


def build_home_launch_artifacts(
    *,
    server: Any,
    user_scope_id: str,
    force: bool = False,
    profile: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return _artifact_build_home_launch_artifacts(
        server=server,
        user_scope_id=user_scope_id,
        force=force,
        profile=profile,
        snapshot_builder=build_home_snapshot,
        rows_builder=build_rows_v41,
        artifact_store=store_home_serving_artifacts,
    )


def run_precompute_cycle(*, server: Any | None = None, force: bool = False) -> Dict[str, Any]:
    if not _PRECOMPUTE_ENABLED:
        return {
            "enabled": False,
            "home_built": 0,
            "search_built": 0,
            "home_users": [],
            "search_queries": {},
        }
    srv = resolve_server(server)
    started_at = time.time()
    _stats_set(
        last_cycle_started_at=started_at,
        last_cycle_status="running",
        last_cycle_error="",
    )
    home_built = 0
    search_built = 0
    home_users: List[str] = []
    search_queries: Dict[str, List[str]] = {}
    try:
        home_users = _active_user_scopes(
            srv,
            limit=_PRECOMPUTE_HOME_USERS_LIMIT,
        )
        for user_scope_id in home_users:
            try:
                build_home_launch_artifacts(
                    server=srv,
                    user_scope_id=user_scope_id,
                    force=force,
                )
                home_built += 1
            except Exception:
                continue
        search_users = home_users[:_PRECOMPUTE_SEARCH_USERS_LIMIT]
        for user_scope_id in search_users:
            top_queries = _top_queries_for_user(
                srv,
                user_scope_id=user_scope_id,
                limit=_PRECOMPUTE_SEARCH_QUERIES_PER_USER,
            )
            if not top_queries:
                continue
            search_queries[user_scope_id] = list(top_queries)
            for query in top_queries:
                try:
                    snapshot = build_search_snapshot(
                        server=srv,
                        user_scope_id=user_scope_id,
                        query=query,
                        force=force,
                    )
                    if snapshot is not None:
                        search_built += 1
                except Exception:
                    continue
        completed_at = time.time()
        result = {
            "enabled": True,
            "home_built": home_built,
            "search_built": search_built,
            "home_users": home_users,
            "search_queries": search_queries,
            "duration_ms": int((completed_at - started_at) * 1000),
            "ttl_seconds": _PRECOMPUTE_TTL_SECONDS,
        }
        _stats_set(
            last_cycle_completed_at=completed_at,
            last_cycle_status="success",
            last_cycle_result=result,
        )
        return result
    except Exception as exc:
        completed_at = time.time()
        result = {
            "enabled": True,
            "home_built": home_built,
            "search_built": search_built,
            "home_users": home_users,
            "search_queries": search_queries,
            "duration_ms": int((completed_at - started_at) * 1000),
            "error": str(exc)[:320],
        }
        _stats_set(
            last_cycle_completed_at=completed_at,
            last_cycle_status="failed",
            last_cycle_error=str(exc)[:320],
            last_cycle_result=result,
        )
        return result


def runtime_snapshot() -> Dict[str, Any]:
    snapshot = _stats_snapshot()
    snapshot["enabled"] = _PRECOMPUTE_ENABLED
    snapshot["ttl_seconds"] = _PRECOMPUTE_TTL_SECONDS
    snapshot["max_age_seconds"] = _PRECOMPUTE_MAX_AGE_SECONDS
    snapshot["home_users_limit"] = _PRECOMPUTE_HOME_USERS_LIMIT
    snapshot["search_users_limit"] = _PRECOMPUTE_SEARCH_USERS_LIMIT
    snapshot["queries_per_user"] = _PRECOMPUTE_SEARCH_QUERIES_PER_USER
    return snapshot
