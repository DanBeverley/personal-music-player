from __future__ import annotations

from hashlib import sha1
from typing import Any, Dict, List
import json
import time

from ..storage.session_store import get_session_store
from .quality import artifact_quality_score
from .store_runtime import open_recommendation_store_connection, resolve_server


_PRECOMPUTE_TTL_SECONDS = 900
_PRECOMPUTE_MAX_AGE_SECONDS = 21600
_HOME_ARTIFACT_VERSION = "home_launch_artifact_v4"
_HOME_HEAVY_ARTIFACT_VERSION = "home_heavy_rows_artifact_v2"
_HOME_ROW_CONTRACT_VERSION = "home_row_contract_v2"
_HOME_ARTIFACT_STORE_NAMESPACE = "precompute_home_artifact_v1"


def configure_precompute_store(
    *,
    ttl_seconds: int,
    max_age_seconds: int,
    home_artifact_version: str,
    home_heavy_artifact_version: str,
    artifact_store_namespace: str,
) -> None:
    global _PRECOMPUTE_TTL_SECONDS
    global _PRECOMPUTE_MAX_AGE_SECONDS
    global _HOME_ARTIFACT_VERSION
    global _HOME_HEAVY_ARTIFACT_VERSION
    global _HOME_ARTIFACT_STORE_NAMESPACE
    _PRECOMPUTE_TTL_SECONDS = int(ttl_seconds)
    _PRECOMPUTE_MAX_AGE_SECONDS = int(max_age_seconds)
    _HOME_ARTIFACT_VERSION = str(home_artifact_version or _HOME_ARTIFACT_VERSION)
    _HOME_HEAVY_ARTIFACT_VERSION = str(
        home_heavy_artifact_version or _HOME_HEAVY_ARTIFACT_VERSION
    )
    _HOME_ARTIFACT_STORE_NAMESPACE = str(
        artifact_store_namespace or _HOME_ARTIFACT_STORE_NAMESPACE
    )


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


def _home_launch_last_good_cache_key(user_scope_id: str) -> str:
    return f"auralis:precompute:home_launch_last_good:{user_scope_id}"


def _home_launch_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_profile:{digest}"


def _home_launch_usable_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_usable_profile:{digest}"


def _home_launch_acceptable_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_acceptable_profile:{digest}"


def _home_launch_last_good_profile_cache_key(profile_key: str) -> str:
    digest = sha1(profile_key.encode("utf-8")).hexdigest()
    return f"auralis:precompute:home_launch_last_good_profile:{digest}"


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


def _store_get(key: str, *, server: Any | None = None) -> Dict[str, Any] | None:
    try:
        payload = get_session_store().get(key)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        if _is_persistent_artifact_key(key) and not _artifact_version_matches(key, payload):
            _store_delete(key, server=server)
            return None
        return payload
    if not _is_persistent_artifact_key(key):
        return None
    persisted = _persistent_artifact_get(key, server=server)
    if not isinstance(persisted, dict):
        return None
    if not _artifact_version_matches(key, persisted):
        _persistent_artifact_delete(key, server=server)
        return None
    try:
        get_session_store().set(key, persisted, _PRECOMPUTE_TTL_SECONDS)
    except Exception:
        pass
    return persisted


def _store_set(
    key: str,
    payload: Dict[str, Any],
    ttl_seconds: int,
    *,
    server: Any | None = None,
) -> None:
    try:
        get_session_store().set(key, payload, ttl_seconds)
    except Exception:
        pass
    if _is_persistent_artifact_key(key):
        _persistent_artifact_set(key, payload, server=server)


def _store_delete(key: str, *, server: Any | None = None) -> None:
    try:
        get_session_store().delete(key)
    except Exception:
        pass
    if _is_persistent_artifact_key(key):
        _persistent_artifact_delete(key, server=server)


def _is_persistent_artifact_key(key: str) -> bool:
    normalized = str(key or "")
    return normalized.startswith(
        (
            "auralis:precompute:home_launch:",
            "auralis:precompute:home_launch_usable:",
            "auralis:precompute:home_launch_acceptable:",
            "auralis:precompute:home_launch_last_good:",
            "auralis:precompute:home_launch_profile:",
            "auralis:precompute:home_launch_usable_profile:",
            "auralis:precompute:home_launch_acceptable_profile:",
            "auralis:precompute:home_launch_last_good_profile:",
            "auralis:precompute:home_heavy:",
            "auralis:precompute:home_heavy_usable:",
            "auralis:precompute:home_heavy_profile:",
            "auralis:precompute:home_heavy_usable_profile:",
        )
    )


