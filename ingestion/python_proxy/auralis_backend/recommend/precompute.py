from __future__ import annotations

from hashlib import sha1
from threading import Lock
from typing import Any, Dict, List, Tuple
import json
import os
import time

from ..contracts import RecommendationHomeV3Request, SearchV3Request
from ..domain.features import build_home_profile, build_search_profile
from ..legacy import get_server
from ..storage.session_store import get_session_store
from ..domain.retrieval import retrieve_search_candidates, retrieve_search_candidates_fast
from ..search.pipeline import (
    rank_album_candidates,
    rank_artist_candidates,
    rank_track_candidates,
    summarize_ranked_results,
)
from .home_pipeline import build_home_candidate_snapshot, trim_home_candidate_snapshot
from .quality import (
    acceptable_launch_artifact,
    artifact_repetition_reasons,
    artifact_quality_score,
    promote_artifact_status,
    split_rows_by_kind,
    summarize_row_status,
)
from .row_runtime import build_rows_v41
from .store_runtime import open_recommendation_store_connection
from .taste_runtime import warm_profile_feature_artifacts


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
_HOME_ARTIFACT_VERSION = "home_launch_artifact_v1"
_HOME_HEAVY_ARTIFACT_VERSION = "home_heavy_rows_artifact_v1"
_HOME_HEAVY_ROW_KINDS = {"recommended_artists", "recommended_albums"}
_HOME_ARTIFACT_STORE_NAMESPACE = "precompute_home_artifact_v1"
_HOME_PRIMARY_ROW_KINDS = {
    "continue_listening",
    "because_you_played",
    "trending_for_you",
    "quiet_picks",
}


_runtime_lock = Lock()
_runtime_stats: Dict[str, Any] = {
    "last_cycle_started_at": 0.0,
    "last_cycle_completed_at": 0.0,
    "last_cycle_status": "idle",
    "last_cycle_error": "",
    "home_profiles_warmed": 0,
    "search_profiles_warmed": 0,
    "home_profile_cache_hits": 0,
    "search_profile_cache_hits": 0,
    "home_profile_snapshot_hits": 0,
    "home_profile_snapshot_misses": 0,
    "search_profile_snapshot_hits": 0,
    "search_profile_snapshot_misses": 0,
    "home_snapshots_built": 0,
    "search_snapshots_built": 0,
    "home_launch_artifacts_built": 0,
    "home_heavy_artifacts_built": 0,
    "home_launch_artifact_hits": 0,
    "home_heavy_artifact_hits": 0,
    "home_cache_hits": 0,
    "search_cache_hits": 0,
    "home_cache_misses": 0,
    "search_cache_misses": 0,
    "last_cycle_result": {},
}

_warmup_lock = Lock()
_inflight_warmups: set[str] = set()


def _stats_increment(name: str, amount: int = 1) -> None:
    with _runtime_lock:
        _runtime_stats[name] = int(_runtime_stats.get(name) or 0) + int(amount)


def _stats_set(**kwargs: Any) -> None:
    with _runtime_lock:
        for key, value in kwargs.items():
            _runtime_stats[key] = value


def _stats_snapshot() -> Dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime_stats)


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


def _schedule_profile_feature_warmup(
    *,
    server: Any,
    warmup_key: str,
    profile: Dict[str, Any],
    extra_tracks: List[Dict[str, Any]] | None = None,
    extra_artists: List[Dict[str, Any]] | None = None,
    extra_albums: List[Dict[str, Any]] | None = None,
) -> bool:
    if not _PRECOMPUTE_ENABLED:
        return False
    with _warmup_lock:
        if warmup_key in _inflight_warmups:
            return False
        _inflight_warmups.add(warmup_key)

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
            with _warmup_lock:
                _inflight_warmups.discard(warmup_key)

    try:
        getattr(server, "search_executor", server.recommendation_executor).submit(_warm)
        return True
    except Exception:
        with _warmup_lock:
            _inflight_warmups.discard(warmup_key)
        return False


def _home_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home:{user_scope_id}"


def _home_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_profile:{digest}"


def _home_launch_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_launch:{user_scope_id}"


def _home_launch_usable_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_launch_usable:{user_scope_id}"


def _home_launch_acceptable_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_launch_acceptable:{user_scope_id}"


def _home_launch_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_profile:{digest}"


def _home_launch_usable_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_usable_profile:{digest}"


