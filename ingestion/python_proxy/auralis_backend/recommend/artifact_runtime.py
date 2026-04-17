from __future__ import annotations

from typing import Any, Callable, Dict, List, Set

from ..contracts import RecommendationHomeV3Request
from ..domain.features import build_home_profile
from .precompute_store import (
    _build_home_artifact_payload,
    _home_heavy_cache_key,
    _home_heavy_profile_cache_key,
    _home_heavy_usable_cache_key,
    _home_heavy_usable_profile_cache_key,
    _home_launch_acceptable_cache_key,
    _home_launch_acceptable_profile_cache_key,
    _home_launch_cache_key,
    _home_launch_last_good_cache_key,
    _home_launch_last_good_profile_cache_key,
    _home_launch_profile_cache_key,
    _home_launch_usable_cache_key,
    _home_launch_usable_profile_cache_key,
    _should_replace_acceptible_artifact,
    _store_get,
    _store_set,
)
from .quality import (
    acceptable_launch_artifact,
    artifact_repetition_reasons,
    promote_artifact_status,
    split_rows_by_kind,
    summarize_row_status,
)
from .store_runtime import resolve_server


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
    ttl_seconds: int,
    heavy_row_kinds: Set[str],
    primary_row_kinds: Set[str],
    thin_primary_row_kinds: Set[str],
    stats_increment: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    srv = resolve_server(server)
    normalized_scope = srv._assistant_safe_scope_id(user_scope_id or "guest")
    normalized_profile_key = srv._recommendation_trim_text(profile_key)
    builder_mode = str((diagnostics or {}).get("row_builder_mode") or "").strip().lower()
    effective_heavy_row_kinds = set() if "thin_core" in builder_mode else set(heavy_row_kinds or ())
    effective_primary_row_kinds = (
        set(thin_primary_row_kinds or ())
        if "thin_core" in builder_mode
        else set(primary_row_kinds or ())
    )
    launch_rows, heavy_rows = split_rows_by_kind(
        rows,
        heavy_row_kinds=effective_heavy_row_kinds,
    )
    row_status = summarize_row_status(row_diagnostics)
    quality_reasons = list((diagnostics or {}).get("snapshot_quality_reasons") or [])
    quality_reasons.extend(
        reason
        for reason in artifact_repetition_reasons(launch_rows)
        if reason not in quality_reasons
    )
    for row_kind in effective_primary_row_kinds:
        status = row_status.get(row_kind, "")
        reason = f"{row_kind}:{status or 'missing'}"
        if status != "emitted" and reason not in quality_reasons:
            quality_reasons.append(reason)
    promotion_status = promote_artifact_status(
        row_status,
        launch_rows,
        quality_reasons,
        primary_row_kinds=effective_primary_row_kinds,
        builder_mode=builder_mode,
    )
    launch_acceptable = acceptable_launch_artifact(
        row_status,
        launch_rows,
        quality_reasons,
        builder_mode=builder_mode,
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
    launch_promoted_key = _home_launch_cache_key(normalized_scope)
    launch_usable_key = _home_launch_usable_cache_key(normalized_scope)
    launch_acceptable_key = _home_launch_acceptable_cache_key(normalized_scope)
    launch_last_good_key = _home_launch_last_good_cache_key(normalized_scope)
    heavy_promoted_key = _home_heavy_cache_key(normalized_scope)
    heavy_usable_key = _home_heavy_usable_cache_key(normalized_scope)
    if promotion_status == "promoted":
        _store_set(launch_promoted_key, launch_artifact, ttl_seconds, server=srv)
        _store_set(launch_usable_key, launch_artifact, ttl_seconds, server=srv)
        if stats_increment is not None:
            stats_increment("home_launch_artifacts_built")
    elif promotion_status == "usable":
        _store_set(launch_usable_key, launch_artifact, ttl_seconds, server=srv)
        if stats_increment is not None:
            stats_increment("home_launch_artifacts_built")
    if launch_acceptable:
        existing_acceptable = _store_get(launch_acceptable_key, server=srv)
        if _should_replace_acceptible_artifact(existing_acceptable, launch_artifact):
            _store_set(launch_acceptable_key, launch_artifact, ttl_seconds, server=srv)
        existing_last_good = _store_get(launch_last_good_key, server=srv)
        if _should_replace_acceptible_artifact(existing_last_good, launch_artifact):
            _store_set(launch_last_good_key, launch_artifact, ttl_seconds, server=srv)
    if heavy_promotion_status == "promoted":
        _store_set(heavy_promoted_key, heavy_artifact, ttl_seconds, server=srv)
        _store_set(heavy_usable_key, heavy_artifact, ttl_seconds, server=srv)
        if stats_increment is not None:
            stats_increment("home_heavy_artifacts_built")
    elif heavy_promotion_status == "usable":
        _store_set(heavy_usable_key, heavy_artifact, ttl_seconds, server=srv)
        if stats_increment is not None:
            stats_increment("home_heavy_artifacts_built")
    if normalized_profile_key:
        launch_profile_promoted_key = _home_launch_profile_cache_key(normalized_profile_key)
        launch_profile_usable_key = _home_launch_usable_profile_cache_key(normalized_profile_key)
        launch_profile_acceptable_key = _home_launch_acceptable_profile_cache_key(normalized_profile_key)
        launch_profile_last_good_key = _home_launch_last_good_profile_cache_key(normalized_profile_key)
        heavy_profile_promoted_key = _home_heavy_profile_cache_key(normalized_profile_key)
        heavy_profile_usable_key = _home_heavy_usable_profile_cache_key(normalized_profile_key)
        if promotion_status == "promoted":
            _store_set(launch_profile_promoted_key, launch_artifact, ttl_seconds, server=srv)
            _store_set(launch_profile_usable_key, launch_artifact, ttl_seconds, server=srv)
        elif promotion_status == "usable":
            _store_set(launch_profile_usable_key, launch_artifact, ttl_seconds, server=srv)
        if launch_acceptable:
            existing_profile_acceptable = _store_get(launch_profile_acceptable_key, server=srv)
            if _should_replace_acceptible_artifact(existing_profile_acceptable, launch_artifact):
                _store_set(launch_profile_acceptable_key, launch_artifact, ttl_seconds, server=srv)
            existing_profile_last_good = _store_get(launch_profile_last_good_key, server=srv)
            if _should_replace_acceptible_artifact(existing_profile_last_good, launch_artifact):
                _store_set(launch_profile_last_good_key, launch_artifact, ttl_seconds, server=srv)
        if heavy_promotion_status == "promoted":
            _store_set(heavy_profile_promoted_key, heavy_artifact, ttl_seconds, server=srv)
            _store_set(heavy_profile_usable_key, heavy_artifact, ttl_seconds, server=srv)
        elif heavy_promotion_status == "usable":
            _store_set(heavy_profile_usable_key, heavy_artifact, ttl_seconds, server=srv)
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
    snapshot_builder: Callable[..., Dict[str, Any]],
    rows_builder: Callable[..., Any],
    artifact_store: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_scope = server._assistant_safe_scope_id(user_scope_id or "guest")
    req = RecommendationHomeV3Request(
        query="",
        user_scope_id=normalized_scope,
        limit=18,
        force_refresh=bool(force),
    )
    resolved_profile = profile
    if not isinstance(resolved_profile, dict) or not resolved_profile:
        _legacy_req, resolved_profile = build_home_profile(req)
    snapshot = snapshot_builder(
        server=server,
        user_scope_id=normalized_scope,
        force=bool(force),
        profile=resolved_profile,
    )
    rows, _generator_timings, row_diagnostics, row_builder_meta = rows_builder(
        server=server,
        profile=resolved_profile,
        precompute_snapshot=snapshot,
        trace=None,
        allow_live_snapshot_build=False,
    )
    diagnostics = {
        "profile_build_ms": 0,
        "row_assembly_ms": 0,
        "row_status": dict(row_diagnostics or {}),
        "row_builder_mode": row_builder_meta.get("row_builder_mode") or "",
        "launch_tier_only": bool(row_builder_meta.get("launch_tier_only")),
        "candidate_snapshot_source": row_builder_meta.get("candidate_snapshot_source") or "",
        "candidate_pool_counts": dict(row_builder_meta.get("candidate_pool_counts") or {}),
        "candidate_stage_timings_ms": dict(row_builder_meta.get("candidate_stage_timings_ms") or {}),
        "deferred_row_kinds": list(row_builder_meta.get("deferred_row_kinds") or []),
        "deferred_rows_pending": bool(row_builder_meta.get("deferred_row_kinds") or []),
        "metadata_enrich_limit": int(row_builder_meta.get("metadata_enrich_limit") or -1),
        "catalog_feature_version": resolved_profile.get("catalog_feature_version") or "",
        "taste_profile_version": resolved_profile.get("taste_profile_version") or "",
        "scene_graph_version": resolved_profile.get("scene_graph_version") or "",
        "feature_source": resolved_profile.get("feature_source") or "",
    }
    return artifact_store(
        server=server,
        user_scope_id=normalized_scope,
        profile_key=resolved_profile.get("profile_key") or "",
        rows=rows,
        candidate_snapshot=dict(snapshot.get("candidate_snapshot") or {}),
        diagnostics=diagnostics,
        row_diagnostics=row_diagnostics,
        source_signature=(
            resolved_profile.get("profile_key")
            or (snapshot.get("profile_summary") or {}).get("profile_key")
            or normalized_scope
        ),
    )