def _expected_artifact_version_for_key(key: str) -> str:
    normalized = str(key or "")
    if any(
        normalized.startswith(prefix)
        for prefix in (
            "auralis:precompute:home_heavy:",
            "auralis:precompute:home_heavy_usable:",
            "auralis:precompute:home_heavy_profile:",
            "auralis:precompute:home_heavy_usable_profile:",
        )
    ):
        return _HOME_HEAVY_ARTIFACT_VERSION
    if _is_persistent_artifact_key(normalized):
        return _HOME_ARTIFACT_VERSION
    return ""


def _artifact_version_matches(key: str, payload: Dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    expected_version = _expected_artifact_version_for_key(key)
    if not expected_version:
        return True
    actual_version = str(payload.get("artifact_version") or "").strip()
    if actual_version != expected_version:
        return False
    if _is_persistent_artifact_key(key):
        return (
            str(payload.get("row_contract_version") or "").strip()
            == _HOME_ROW_CONTRACT_VERSION
        )
    return True


def _persistent_artifact_get(key: str, *, server: Any | None = None) -> Dict[str, Any] | None:
    server = resolve_server(server)
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


def _persistent_artifact_set(
    key: str,
    payload: Dict[str, Any],
    *,
    server: Any | None = None,
) -> None:
    server = resolve_server(server)
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


def _persistent_artifact_delete(key: str, *, server: Any | None = None) -> None:
    server = resolve_server(server)
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
    version = (
        _HOME_HEAVY_ARTIFACT_VERSION
        if artifact_kind == "heavy"
        else _HOME_ARTIFACT_VERSION
    )
    return {
        "artifact_kind": artifact_kind,
        "artifact_version": version,
        "row_contract_version": _HOME_ROW_CONTRACT_VERSION,
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
    last_good_key: str = "",
    include_usable: bool = False,
    hit_stat: str = "",
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    candidates: List[tuple[int, float, float, Dict[str, Any]]] = []

    def _push_candidate(key: str, label: str, priority: int) -> None:
        if not key:
            return
        snapshot = _store_get(key, server=server)
        if _is_fresh(snapshot):
            payload = dict(snapshot or {})
            payload["stale"] = False
            payload["resolved_from"] = label
            candidates.append(
                (
                    priority,
                    -float(payload.get("quality_score") or 0.0),
                    -_snapshot_generated_at(payload),
                    payload,
                )
            )
        elif _is_stale(snapshot):
            payload = dict(snapshot or {})
            payload["stale"] = True
            payload["resolved_from"] = f"{label}_stale"
            candidates.append(
                (
                    priority + 10,
                    -float(payload.get("quality_score") or 0.0),
                    -_snapshot_generated_at(payload),
                    payload,
                )
            )

    _push_candidate(promoted_key, "promoted", 0)
    if include_usable:
        _push_candidate(usable_key, "usable", 1)
        _push_candidate(last_good_key, "last_good", 2)
        _push_candidate(acceptable_key, "acceptable", 3)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    if hit_stat and stats_increment is not None:
        stats_increment(hit_stat)
    return dict(candidates[0][3])


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


def _snapshot_lookup(
    key: str,
    *,
    resolved_from: str,
    hit_stat: str = "",
    miss_stat: str = "",
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    snapshot = _store_get(key, server=server)
    if _is_fresh(snapshot):
        if hit_stat and stats_increment is not None:
            stats_increment(hit_stat)
        payload = dict(snapshot or {})
        payload["resolved_from"] = resolved_from
        payload["stale"] = False
        return payload
    if _is_stale(snapshot):
        if hit_stat and stats_increment is not None:
            stats_increment(hit_stat)
        payload = dict(snapshot or {})
        payload["resolved_from"] = f"{resolved_from}_stale"
        payload["stale"] = True
        return payload
    if miss_stat and stats_increment is not None:
        stats_increment(miss_stat)
    return None


def get_home_launch_artifact(
    *,
    user_scope_id: str,
    include_usable: bool = False,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    return _artifact_lookup(
        _home_launch_cache_key(normalized_scope),
        _home_launch_usable_cache_key(normalized_scope),
        acceptable_key=_home_launch_acceptable_cache_key(normalized_scope),
        last_good_key=_home_launch_last_good_cache_key(normalized_scope),
        include_usable=include_usable,
        hit_stat="home_launch_artifact_hits",
        stats_increment=stats_increment,
        server=srv,
    )


def get_home_launch_artifact_for_profile(
    *,
    profile_key: str,
    include_usable: bool = False,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_key = srv._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    return _artifact_lookup(
        _home_launch_profile_cache_key(normalized_key),
        _home_launch_usable_profile_cache_key(normalized_key),
        acceptable_key=_home_launch_acceptable_profile_cache_key(normalized_key),
        last_good_key=_home_launch_last_good_profile_cache_key(normalized_key),
        include_usable=include_usable,
        hit_stat="home_launch_artifact_hits",
        stats_increment=stats_increment,
        server=srv,
    )


def get_home_heavy_artifact(
    *,
    user_scope_id: str,
    include_usable: bool = False,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    return _artifact_lookup(
        _home_heavy_cache_key(normalized_scope),
        _home_heavy_usable_cache_key(normalized_scope),
        include_usable=include_usable,
        hit_stat="home_heavy_artifact_hits",
        stats_increment=stats_increment,
        server=srv,
    )


def get_home_heavy_artifact_for_profile(
    *,
    profile_key: str,
    include_usable: bool = False,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_key = srv._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    return _artifact_lookup(
        _home_heavy_profile_cache_key(normalized_key),
        _home_heavy_usable_profile_cache_key(normalized_key),
        include_usable=include_usable,
        hit_stat="home_heavy_artifact_hits",
        stats_increment=stats_increment,
        server=srv,
    )


def get_home_snapshot(
    *,
    user_scope_id: str,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    return _snapshot_lookup(
        _home_cache_key(normalized_scope),
        resolved_from="user_scope",
        hit_stat="home_cache_hits",
        miss_stat="home_cache_misses",
        stats_increment=stats_increment,
        server=srv,
    )


def get_home_snapshot_for_profile(
    *,
    profile_key: str,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_key = srv._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    return _snapshot_lookup(
        _home_profile_cache_key(normalized_key),
        resolved_from="profile_key",
        hit_stat="home_profile_snapshot_hits",
        miss_stat="home_profile_snapshot_misses",
        stats_increment=stats_increment,
        server=srv,
    )


def get_search_snapshot(
    *,
    user_scope_id: str,
    query: str,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = srv._recommendation_trim_text(query)
    if not normalized_query:
        return None
    return _snapshot_lookup(
        _search_cache_key(normalized_scope, normalized_query),
        resolved_from="user_scope",
        hit_stat="search_cache_hits",
        miss_stat="search_cache_misses",
        stats_increment=stats_increment,
        server=srv,
    )


def get_search_snapshot_for_profile(
    *,
    profile_key: str,
    query: str,
    stats_increment=None,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    srv = resolve_server(server)
    normalized_key = srv._recommendation_trim_text(profile_key)
    normalized_query = srv._recommendation_trim_text(query)
    if not normalized_key or not normalized_query:
        return None
    return _snapshot_lookup(
        _search_profile_cache_key(normalized_key, normalized_query),
        resolved_from="profile_key",
        hit_stat="search_profile_snapshot_hits",
        miss_stat="search_profile_snapshot_misses",
        stats_increment=stats_increment,
        server=srv,
    )


def invalidate_user(
    user_scope_id: str,
    *,
    include_artifacts: bool = True,
    server: Any | None = None,
) -> None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    _store_delete(_home_cache_key(normalized_scope), server=srv)
    if include_artifacts:
        _store_delete(_home_launch_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_usable_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_acceptable_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_last_good_cache_key(normalized_scope), server=srv)
        _store_delete(_home_heavy_cache_key(normalized_scope), server=srv)
        _store_delete(_home_heavy_usable_cache_key(normalized_scope), server=srv)


def invalidate_home_snapshots(
    *,
    user_scope_id: str,
    profile_key: str = "",
    include_artifacts: bool = True,
    server: Any | None = None,
) -> None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    _store_delete(_home_cache_key(normalized_scope), server=srv)
    if include_artifacts:
        _store_delete(_home_launch_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_usable_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_acceptable_cache_key(normalized_scope), server=srv)
        _store_delete(_home_launch_last_good_cache_key(normalized_scope), server=srv)
        _store_delete(_home_heavy_cache_key(normalized_scope), server=srv)
        _store_delete(_home_heavy_usable_cache_key(normalized_scope), server=srv)
    normalized_profile_key = srv._recommendation_trim_text(profile_key)
    if normalized_profile_key:
        _store_delete(_home_profile_cache_key(normalized_profile_key), server=srv)
        if include_artifacts:
            _store_delete(_home_launch_profile_cache_key(normalized_profile_key), server=srv)
            _store_delete(_home_launch_usable_profile_cache_key(normalized_profile_key), server=srv)
            _store_delete(_home_launch_acceptable_profile_cache_key(normalized_profile_key), server=srv)
            _store_delete(_home_launch_last_good_profile_cache_key(normalized_profile_key), server=srv)
            _store_delete(_home_heavy_profile_cache_key(normalized_profile_key), server=srv)
            _store_delete(_home_heavy_usable_profile_cache_key(normalized_profile_key), server=srv)


def invalidate_user_query(
    user_scope_id: str,
    query: str,
    *,
    server: Any | None = None,
) -> None:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_query = srv._recommendation_trim_text(query)
    if not normalized_query:
        return
    _store_delete(_search_cache_key(normalized_scope, normalized_query), server=srv)