def _home_launch_acceptable_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_acceptable_profile:{digest}"


def _home_heavy_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_heavy:{user_scope_id}"


def _home_heavy_usable_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_heavy_usable:{user_scope_id}"


def _home_heavy_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_heavy_profile:{digest}"


def _home_heavy_usable_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_heavy_usable_profile:{digest}"


def _search_cache_key(user_scope_id: str, query: str) -> str:
    digest = sha1(query.lower().encode("utf-8")).hexdigest()
    return f"auralis:precompute:search:{user_scope_id}:{digest}"


def _search_profile_cache_key(profile_key: str, query: str) -> str:
    digest = sha1(f"{profile_key.lower()}||{query.lower()}".encode("utf-8")).hexdigest()
    return f"auralis:precompute:search_profile:{digest}"


def _store_get(key: str) -> Dict[str, Any] | None:
    try:
        payload = get_session_store().get(key)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload
    if not _is_persistent_artifact_key(key):
        return None
    persisted = _persistent_artifact_get(key)
    if not isinstance(persisted, dict):
        return None
    try:
        get_session_store().set(key, persisted, _PRECOMPUTE_TTL_SECONDS)
    except Exception:
        pass
    return persisted


def _store_set(key: str, payload: Dict[str, Any], ttl_seconds: int) -> None:
    try:
        get_session_store().set(key, payload, ttl_seconds)
    except Exception:
        pass
    if _is_persistent_artifact_key(key):
        _persistent_artifact_set(key, payload)


def _store_delete(key: str) -> None:
    try:
        get_session_store().delete(key)
    except Exception:
        pass
    if _is_persistent_artifact_key(key):
        _persistent_artifact_delete(key)


def _is_persistent_artifact_key(key: str) -> bool:
    normalized = str(key or "")
    return normalized.startswith(
        (
            "auralis:precompute:home_launch:",
            "auralis:precompute:home_launch_usable:",
            "auralis:precompute:home_launch_acceptable:",
            "auralis:precompute:home_launch_profile:",
            "auralis:precompute:home_launch_usable_profile:",
            "auralis:precompute:home_launch_acceptable_profile:",
            "auralis:precompute:home_heavy:",
            "auralis:precompute:home_heavy_usable:",
            "auralis:precompute:home_heavy_profile:",
            "auralis:precompute:home_heavy_usable_profile:",
        )
    )


def _persistent_artifact_get(key: str) -> Dict[str, Any] | None:
    server = get_server()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            """
            SELECT payload_json
            FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [_HOME_ARTIFACT_STORE_NAMESPACE, key],
        ).fetchone()
    except Exception:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _persistent_artifact_set(key: str, payload: Dict[str, Any]) -> None:
    server = get_server()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
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
                _HOME_ARTIFACT_STORE_NAMESPACE,
                key,
                str((payload or {}).get("artifact_version") or ""),
                json.dumps(payload or {}, ensure_ascii=False),
                time.time(),
            ],
        )
        connection.commit()
    except Exception:
        return
    finally:
        connection.close()


def _persistent_artifact_delete(key: str) -> None:
    server = get_server()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    try:
        connection.execute(
            """
            DELETE FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [_HOME_ARTIFACT_STORE_NAMESPACE, key],
        )
        connection.commit()
    except Exception:
        return
    finally:
        connection.close()


def _is_fresh(snapshot: Dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    return float(snapshot.get("expires_at") or 0.0) > time.time()


def _snapshot_generated_at(snapshot: Dict[str, Any] | None) -> float:
    if not isinstance(snapshot, dict):
        return 0.0
    generated_at = float(snapshot.get("generated_at") or 0.0)
    if generated_at > 0.0:
        return generated_at
    expires_at = float(snapshot.get("expires_at") or 0.0)
    if expires_at > 0.0:
        return max(expires_at - float(_PRECOMPUTE_TTL_SECONDS), 0.0)
    return 0.0


def _is_usable(snapshot: Dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    generated_at = _snapshot_generated_at(snapshot)
    if generated_at <= 0.0:
        return False
    return (time.time() - generated_at) <= float(_PRECOMPUTE_MAX_AGE_SECONDS)


def _is_stale(snapshot: Dict[str, Any] | None) -> bool:
    return _is_usable(snapshot) and not _is_fresh(snapshot)


def _build_home_artifact_payload(
    *,
    artifact_kind: str,
    user_scope_id: str,
    profile_key: str,
    rows: List[Dict[str, Any]],
    candidate_snapshot: Dict[str, Any] | None,
    diagnostics: Dict[str, Any] | None,
    row_status: Dict[str, str],
    promotion_status: str,
    quality_reasons: List[str],
    source_signature: str,
) -> Dict[str, Any]:
    now = time.time()
    ttl_seconds = _PRECOMPUTE_TTL_SECONDS
    version = _HOME_HEAVY_ARTIFACT_VERSION if artifact_kind == "heavy" else _HOME_ARTIFACT_VERSION
    return {
        "artifact_kind": artifact_kind,
        "artifact_version": version,
        "user_scope_id": user_scope_id,
        "profile_key": profile_key,
        "generated_at": now,
        "expires_at": now + ttl_seconds,
        "rows": list(rows or []),
        "candidate_snapshot": (
            dict(candidate_snapshot or {})
            if artifact_kind == "launch" and isinstance(candidate_snapshot, dict)
            else {}
        ),
        "row_status": dict(row_status or {}),
        "diagnostics": dict(diagnostics or {}),
        "promotion_status": promotion_status,
        "quality_reasons": list(quality_reasons or []),
        "quality_score": artifact_quality_score(row_status, quality_reasons),
        "source_signature": source_signature,
    }


def _artifact_lookup(
    promoted_key: str,
    usable_key: str,
    *,
    acceptable_key: str = "",
    include_usable: bool = False,
    hit_stat: str = "",
) -> Dict[str, Any] | None:
    promoted = _store_get(promoted_key)
    if _is_fresh(promoted):
        if hit_stat:
            _stats_increment(hit_stat)
        payload = dict(promoted or {})
        payload["stale"] = False
        payload["resolved_from"] = "promoted"
        return payload
    if _is_stale(promoted):
        if hit_stat:
            _stats_increment(hit_stat)
        payload = dict(promoted or {})
        payload["stale"] = True
        payload["resolved_from"] = "promoted_stale"
        return payload
    if not include_usable:
        return None
    usable = _store_get(usable_key)
    if _is_fresh(usable):
        if hit_stat:
            _stats_increment(hit_stat)
        payload = dict(usable or {})
        payload["stale"] = False
        payload["resolved_from"] = "usable"
        return payload
    if _is_stale(usable):
        if hit_stat:
            _stats_increment(hit_stat)
        payload = dict(usable or {})
        payload["stale"] = True
        payload["resolved_from"] = "usable_stale"
        return payload
    if acceptable_key:
        acceptable = _store_get(acceptable_key)
        if _is_fresh(acceptable):
            if hit_stat:
                _stats_increment(hit_stat)
            payload = dict(acceptable or {})
            payload["stale"] = False
            payload["resolved_from"] = "acceptable"
            return payload
        if _is_stale(acceptable):
            if hit_stat:
                _stats_increment(hit_stat)
            payload = dict(acceptable or {})
            payload["stale"] = True
            payload["resolved_from"] = "acceptable_stale"
            return payload
    return None


def _should_replace_acceptible_artifact(
    existing_artifact: Dict[str, Any] | None,
    next_artifact: Dict[str, Any] | None,
) -> bool:
    if not isinstance(next_artifact, dict):
        return False
    if not isinstance(existing_artifact, dict):
        return True
    existing_score = float(existing_artifact.get("quality_score") or 0.0)
    next_score = float(next_artifact.get("quality_score") or 0.0)
    if next_score > existing_score + 0.02:
        return True
    if next_score + 0.05 < existing_score and _is_usable(existing_artifact):
        return False
    return float(next_artifact.get("generated_at") or 0.0) >= float(
        existing_artifact.get("generated_at") or 0.0
    )


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
    _schedule_profile_feature_warmup(
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
        limit=_PRECOMPUTE_SEARCH_LIMIT,
        force_refresh=bool(force),
    )
    if legacy_req is None or not isinstance(profile, dict) or not profile:
        legacy_req, profile = build_search_profile(req)
    if bool((profile.get("profile_runtime") or {}).get("cache_hit")):
        _stats_increment("search_profile_cache_hits")
    else:
        _stats_increment("search_profiles_warmed")
    if bool(force):
        payload = retrieve_search_candidates(
            legacy_req,
            profile,
            limit=_PRECOMPUTE_SEARCH_LIMIT,
        )
    else:
        payload = retrieve_search_candidates_fast(
            legacy_req,
            profile,
            limit=_PRECOMPUTE_SEARCH_LIMIT,
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
    _schedule_profile_feature_warmup(
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
    user_scope_id: str,
    profile_key: str,
    rows: List[Dict[str, Any]],
    candidate_snapshot: Dict[str, Any] | None = None,
    diagnostics: Dict[str, Any] | None = None,
    row_diagnostics: Dict[str, Dict[str, Any]] | None = None,
    source_signature: str = "",
) -> Dict[str, Any]:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_profile_key = server._recommendation_trim_text(profile_key)
    launch_rows, heavy_rows = split_rows_by_kind(
        rows,
        heavy_row_kinds=_HOME_HEAVY_ROW_KINDS,
    )
    row_status = summarize_row_status(row_diagnostics)
    quality_reasons = list((diagnostics or {}).get("snapshot_quality_reasons") or [])
    quality_reasons.extend(
        reason
        for reason in artifact_repetition_reasons(launch_rows)
        if reason not in quality_reasons
    )
    for row_kind in _HOME_PRIMARY_ROW_KINDS:
        status = row_status.get(row_kind, "")
        reason = f"{row_kind}:{status or 'missing'}"
        if status != "emitted" and reason not in quality_reasons:
            quality_reasons.append(reason)
    promotion_status = promote_artifact_status(
        row_status,
        launch_rows,
        quality_reasons,
        primary_row_kinds=_HOME_PRIMARY_ROW_KINDS,
    )
    launch_acceptable = acceptable_launch_artifact(
        row_status,
        launch_rows,
        quality_reasons,
    )
    heavy_promotion_status = (
        promotion_status
        if heavy_rows and promotion_status in {"promoted", "usable"}
        else "rejected"
    )
    launch_artifact = _build_home_artifact_payload(
        artifact_kind="launch",
        user_scope_id=normalized_scope,
        profile_key=normalized_profile_key,
        rows=launch_rows,
        candidate_snapshot=candidate_snapshot,
        diagnostics=diagnostics,
        row_status=row_status,
        promotion_status=promotion_status,
        quality_reasons=quality_reasons,
        source_signature=source_signature,
    )
    heavy_artifact = _build_home_artifact_payload(
        artifact_kind="heavy",
        user_scope_id=normalized_scope,
        profile_key=normalized_profile_key,
        rows=heavy_rows,
        candidate_snapshot=None,
        diagnostics=diagnostics,
        row_status=row_status,
        promotion_status=heavy_promotion_status,
        quality_reasons=quality_reasons if heavy_promotion_status != "promoted" else [],
        source_signature=source_signature,
    )
    ttl_seconds = _PRECOMPUTE_TTL_SECONDS
    launch_promoted_key = _home_launch_cache_key(normalized_scope)
    launch_usable_key = _home_launch_usable_cache_key(normalized_scope)
    launch_acceptable_key = _home_launch_acceptable_cache_key(normalized_scope)
    heavy_promoted_key = _home_heavy_cache_key(normalized_scope)
    heavy_usable_key = _home_heavy_usable_cache_key(normalized_scope)
    if promotion_status == "promoted":
        _store_set(launch_promoted_key, launch_artifact, ttl_seconds)
        _store_set(launch_usable_key, launch_artifact, ttl_seconds)
        _stats_increment("home_launch_artifacts_built")
    elif promotion_status == "usable":
        _store_set(launch_usable_key, launch_artifact, ttl_seconds)
        _stats_increment("home_launch_artifacts_built")
    if launch_acceptable:
        existing_acceptable = _store_get(launch_acceptable_key)
        if _should_replace_acceptible_artifact(existing_acceptable, launch_artifact):
            _store_set(launch_acceptable_key, launch_artifact, ttl_seconds)
    if heavy_promotion_status == "promoted":
        _store_set(heavy_promoted_key, heavy_artifact, ttl_seconds)
        _store_set(heavy_usable_key, heavy_artifact, ttl_seconds)
        _stats_increment("home_heavy_artifacts_built")
    elif heavy_promotion_status == "usable":
        _store_set(heavy_usable_key, heavy_artifact, ttl_seconds)
        _stats_increment("home_heavy_artifacts_built")
    if normalized_profile_key:
        launch_profile_promoted_key = _home_launch_profile_cache_key(normalized_profile_key)
        launch_profile_usable_key = _home_launch_usable_profile_cache_key(normalized_profile_key)
        launch_profile_acceptable_key = _home_launch_acceptable_profile_cache_key(normalized_profile_key)
        heavy_profile_promoted_key = _home_heavy_profile_cache_key(normalized_profile_key)
        heavy_profile_usable_key = _home_heavy_usable_profile_cache_key(normalized_profile_key)
        if promotion_status == "promoted":
            _store_set(launch_profile_promoted_key, launch_artifact, ttl_seconds)
            _store_set(launch_profile_usable_key, launch_artifact, ttl_seconds)
        elif promotion_status == "usable":
            _store_set(launch_profile_usable_key, launch_artifact, ttl_seconds)
        if launch_acceptable:
            existing_profile_acceptable = _store_get(launch_profile_acceptable_key)
            if _should_replace_acceptible_artifact(existing_profile_acceptable, launch_artifact):
                _store_set(launch_profile_acceptable_key, launch_artifact, ttl_seconds)
        if heavy_promotion_status == "promoted":
            _store_set(heavy_profile_promoted_key, heavy_artifact, ttl_seconds)
            _store_set(heavy_profile_usable_key, heavy_artifact, ttl_seconds)
        elif heavy_promotion_status == "usable":
            _store_set(heavy_profile_usable_key, heavy_artifact, ttl_seconds)
    return {
        "launch_artifact": launch_artifact,
        "heavy_artifact": heavy_artifact,
        "promotion_status": promotion_status,
        "launch_acceptable": launch_acceptable,
        "heavy_promotion_status": heavy_promotion_status,
        "quality_reasons": quality_reasons,
    }


def build_home_launch_artifacts(
    *,
    server: Any,
    user_scope_id: str,
    force: bool = False,
    profile: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    req = RecommendationHomeV3Request(
        query="",
        user_scope_id=normalized_scope,
        limit=18,
        force_refresh=bool(force),
    )
    if not isinstance(profile, dict) or not profile:
        _legacy_req, profile = build_home_profile(req)
    snapshot = build_home_snapshot(
        server=server,
        user_scope_id=normalized_scope,
        force=bool(force),
        profile=profile,
    )
    rows, _generator_timings, row_diagnostics, row_builder_meta = build_rows_v41(
        server=server,
        profile=profile,
        precompute_snapshot=snapshot,
        trace=None,
        allow_live_snapshot_build=False,
    )
    diagnostics = {
        "profile_build_ms": 0,
        "row_assembly_ms": 0,
        "row_status": dict(row_diagnostics or {}),
        "candidate_snapshot_source": row_builder_meta.get("candidate_snapshot_source") or "",
        "candidate_pool_counts": dict(row_builder_meta.get("candidate_pool_counts") or {}),
        "candidate_stage_timings_ms": dict(row_builder_meta.get("candidate_stage_timings_ms") or {}),
        "catalog_feature_version": profile.get("catalog_feature_version") or "",
        "taste_profile_version": profile.get("taste_profile_version") or "",
        "scene_graph_version": profile.get("scene_graph_version") or "",
        "feature_source": profile.get("feature_source") or "",
    }
    return store_home_serving_artifacts(
        user_scope_id=normalized_scope,
        profile_key=profile.get("profile_key") or "",
        rows=rows,
        candidate_snapshot=dict(snapshot.get("candidate_snapshot") or {}),
        diagnostics=diagnostics,
        row_diagnostics=row_diagnostics,
        source_signature=(
            profile.get("profile_key")
            or (snapshot.get("profile_summary") or {}).get("profile_key")
            or normalized_scope
        ),
    )


def get_home_launch_artifact(
    *,
    user_scope_id: str,
    include_usable: bool = False,
) -> Dict[str, Any] | None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    return _artifact_lookup(
        _home_launch_cache_key(normalized_scope),
        _home_launch_usable_cache_key(normalized_scope),
        acceptable_key=_home_launch_acceptable_cache_key(normalized_scope),
        include_usable=include_usable,
        hit_stat="home_launch_artifact_hits",
    )


def get_home_launch_artifact_for_profile(
    *,
    profile_key: str,
    include_usable: bool = False,
) -> Dict[str, Any] | None:
    server = get_server()
    normalized_key = server._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    return _artifact_lookup(
        _home_launch_profile_cache_key(normalized_key),
        _home_launch_usable_profile_cache_key(normalized_key),
        acceptable_key=_home_launch_acceptable_profile_cache_key(normalized_key),
        include_usable=include_usable,
        hit_stat="home_launch_artifact_hits",
    )


def get_home_heavy_artifact(
    *,
    user_scope_id: str,
    include_usable: bool = False,
) -> Dict[str, Any] | None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    return _artifact_lookup(
        _home_heavy_cache_key(normalized_scope),
        _home_heavy_usable_cache_key(normalized_scope),
        include_usable=include_usable,
        hit_stat="home_heavy_artifact_hits",
    )


def get_home_heavy_artifact_for_profile(
    *,
    profile_key: str,
    include_usable: bool = False,
) -> Dict[str, Any] | None:
    server = get_server()
    normalized_key = server._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    return _artifact_lookup(
        _home_heavy_profile_cache_key(normalized_key),
        _home_heavy_usable_profile_cache_key(normalized_key),
        include_usable=include_usable,
        hit_stat="home_heavy_artifact_hits",
    )


def get_home_snapshot(*, user_scope_id: str) -> Dict[str, Any] | None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    snapshot = _store_get(_home_cache_key(normalized_scope))
    if _is_fresh(snapshot):
        _stats_increment("home_cache_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "user_scope"
        payload["stale"] = False
        return payload
    if _is_stale(snapshot):
        _stats_increment("home_cache_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "user_scope_stale"
        payload["stale"] = True
        return payload
    _stats_increment("home_cache_misses")
    return None


def get_home_snapshot_for_profile(*, profile_key: str) -> Dict[str, Any] | None:
    server = get_server()
    normalized_key = server._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    snapshot = _store_get(_home_profile_cache_key(normalized_key))
    if _is_fresh(snapshot):
        _stats_increment("home_profile_snapshot_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "profile_key"
        payload["stale"] = False
        return payload
    if _is_stale(snapshot):
        _stats_increment("home_profile_snapshot_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "profile_key_stale"
        payload["stale"] = True
        return payload
    _stats_increment("home_profile_snapshot_misses")
    return None


def get_search_snapshot(*, user_scope_id: str, query: str) -> Dict[str, Any] | None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = server._recommendation_trim_text(query)
    if not normalized_query:
        return None
    snapshot = _store_get(_search_cache_key(normalized_scope, normalized_query))
    if _is_fresh(snapshot):
        _stats_increment("search_cache_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "user_scope"
        payload["stale"] = False
        return payload
    if _is_stale(snapshot):
        _stats_increment("search_cache_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "user_scope_stale"
        payload["stale"] = True
        return payload
    _stats_increment("search_cache_misses")
    return None


def get_search_snapshot_for_profile(
    *,
    profile_key: str,
    query: str,
) -> Dict[str, Any] | None:
    server = get_server()
    normalized_key = server._recommendation_trim_text(profile_key)
    normalized_query = server._recommendation_trim_text(query)
    if not normalized_key or not normalized_query:
        return None
    snapshot = _store_get(_search_profile_cache_key(normalized_key, normalized_query))
    if _is_fresh(snapshot):
        _stats_increment("search_profile_snapshot_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "profile_key"
        payload["stale"] = False
        return payload
    if _is_stale(snapshot):
        _stats_increment("search_profile_snapshot_hits")
        payload = dict(snapshot or {})
        payload["resolved_from"] = "profile_key_stale"
        payload["stale"] = True
        return payload
    _stats_increment("search_profile_snapshot_misses")
    return None


def invalidate_user(user_scope_id: str, *, include_artifacts: bool = True) -> None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    _store_delete(_home_cache_key(normalized_scope))
    if include_artifacts:
        _store_delete(_home_launch_cache_key(normalized_scope))
        _store_delete(_home_launch_usable_cache_key(normalized_scope))
        _store_delete(_home_launch_acceptable_cache_key(normalized_scope))
        _store_delete(_home_heavy_cache_key(normalized_scope))
        _store_delete(_home_heavy_usable_cache_key(normalized_scope))


def invalidate_home_snapshots(
    *,
    user_scope_id: str,
    profile_key: str = "",
    include_artifacts: bool = True,
) -> None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    _store_delete(_home_cache_key(normalized_scope))
    if include_artifacts:
        _store_delete(_home_launch_cache_key(normalized_scope))
        _store_delete(_home_launch_usable_cache_key(normalized_scope))
        _store_delete(_home_launch_acceptable_cache_key(normalized_scope))
        _store_delete(_home_heavy_cache_key(normalized_scope))
        _store_delete(_home_heavy_usable_cache_key(normalized_scope))
    normalized_profile_key = server._recommendation_trim_text(profile_key)
    if normalized_profile_key:
        _store_delete(_home_profile_cache_key(normalized_profile_key))
        if include_artifacts:
            _store_delete(_home_launch_profile_cache_key(normalized_profile_key))
            _store_delete(_home_launch_usable_profile_cache_key(normalized_profile_key))
            _store_delete(_home_launch_acceptable_profile_cache_key(normalized_profile_key))
            _store_delete(_home_heavy_profile_cache_key(normalized_profile_key))
            _store_delete(_home_heavy_usable_profile_cache_key(normalized_profile_key))


def invalidate_user_query(user_scope_id: str, query: str) -> None:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = server._recommendation_trim_text(query)
    if not normalized_query:
        return
    _store_delete(_search_cache_key(normalized_scope, normalized_query))


def schedule_search_warmup(*, user_scope_id: str, query: str) -> bool:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = server._recommendation_trim_text(query)
    if not _PRECOMPUTE_ENABLED or not normalized_query:
        return False
    warmup_key = f"search:{normalized_scope}:{normalized_query.lower()}"
    with _warmup_lock:
        if warmup_key in _inflight_warmups:
            return False
        _inflight_warmups.add(warmup_key)

    def _warm() -> None:
        try:
            build_search_snapshot(
                server=server,
                user_scope_id=normalized_scope,
                query=normalized_query,
                force=False,
            )
        except Exception:
            return
        finally:
            with _warmup_lock:
                _inflight_warmups.discard(warmup_key)

    try:
        server.recommendation_executor.submit(_warm)
        return True
    except Exception:
        with _warmup_lock:
            _inflight_warmups.discard(warmup_key)
        return False


def schedule_home_warmup(
    *,
    user_scope_id: str,
    profile: Dict[str, Any] | None = None,
    force: bool = False,
) -> bool:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    if not _PRECOMPUTE_ENABLED:
        return False
    warmup_key = f"home:{normalized_scope}"
    with _warmup_lock:
        if warmup_key in _inflight_warmups:
            return False
        _inflight_warmups.add(warmup_key)

    def _warm() -> None:
        try:
            build_home_snapshot(
                server=server,
                user_scope_id=normalized_scope,
                force=bool(force),
                profile=profile if isinstance(profile, dict) and profile else None,
            )
        except Exception:
            return
        finally:
            with _warmup_lock:
                _inflight_warmups.discard(warmup_key)

    try:
        server.recommendation_executor.submit(_warm)
        return True
    except Exception:
        with _warmup_lock:
            _inflight_warmups.discard(warmup_key)
        return False


def schedule_home_artifact_warmup(
    *,
    user_scope_id: str,
    profile: Dict[str, Any] | None = None,
    force: bool = False,
) -> bool:
    server = get_server()
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    if not _PRECOMPUTE_ENABLED:
        return False
    warmup_key = f"home_artifact:{normalized_scope}"
    with _warmup_lock:
        if warmup_key in _inflight_warmups:
            return False
        _inflight_warmups.add(warmup_key)

    def _warm() -> None:
        try:
            build_home_launch_artifacts(
                server=server,
                user_scope_id=normalized_scope,
                force=bool(force),
                profile=profile if isinstance(profile, dict) and profile else None,
            )
        except Exception:
            return
        finally:
            with _warmup_lock:
                _inflight_warmups.discard(warmup_key)

    try:
        server.recommendation_executor.submit(_warm)
        return True
    except Exception:
        with _warmup_lock:
            _inflight_warmups.discard(warmup_key)
        return False


def run_precompute_cycle(*, server: Any | None = None, force: bool = False) -> Dict[str, Any]:
    if not _PRECOMPUTE_ENABLED:
        return {
            "enabled": False,
            "home_built": 0,
            "search_built": 0,
            "home_users": [],
            "search_queries": {},
        }
    srv = server or get_server()
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
