from __future__ import annotations

import os
import time
import uuid
from typing import Any
from typing import Dict, List, Tuple

from fastapi import HTTPException

from ..domain.features import build_home_profile
from .home_pipeline import (
    build_continue_listening_row,
    build_home_candidate_snapshot,
    build_home_candidate_snapshot_fast_fallback,
    extend_row_from_snapshot,
    refresh_trending_by_genre_row,
    snapshot_substrate_mode,
    trim_home_candidate_snapshot,
)
from .policy import should_extend_row
from .precompute import (
    build_home_snapshot,
    get_home_heavy_artifact,
    get_home_heavy_artifact_for_profile,
    get_home_launch_artifact,
    get_home_launch_artifact_for_profile,
    get_home_snapshot,
    get_home_snapshot_for_profile,
    invalidate_home_snapshots,
    runtime_snapshot as precompute_runtime_snapshot,
    store_home_serving_artifacts,
)
from .feature_store import request_store_runtime
from .freshness_runtime import freshen_launch_rows, visible_impression_rows
from .quality import snapshot_quality_reasons as compute_snapshot_quality_reasons
from .admin_runtime import (
    evaluate_experiments as evaluate_experiments_runtime,
    experiments as experiments_runtime,
    interaction_event as interaction_event_runtime,
    model_status as model_status_runtime,
    model_versions as model_versions_runtime,
    recommended_artists as recommended_artists_runtime,
    search_interaction as search_interaction_runtime,
    train_model as train_model_runtime,
)
from .row_runtime import (
    QUALITY_CRITICAL_ROWS,
    build_rows_v41 as build_rows_for_snapshot,
    merge_home_rows,
)
from .row_registry import deferred_row_kinds as registry_deferred_row_kinds
from .session_runtime import load_feed_session, prune_feed_cache, store_feed_session
from .warmup_runtime import schedule_home_artifact_warmup, schedule_home_warmup

try:
    from ..storage.postgres import (
        activate_model_version,
        list_model_versions,
        list_rollout_events,
        rollback_model_version,
    )
except Exception:
    def list_model_versions(*, model_key: str, limit: int = 20):
        return []

    def activate_model_version(
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return False

    def rollback_model_version(
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return {"ok": False, "reason": "postgres_unavailable"}

    def list_rollout_events(*, model_key: str = "", limit: int = 50):
        return []


RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS = max(
    1.5,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS",
            "3.5",
        )
    ),
)
RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS = max(
    0.8,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS",
            "1.8",
        )
    ),
)
RECOMMEND_ROW_FINALIZE_BUDGET_SECONDS = max(
    1.5,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_FINALIZE_BUDGET_SECONDS",
            "3.2",
        )
    ),
)
RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS = max(
    RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS + 0.4,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS",
            "5.8",
        )
    ),
)
RECOMMEND_DISABLE_TIMEOUTS = (
    (os.environ.get("AURALIS_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
    or (os.environ.get("AURALIS_RECOMMEND_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
)
RECOMMEND_LIVE_SNAPSHOT_ON_MISS = (
    os.environ.get("AURALIS_RECOMMEND_LIVE_SNAPSHOT_ON_MISS", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)


def _snapshot_quality_is_weak(
    row_diagnostics: Dict[str, Dict[str, Any]] | None,
    *,
    critical_rows: tuple[str, ...] = ("continue_listening", "because_you_played", "trending_for_you", "quiet_picks"),
) -> bool:
    return bool(
        compute_snapshot_quality_reasons(
            row_diagnostics,
            critical_rows=critical_rows,
        )
    )


def _should_bootstrap_rich_snapshot(profile: Dict[str, Any]) -> bool:
    recent_depth = max(
        len(list(profile.get("recent_track_ids") or [])),
        len(list(profile.get("recent_track_snapshots") or [])),
        len(list(profile.get("last_played_tracks") or [])),
    )
    long_term_depth = max(
        len(list(profile.get("top_track_ids") or [])),
        len(list(profile.get("top_track_snapshots") or [])),
    )
    artist_depth = len(
        {
            str(value or "").strip().lower()
            for value in [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
                *(profile.get("listened_artists") or []),
            ]
            if str(value or "").strip()
        }
    )
    collaborative_ready = bool(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )
    anchor_depth = len(list(profile.get("anchor_track_snapshots") or []))
    library_depth = max(
        len(list(profile.get("library_track_ids") or [])),
        len(list(profile.get("offline_track_ids") or [])),
    )
    query_depth = max(
        len(list(profile.get("recent_queries") or [])),
        len(list(profile.get("taste_queries") or [])),
    )
    signal_score = 0
    if recent_depth >= 2:
        signal_score += 2
    elif recent_depth >= 1:
        signal_score += 1
    if long_term_depth >= 4:
        signal_score += 2
    elif long_term_depth >= 2:
        signal_score += 1
    if artist_depth >= 3:
        signal_score += 2
    elif artist_depth >= 2:
        signal_score += 1
    if collaborative_ready:
        signal_score += 2
    if anchor_depth >= 1:
        signal_score += 1
    if library_depth >= 8:
        signal_score += 1
    if query_depth >= 2:
        signal_score += 1
    return signal_score >= 3


def _candidate_snapshot_from_precompute(
    precompute_snapshot: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    if not isinstance(precompute_snapshot, dict):
        return None
    candidate_snapshot = precompute_snapshot.get("candidate_snapshot")
    if isinstance(candidate_snapshot, dict) and candidate_snapshot:
        return dict(candidate_snapshot)
    return None


def _precompute_snapshot_supports_rich_rows(
    precompute_snapshot: Dict[str, Any] | None,
) -> bool:
    candidate_snapshot = _candidate_snapshot_from_precompute(precompute_snapshot)
    if not isinstance(candidate_snapshot, dict) or not candidate_snapshot:
        return False
    return snapshot_substrate_mode(candidate_snapshot) == "rich_personalized"


def _launch_artifact_supports_rich_rows(
    launch_artifact: Dict[str, Any] | None,
) -> bool:
    if not isinstance(launch_artifact, dict):
        return False
    diagnostics = dict(launch_artifact.get("diagnostics") or {})
    row_builder_mode = str(diagnostics.get("row_builder_mode") or "").strip().lower()
    if "rich" in row_builder_mode:
        return True
    candidate_snapshot = launch_artifact.get("candidate_snapshot")
    if isinstance(candidate_snapshot, dict) and candidate_snapshot:
        return snapshot_substrate_mode(candidate_snapshot) == "rich_personalized"
    row_status = dict(launch_artifact.get("row_status") or {})
    deferred_rich_rows = (
        "todays_pick",
        "trending_by_genre",
        "trending_for_you",
        "quiet_picks",
        "deep_cuts",
    )
    return not any(
        str(row_status.get(row_kind) or "").strip().lower() == "deferred_substrate"
        for row_kind in deferred_rich_rows
    )


def _launch_artifact_meets_flagship_contract(
    launch_artifact: Dict[str, Any] | None,
) -> bool:
    if not isinstance(launch_artifact, dict):
        return False
    row_status = {
        str(row_kind or ""): str(status or "").strip().lower()
        for row_kind, status in dict(launch_artifact.get("row_status") or {}).items()
    }
    hard_missing = {
        "empty",
        "missing_no_fallback",
        "fallback_unavailable",
        "seed_pool_empty",
        "post_filter_empty",
        "finalize_filtered_out",
    }
    if row_status.get("todays_pick") in hard_missing:
        return False
    if row_status.get("trending_for_you") in hard_missing:
        return False
    if row_status.get("quiet_picks") in hard_missing:
        return False
    return True


def _build_rich_bootstrap_snapshot(
    *,
    server: Any,
    user_scope_id: str,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    candidate_snapshot = trim_home_candidate_snapshot(
        server,
        build_home_candidate_snapshot(
            server=server,
            profile=profile,
        ),
    )
    now = time.time()
    return {
        "user_scope_id": user_scope_id,
        "generated_at": now,
        "expires_at": now + 900,
        "candidate_snapshot": candidate_snapshot,
        "resolved_from": "request_bootstrap_live",
        "profile_summary": {
            "profile_key": profile.get("profile_key") or "",
        },
    }


def _recommendation_runtime_health(server: Any) -> Dict[str, Any]:
    runtime_snapshot_fn = getattr(server, "_recommendation_runtime_snapshot", None)
    runtime = {}
    if callable(runtime_snapshot_fn):
        try:
            runtime = dict(runtime_snapshot_fn() or {})
        except Exception:
            runtime = {}
    external_worker_expected = bool(
        runtime.get("external_worker_expected")
        if runtime
        else getattr(server, "RECOMMENDATION_EXTERNAL_WORKER", False)
    )
    worker_status = str(runtime.get("worker_status") or "").strip().lower()
    worker_mode = str(runtime.get("worker_mode") or "").strip().lower()
    worker_last_heartbeat_at = float(runtime.get("worker_last_heartbeat_at") or 0.0)
    heartbeat_age_seconds = (
        max(0.0, time.time() - worker_last_heartbeat_at)
        if worker_last_heartbeat_at > 0
        else 0.0
    )
    max_heartbeat_age_seconds = max(
        60.0,
        float(getattr(server, "RECOMMENDATION_SYNC_INTERVAL_SECONDS", 300)) * 2.0,
    )
    worker_healthy = (
        not external_worker_expected
        or (
            worker_status == "running"
            and worker_last_heartbeat_at > 0
            and heartbeat_age_seconds <= max_heartbeat_age_seconds
        )
    )
    return {
        "external_worker_expected": external_worker_expected,
        "worker_mode": worker_mode,
        "worker_status": worker_status,
        "worker_last_heartbeat_at": worker_last_heartbeat_at,
        "worker_heartbeat_age_seconds": round(heartbeat_age_seconds, 3),
        "worker_max_heartbeat_age_seconds": round(max_heartbeat_age_seconds, 3),
        "worker_healthy": worker_healthy,
        "external_worker_unhealthy": external_worker_expected and not worker_healthy,
        "scheduler_enabled": bool(runtime.get("scheduler_enabled") or getattr(server, "RECOMMENDATION_ENABLE_SCHEDULER", False)),
        "scheduler_last_error": str(runtime.get("last_scheduler_error") or "").strip(),
        "nearline_last_cycle_status": str(runtime.get("nearline_last_cycle_status") or "").strip(),
        "nearline_last_cycle_at": float(runtime.get("nearline_last_cycle_at") or 0.0),
    }


def _schedule_runtime_bootstrap(server: Any) -> bool:
    bootstrap_fn = getattr(server, "_start_recommendation_bootstrap_thread", None)
    if not callable(bootstrap_fn):
        return False
    try:
        return bool(bootstrap_fn())
    except Exception:
        return False


class RecommendationService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def _resolve_launch_artifact_policy(
        self,
        req,
        *,
        launch_artifact: Dict[str, Any],
        rich_launch_required: bool = False,
    ) -> Dict[str, Any]:
        resolved_from = str(launch_artifact.get("resolved_from") or "")
        stale_artifact = bool(launch_artifact.get("stale"))
        acceptable_artifact = resolved_from.startswith("acceptable")
        prefer_fresh_rows = bool(getattr(req, "prefer_fresh_rows", False))
        thin_launch_artifact = bool(
            rich_launch_required and not _launch_artifact_supports_rich_rows(launch_artifact)
        )
        should_schedule_refresh = bool(
            prefer_fresh_rows or stale_artifact or acceptable_artifact or thin_launch_artifact
        )
        should_bypass_artifact = bool(
            prefer_fresh_rows and (stale_artifact or acceptable_artifact or thin_launch_artifact)
        )
        artifact_bypass_reason = (
            "thin_launch_artifact"
            if thin_launch_artifact and should_bypass_artifact
            else (
                "stale_launch_artifact"
                if stale_artifact and should_bypass_artifact
                else ("acceptable_launch_artifact" if acceptable_artifact and should_bypass_artifact else "")
            )
        )
        return {
            "resolved_from": resolved_from,
            "stale_artifact": stale_artifact,
            "acceptable_artifact": acceptable_artifact,
            "thin_launch_artifact": thin_launch_artifact,
            "prefer_fresh_rows": prefer_fresh_rows,
            "should_schedule_refresh": should_schedule_refresh,
            "should_bypass_artifact": should_bypass_artifact,
            "artifact_bypass_reason": artifact_bypass_reason,
        }

    def _summarize_row_diagnostics(
        self,
        row_diagnostics: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        server = self._server
        row_source_counts = {}
        row_selected_source_counts = {}
        row_model_versions = {}
        for row_id, diagnostics in row_diagnostics.items():
            if not isinstance(diagnostics, dict):
                continue
            source_counts = diagnostics.get("source_counts")
            if isinstance(source_counts, dict) and source_counts:
                row_source_counts[row_id] = dict(source_counts)
            selected_source_counts = diagnostics.get("selected_source_counts")
            if isinstance(selected_source_counts, dict) and selected_source_counts:
                row_selected_source_counts[row_id] = dict(selected_source_counts)
            ranking_model_version = server._recommendation_trim_text(
                diagnostics.get("ranking_model_version")
            )
            if ranking_model_version:
                row_model_versions[row_id] = ranking_model_version
        return {
            "row_source_counts": row_source_counts,
            "row_selected_source_counts": row_selected_source_counts,
            "row_model_versions": row_model_versions,
        }

    def _load_precompute_snapshot(
        self,
        req,
        *,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        server = self._server
        profile_key = profile.get("profile_key") or ""
        precompute_snapshot = None
        precompute_hit = False
        precompute_cache_hit = False
        if bool(req.force_refresh):
            try:
                precompute_snapshot = build_home_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    force=True,
                    profile=profile,
                )
                if isinstance(precompute_snapshot, dict) and precompute_snapshot:
                    precompute_snapshot = dict(precompute_snapshot)
                    precompute_snapshot.setdefault(
                        "resolved_from",
                        "request_force_snapshot_build",
                    )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
            except Exception:
                precompute_snapshot = None
        else:
            precompute_snapshot = get_home_snapshot(
                user_scope_id=profile.get("user_scope_id") or "guest",
                server=server,
            )
            if isinstance(precompute_snapshot, dict):
                precompute_hit = bool(
                    precompute_snapshot.get("candidate_snapshot")
                    or precompute_snapshot.get("rows")
                )
                precompute_cache_hit = precompute_hit
            if not precompute_hit:
                precompute_snapshot = get_home_snapshot_for_profile(
                    profile_key=profile_key,
                    server=server,
                )
                if isinstance(precompute_snapshot, dict):
                    precompute_hit = bool(
                        (precompute_snapshot or {}).get("candidate_snapshot")
                    )
                    precompute_cache_hit = precompute_hit
        return {
            "precompute_snapshot": precompute_snapshot,
            "precompute_hit": precompute_hit,
            "precompute_cache_hit": precompute_cache_hit,
        }

    def _apply_precompute_snapshot_policy(
        self,
        *,
        req,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        worker_runtime: Dict[str, Any] | None = None,
        precompute_snapshot: Dict[str, Any] | None = None,
        precompute_hit: bool = False,
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = dict(worker_runtime or {})
        rich_launch_required = _should_bootstrap_rich_snapshot(profile)
        force_rich_rows = False
        runtime_bootstrap_scheduled = False
        rich_bootstrap_deferred = False
        rich_bootstrap_attempted = False
        rich_bootstrap_ms = 0
        allow_request_bootstrap = bool(getattr(req, "force_refresh", False))

        if (
            rich_launch_required
            and precompute_hit
            and not _precompute_snapshot_supports_rich_rows(precompute_snapshot)
        ):
            precompute_snapshot = None
            precompute_hit = False
            if bool(worker_runtime.get("external_worker_unhealthy")):
                rich_bootstrap_deferred = True
                runtime_bootstrap_scheduled = _schedule_runtime_bootstrap(server)
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.precompute_thin_deferred_external_worker_unhealthy",
                    True,
                )
            else:
                force_rich_rows = True
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.precompute_thin_rejected",
                    True,
                )

        if (
            not precompute_hit
            and rich_launch_required
            and bool(worker_runtime.get("external_worker_unhealthy"))
        ):
            rich_bootstrap_deferred = True
            runtime_bootstrap_scheduled = (
                runtime_bootstrap_scheduled or _schedule_runtime_bootstrap(server)
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.rich_bootstrap_deferred_external_worker_unhealthy",
                True,
            )
        elif (
            not precompute_hit
            and rich_launch_required
            and allow_request_bootstrap
            and not rich_bootstrap_deferred
        ):
            rich_bootstrap_attempted = True
            force_rich_rows = True
            try:
                rich_bootstrap_started_at = time.perf_counter()
                precompute_snapshot = _build_rich_bootstrap_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    profile=profile,
                )
                rich_bootstrap_ms = int(
                    (time.perf_counter() - rich_bootstrap_started_at) * 1000
                )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
                server._trace_stage(
                    trace,
                    "recommend.rich_bootstrap",
                    rich_bootstrap_started_at,
                )
                server._trace_put(
                    trace,
                    "timings_ms",
                    "recommend.rich_bootstrap_ms",
                    rich_bootstrap_ms,
                )
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.rich_bootstrap_snapshot",
                    bool(precompute_hit),
                )
            except Exception as exc:
                server._trace_put(
                    trace,
                    "errors",
                    "recommend.rich_bootstrap_error",
                    str(exc)[:240],
                )
                precompute_snapshot = None
        elif not precompute_hit and rich_launch_required:
            rich_bootstrap_deferred = True
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.rich_bootstrap_deferred_launch_policy",
                True,
            )

        return {
            "precompute_snapshot": precompute_snapshot,
            "precompute_hit": precompute_hit,
            "rich_launch_required": rich_launch_required,
            "force_rich_rows": force_rich_rows,
            "runtime_bootstrap_scheduled": runtime_bootstrap_scheduled,
            "rich_bootstrap_deferred": rich_bootstrap_deferred,
            "rich_bootstrap_attempted": rich_bootstrap_attempted,
            "rich_bootstrap_ms": rich_bootstrap_ms,
        }

    def _build_nearline_precompute_diagnostics(
        self,
        *,
        precompute_cache_hit: bool,
        precompute_hit: bool,
        precompute_stale: bool,
        precompute_snapshot: Dict[str, Any] | None,
        row_builder_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "hit": precompute_cache_hit,
            "snapshot_ready": precompute_hit,
            "stale": precompute_stale,
            "resolved_from": (precompute_snapshot or {}).get("resolved_from") or "",
            "snapshot_hit": bool(row_builder_meta.get("candidate_snapshot_hit")),
            "rows_seeded": len((precompute_snapshot or {}).get("rows") or {}),
            "candidate_pools_seeded": len(
                (row_builder_meta.get("candidate_pool_counts") or {})
            ),
            "snapshot_generated_at": float((precompute_snapshot or {}).get("generated_at") or 0.0),
            "snapshot_expires_at": float((precompute_snapshot or {}).get("expires_at") or 0.0),
            "candidate_snapshot_generated_at": float(
                row_builder_meta.get("candidate_snapshot_generated_at") or 0.0
            ),
            "candidate_snapshot_build_ms": int(
                row_builder_meta.get("candidate_snapshot_build_ms") or 0
            ),
            "candidate_snapshot_albums_count": int(
                row_builder_meta.get("candidate_snapshot_albums_count") or 0
            ),
            "runtime": precompute_runtime_snapshot(),
        }

    def _build_request_session_diagnostics(
        self,
        *,
        profile: Dict[str, Any],
        profile_ms: int,
        profile_key: str,
        worker_runtime: Dict[str, Any],
        precompute_state: Dict[str, Any],
        row_ms: int,
        generator_timings: Dict[str, int],
        row_diagnostics: Dict[str, Dict[str, Any]],
        row_builder_meta: Dict[str, Any],
        rows: List[Dict[str, Any]],
        started_at: float,
    ) -> Dict[str, Any]:
        server = self._server
        summary = self._summarize_row_diagnostics(row_diagnostics)
        row_item_counts = {
            row["id"]: len(row.get("items") or [])
            for row in rows
        }
        snapshot_quality_reasons = compute_snapshot_quality_reasons(
            row_diagnostics,
            critical_rows=QUALITY_CRITICAL_ROWS,
        )
        snapshot_cacheable = not bool(snapshot_quality_reasons)
        deferred_row_kinds = list(row_builder_meta.get("deferred_row_kinds") or [])
        home_ranking_model_key = "home_global_ranker_v4"
        home_ranking_model_version = server._ranking_model_version(home_ranking_model_key)
        precompute_snapshot = precompute_state["precompute_snapshot"]

        total_build_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "cache_hit": False,
            "ranking_backend": "embedding_profile",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "home_ranking_model_key": home_ranking_model_key,
            "home_ranking_model_version": home_ranking_model_version,
            "collaborative_model_ready": bool((profile.get("collaborative") or {}).get("model_ready")),
            "collaborative_model_type": (profile.get("collaborative") or {}).get("model_type") or "",
            "collaborative_model_id": (profile.get("collaborative") or {}).get("model_id") or "",
            "experiment_key": server.RECOMMENDATION_EXPERIMENT_KEY,
            "experiment_variant": profile.get("experiment_variant") or "control",
            "active_promotion_variant": (server._recommendation_active_promotion() or {}).get("promoted_variant") or "",
            "external_worker_expected": server.RECOMMENDATION_EXTERNAL_WORKER,
            "sync_dsn_configured": bool(server.RECOMMENDATION_SYNC_DATABASE_DSN),
            "scheduler_enabled": server.RECOMMENDATION_ENABLE_SCHEDULER,
            "profile_build_ms": profile_ms,
            "artifact_load_ms": 0,
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": profile.get("catalog_feature_version") or "",
            "taste_profile_version": profile.get("taste_profile_version") or "",
            "scene_graph_version": profile.get("scene_graph_version") or "",
            "feature_source": profile.get("feature_source") or "",
            "negative_feedback_applied": bool(profile.get("negative_feedback_applied")),
            "rich_launch_required": bool(precompute_state["rich_launch_required"]),
            "rich_bootstrap_attempted": bool(precompute_state["rich_bootstrap_attempted"]),
            "rich_bootstrap_deferred": bool(precompute_state["rich_bootstrap_deferred"]),
            "rich_bootstrap_snapshot_ms": int(precompute_state["rich_bootstrap_ms"] or 0),
            "precompute_cache_hit": bool(precompute_state["precompute_cache_hit"]),
            "precompute_resolution_ms": int(precompute_state["precompute_resolution_ms"] or 0),
            "force_rich_rows": bool(precompute_state["force_rich_rows"]),
            "launch_tier_only": bool(row_builder_meta.get("launch_tier_only")),
            "runtime_bootstrap_scheduled": bool(precompute_state["runtime_bootstrap_scheduled"]),
            "worker_runtime": worker_runtime,
            "precompute_substrate_mode": snapshot_substrate_mode(
                _candidate_snapshot_from_precompute(precompute_snapshot) or {}
            ),
            "snapshot_cacheable": snapshot_cacheable,
            "snapshot_quality_reasons": list(snapshot_quality_reasons),
            "row_assembly_ms": row_ms,
            "generator_timings_ms": generator_timings,
            "row_status": row_diagnostics,
            "row_order": [row["id"] for row in rows],
            "row_item_counts": row_item_counts,
            "row_source_counts": summary["row_source_counts"],
            "row_selected_source_counts": summary["row_selected_source_counts"],
            "row_model_versions": summary["row_model_versions"],
            "profile_key": profile_key,
            "row_builder_mode": row_builder_meta.get("row_builder_mode") or "candidate_snapshot_v42",
            "candidate_snapshot_source": row_builder_meta.get("candidate_snapshot_source") or "",
            "deferred_row_kinds": deferred_row_kinds,
            "deferred_rows_pending": bool(deferred_row_kinds),
            "candidate_pool_counts": dict(
                row_builder_meta.get("candidate_pool_counts") or {}
            ),
            "candidate_stage_timings_ms": dict(
                row_builder_meta.get("candidate_stage_timings_ms") or {}
            ),
            "artifact_source": "request_build",
            "promotion_status": "pending",
            "background_refresh_scheduled": False,
            "heavy_rows_pending": False,
            "heavy_rows_hydrated": True,
            "nearline_precompute": self._build_nearline_precompute_diagnostics(
                precompute_cache_hit=bool(precompute_state["precompute_cache_hit"]),
                precompute_hit=bool(precompute_state["precompute_hit"]),
                precompute_stale=bool(precompute_state["precompute_stale"]),
                precompute_snapshot=precompute_snapshot,
                row_builder_meta=row_builder_meta,
            ),
            "row_builder_budget_ms": {
                "required": int(RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS * 1000),
                "optional": int(RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS * 1000),
                "finalize": int(RECOMMEND_ROW_FINALIZE_BUDGET_SECONDS * 1000),
                "total": int(RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS * 1000),
                "disabled": bool(RECOMMEND_DISABLE_TIMEOUTS),
            },
            "request_mode": "request_build",
            "artifact_bypass_reason": "",
            "live_row_refresh": False,
            "live_row_refresh_ms": 0,
            "time_to_first_home_payload_ms": total_build_ms,
            "total_build_ms": total_build_ms,
        }

    def _build_rows_v41(
        self,
        profile: Dict[str, Any],
        *,
        precompute_snapshot: Dict[str, Any] | None = None,
        trace: Dict[str, Any] | None = None,
        allow_live_snapshot_build: bool = False,
        force_rich_rows: bool = False,
        launch_tier_only: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        return build_rows_for_snapshot(
            server=self._server,
            profile=profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=allow_live_snapshot_build,
            force_rich_rows=force_rich_rows,
            launch_tier_only=launch_tier_only,
        )

    def _prepare_next_session_response(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        started_at = time.perf_counter()
        profile_started_at = time.perf_counter()
        _legacy_req, profile = build_home_profile(req)
        profile_ms = int((time.perf_counter() - profile_started_at) * 1000)
        server._trace_stage(trace, "recommend.profile_build", profile_started_at)
        prepare_started_at = time.perf_counter()
        prepared = schedule_home_artifact_warmup(
            server=server,
            user_scope_id=profile.get("user_scope_id") or "guest",
            profile=profile,
            force=True,
        )
        prepare_ms = int((time.perf_counter() - prepare_started_at) * 1000)
        request_ms = int((time.perf_counter() - started_at) * 1000)
        diagnostics = {
            "cache_hit": False,
            "ranking_backend": "background_prepare",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "profile_build_ms": profile_ms,
            "row_assembly_ms": 0,
            "generator_timings_ms": {},
            "request_mode": "background_prepare",
            "background_refresh_scheduled": bool(prepared),
            "prepare_next_session": True,
            "prepare_only": True,
            "prepare_force_refresh": True,
            "promotion_status": "pending",
            "artifact_source": "background_prepare",
            "artifact_bypass_reason": "",
            "row_status": {},
            "time_to_first_home_payload_ms": request_ms,
            "total_build_ms": request_ms,
            "prepare_schedule_ms": prepare_ms,
        }
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=background_prepare scheduled={bool(prepared)} "
            f"profile_ms={profile_ms} schedule_ms={prepare_ms}",
            flush=True,
        )
        return {
            "status": "accepted",
            "prepared": bool(prepared),
            "diagnostics": diagnostics,
        }

    def _build_session_from_rows(
        self,
        *,
        profile: Dict[str, Any],
        rows: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
        candidate_snapshot: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        session_id = str(uuid.uuid4())
        now = time.time()
        session = {
            "session_id": session_id,
            "user_scope_id": profile["user_scope_id"],
            "profile_key": profile["profile_key"],
            "profile": profile,
            "candidate_snapshot": dict(candidate_snapshot or {}),
            "generated_at": now,
            "expires_at": now + server.RECOMMENDATION_FEED_SESSION_TTL_SECONDS,
            "rows": list(rows or []),
            "diagnostics": dict(diagnostics or {}),
        }
        store_feed_session(server, session)
        server._recommendation_record_impressions(
            session,
            visible_impression_rows(
                rows,
                page_size=server.RECOMMENDATION_ROW_PAGE_SIZE,
            ),
        )
        return session

    def _session_from_launch_artifact(
        self,
        *,
        req,
        profile: Dict[str, Any],
        launch_artifact: Dict[str, Any],
        heavy_artifact: Dict[str, Any] | None = None,
        background_refresh_scheduled: bool = False,
        aggressive_refresh: bool = False,
        refresh_token: str = "",
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = _recommendation_runtime_health(server)
        launch_rows = freshen_launch_rows(
            server,
            profile,
            list(launch_artifact.get("rows") or []),
            aggressive_refresh=aggressive_refresh,
            refresh_token=refresh_token,
        )
        launch_artifact_diagnostics = dict(launch_artifact.get("diagnostics") or {})
        artifact_row_builder_mode = str(
            launch_artifact_diagnostics.get("row_builder_mode") or ""
        )
        launch_tier_only = bool(launch_artifact_diagnostics.get("launch_tier_only")) or (
            artifact_row_builder_mode == "candidate_snapshot_thin_core_v1"
        )
        deferred_row_kinds = list(
            launch_artifact_diagnostics.get("deferred_row_kinds") or []
        )
        if launch_tier_only and not deferred_row_kinds:
            deferred_row_kinds = list(registry_deferred_row_kinds())
        heavy_rows = list((heavy_artifact or {}).get("rows") or [])
        rows = merge_home_rows(launch_rows, heavy_rows)
        launch_age_ms = max(
            0,
            int((time.time() - float(launch_artifact.get("generated_at") or 0.0)) * 1000),
        )
        diagnostics = {
            "cache_hit": True,
            "ranking_backend": "artifact_launch",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "profile_build_ms": 0,
            "row_assembly_ms": 0,
            "generator_timings_ms": {},
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": profile.get("catalog_feature_version") or "",
            "taste_profile_version": profile.get("taste_profile_version") or "",
            "scene_graph_version": profile.get("scene_graph_version") or "",
            "feature_source": profile.get("feature_source") or "",
            "negative_feedback_applied": bool(profile.get("negative_feedback_applied")),
            "row_status": {
                row_kind: {"status": status}
                for row_kind, status in dict(launch_artifact.get("row_status") or {}).items()
            },
            "row_order": [row.get("id") for row in rows if isinstance(row, dict)],
            "row_item_counts": {
                row.get("id"): len(row.get("items") or [])
                for row in rows
                if isinstance(row, dict)
            },
            "artifact_source": launch_artifact.get("resolved_from") or "promoted",
            "artifact_resolved_from": launch_artifact.get("resolved_from") or "promoted",
            "artifact_bypass_reason": "",
            "artifact_age_ms": launch_age_ms,
            "promotion_status": launch_artifact.get("promotion_status") or "promoted",
            "artifact_quality_score": float(launch_artifact.get("quality_score") or 0.0),
            "artifact_quality_reasons": list(launch_artifact.get("quality_reasons") or []),
            "artifact_row_builder_mode": artifact_row_builder_mode,
            "worker_runtime": worker_runtime,
            "background_refresh_scheduled": bool(background_refresh_scheduled),
            "heavy_rows_pending": not bool(heavy_rows),
            "heavy_rows_hydrated": bool(heavy_rows),
            "heavy_rows_source": (heavy_artifact or {}).get("resolved_from") or "",
            "heavy_rows_promotion_status": (heavy_artifact or {}).get("promotion_status") or "",
            "launch_freshness_applied": True,
            "launch_freshness_mode": "manual_refresh" if aggressive_refresh else "launch",
            "launch_tier_only": launch_tier_only,
            "candidate_snapshot_source": "launch_artifact",
            "candidate_pool_counts": {},
            "candidate_stage_timings_ms": {},
            "deferred_row_kinds": deferred_row_kinds,
            "deferred_rows_pending": bool(
                launch_artifact_diagnostics.get("deferred_rows_pending")
            )
            or bool(deferred_row_kinds),
            "nearline_precompute": {
                "hit": True,
                "stale": bool(launch_artifact.get("stale")),
                "resolved_from": launch_artifact.get("resolved_from") or "promoted",
                "launch_artifact": True,
                "snapshot_hit": False,
                "rows_seeded": len(rows),
                "candidate_pools_seeded": 0,
                "snapshot_generated_at": float(launch_artifact.get("generated_at") or 0.0),
                "snapshot_expires_at": float(launch_artifact.get("expires_at") or 0.0),
                "runtime": precompute_runtime_snapshot(),
            },
            "request_mode": "launch_artifact",
            "live_row_refresh": False,
            "live_row_refresh_ms": 0,
            "time_to_first_home_payload_ms": 0,
            "total_build_ms": 0,
        }
        return self._build_session_from_rows(
            profile=profile,
            rows=rows,
            diagnostics=diagnostics,
            candidate_snapshot=dict(launch_artifact.get("candidate_snapshot") or {}),
        )

    def _try_launch_artifact_session(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        server = self._server
        user_scope_id = profile.get("user_scope_id") or "guest"
        profile_key = profile.get("profile_key") or ""
        rich_launch_required = _should_bootstrap_rich_snapshot(profile)
        launch_artifact = get_home_launch_artifact(
            user_scope_id=user_scope_id,
            include_usable=True,
            server=server,
        )
        if not isinstance(launch_artifact, dict):
            launch_artifact = get_home_launch_artifact_for_profile(
                profile_key=profile_key,
                include_usable=True,
                server=server,
            )
        if not isinstance(launch_artifact, dict):
            return None
        if rich_launch_required:
            artifact_rejected = False
            rejection_reason = ""
            if _launch_artifact_supports_rich_rows(launch_artifact) and not _launch_artifact_meets_flagship_contract(launch_artifact):
                artifact_rejected = True
                rejection_reason = "flagship_contract"
            if artifact_rejected:
                invalidate_home_snapshots(
                    user_scope_id=user_scope_id,
                    profile_key=profile_key,
                    include_artifacts=True,
                    server=server,
                )
                server._trace_put(
                    trace,
                    "ranking_meta",
                    f"recommend.{rejection_reason}_artifact_rejected",
                    True,
                )
                return None
        heavy_artifact = None
        if bool(req.hydrate_heavy_rows):
            heavy_artifact = get_home_heavy_artifact(
                user_scope_id=user_scope_id,
                include_usable=True,
                server=server,
            )
            if not isinstance(heavy_artifact, dict):
                heavy_artifact = get_home_heavy_artifact_for_profile(
                    profile_key=profile_key,
                    include_usable=True,
                    server=server,
                )
        artifact_policy = self._resolve_launch_artifact_policy(
            req,
            launch_artifact=launch_artifact,
            rich_launch_required=rich_launch_required,
        )
        refresh_scheduled = False
        if artifact_policy["should_schedule_refresh"]:
            refresh_scheduled = schedule_home_artifact_warmup(
                server=server,
                user_scope_id=user_scope_id,
                profile=profile,
                force=bool(artifact_policy["prefer_fresh_rows"]),
            )
        if artifact_policy["should_bypass_artifact"]:
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.launch_artifact_bypassed_for_refresh",
                artifact_policy["artifact_bypass_reason"] or artifact_policy["resolved_from"],
            )
            return None
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.launch_artifact_source",
            artifact_policy["resolved_from"],
        )
        return self._session_from_launch_artifact(
            req=req,
            profile=profile,
            launch_artifact=launch_artifact,
            heavy_artifact=heavy_artifact if isinstance(heavy_artifact, dict) else None,
            background_refresh_scheduled=refresh_scheduled,
            aggressive_refresh=bool(artifact_policy["prefer_fresh_rows"]),
            refresh_token=str(getattr(req, "refresh_token", "") or ""),
        )

    def _load_cached_launch_session(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        profile_ms: int = 0,
        worker_runtime: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        if bool(req.force_refresh):
            return None
        server = self._server
        artifact_started_at = time.perf_counter()
        artifact_session = self._try_launch_artifact_session(
            req,
            profile=profile,
            trace=trace,
        )
        artifact_load_ms = int((time.perf_counter() - artifact_started_at) * 1000)
        server._trace_put(
            trace,
            "timings_ms",
            "recommend.artifact_load_ms",
            artifact_load_ms,
        )
        if artifact_session is None:
            return None
        diagnostics = dict(artifact_session.get("diagnostics") or {})
        diagnostics["profile_build_ms"] = profile_ms
        diagnostics["artifact_load_ms"] = artifact_load_ms
        diagnostics["worker_runtime"] = worker_runtime or {}
        diagnostics["request_mode"] = (
            "refresh_artifact"
            if bool(getattr(req, "prefer_fresh_rows", False))
            else "launch_artifact"
        )
        diagnostics["time_to_first_home_payload_ms"] = profile_ms + artifact_load_ms
        diagnostics["total_build_ms"] = profile_ms + artifact_load_ms
        artifact_session["diagnostics"] = diagnostics
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=artifact_load done artifact_ms={artifact_load_ms} "
            f"rows={len(artifact_session.get('rows') or [])}",
            flush=True,
        )
        return artifact_session

    def _resolve_precompute_session_state(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        worker_runtime: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = dict(worker_runtime or {})
        resolution_started_at = time.perf_counter()
        snapshot_state = self._load_precompute_snapshot(
            req,
            profile=profile,
        )
        precompute_state = self._apply_precompute_snapshot_policy(
            req=req,
            profile=profile,
            trace=trace,
            worker_runtime=worker_runtime,
            precompute_snapshot=snapshot_state["precompute_snapshot"],
            precompute_hit=bool(snapshot_state["precompute_hit"]),
        )
        precompute_cache_hit = bool(snapshot_state.get("precompute_cache_hit"))
        precompute_stale = bool(
            (precompute_state["precompute_snapshot"] or {}).get("stale")
        )
        if (not precompute_state["precompute_hit"]) or precompute_stale:
            schedule_home_artifact_warmup(
                server=server,
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile=profile,
                force=False,
            )
        precompute_resolution_ms = int(
            (time.perf_counter() - resolution_started_at) * 1000
        )
        server._trace_put(
            trace,
            "timings_ms",
            "recommend.precompute_resolution_ms",
            precompute_resolution_ms,
        )
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=precompute resolved_hit={precompute_cache_hit} "
            f"snapshot_ready={bool(precompute_state['precompute_hit'])} "
            f"stale={precompute_stale} "
            f"source={((precompute_state['precompute_snapshot'] or {}).get('resolved_from') or '')} "
            f"lookup_ms={precompute_resolution_ms} "
            f"bootstrap_ms={int(precompute_state['rich_bootstrap_ms'] or 0)} "
            f"worker_unhealthy={bool(worker_runtime.get('external_worker_unhealthy'))}",
            flush=True,
        )
        precompute_state["precompute_stale"] = precompute_stale
        precompute_state["precompute_cache_hit"] = precompute_cache_hit
        precompute_state["precompute_resolution_ms"] = precompute_resolution_ms
        return precompute_state

    def _build_session_v41(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        prune_feed_cache(server)
        started_at = time.perf_counter()

        profile_started_at = time.perf_counter()
        _legacy_req, profile = build_home_profile(req)
        profile_ms = int((time.perf_counter() - profile_started_at) * 1000)
        server._trace_stage(trace, "recommend.profile_build", profile_started_at)
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=profile_build done profile_ms={profile_ms}",
            flush=True,
        )
        profile_key = profile["profile_key"]
        worker_runtime = _recommendation_runtime_health(server)
        artifact_session = self._load_cached_launch_session(
            req,
            profile=profile,
            trace=trace,
            profile_ms=profile_ms,
            worker_runtime=worker_runtime,
        )
        if artifact_session is not None:
            return artifact_session
        precompute_state = self._resolve_precompute_session_state(
            req,
            profile=profile,
            trace=trace,
            worker_runtime=worker_runtime,
        )
        precompute_snapshot = precompute_state["precompute_snapshot"]
        precompute_hit = bool(precompute_state["precompute_hit"])
        precompute_stale = bool(precompute_state["precompute_stale"])
        rich_launch_required = bool(precompute_state["rich_launch_required"])
        force_rich_rows = bool(precompute_state["force_rich_rows"])
        runtime_bootstrap_scheduled = bool(precompute_state["runtime_bootstrap_scheduled"])
        rich_bootstrap_deferred = bool(precompute_state["rich_bootstrap_deferred"])
        rich_bootstrap_attempted = bool(precompute_state["rich_bootstrap_attempted"])
        rich_bootstrap_ms = int(precompute_state["rich_bootstrap_ms"] or 0)
        precompute_resolution_ms = int(precompute_state["precompute_resolution_ms"] or 0)
        launch_tier_only = (
            not bool(req.force_refresh)
            and not bool(getattr(req, "prefer_fresh_rows", False))
            and not bool(req.hydrate_heavy_rows)
        )

        row_started_at = time.perf_counter()
        rows, generator_timings, row_diagnostics, row_builder_meta = self._build_rows_v41(
            profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=bool(req.force_refresh)
            or RECOMMEND_LIVE_SNAPSHOT_ON_MISS
            or rich_bootstrap_attempted
            or force_rich_rows,
            force_rich_rows=force_rich_rows,
            launch_tier_only=launch_tier_only,
        )
        row_ms = int((time.perf_counter() - row_started_at) * 1000)
        server._trace_stage(trace, "recommend.row_assembly", row_started_at)
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=row_assembly done row_ms={row_ms} rows={len(rows or [])}",
            flush=True,
        )

        diagnostics = self._build_request_session_diagnostics(
            profile=profile,
            profile_ms=profile_ms,
            profile_key=profile_key,
            worker_runtime=worker_runtime,
            precompute_state=precompute_state,
            row_ms=row_ms,
            generator_timings=generator_timings,
            row_diagnostics=row_diagnostics,
            row_builder_meta=row_builder_meta,
            rows=rows,
            started_at=started_at,
        )
        artifact_result = store_home_serving_artifacts(
            user_scope_id=profile.get("user_scope_id") or "guest",
            profile_key=profile.get("profile_key") or "",
            rows=rows,
            candidate_snapshot=dict(row_builder_meta.get("candidate_snapshot") or {}),
            diagnostics=diagnostics,
            row_diagnostics=row_diagnostics,
            source_signature=profile.get("profile_key") or "",
        )
        diagnostics["promotion_status"] = artifact_result.get("promotion_status") or "rejected"
        diagnostics["artifact_source"] = "request_build"
        diagnostics["heavy_rows_pending"] = not bool(
            (artifact_result.get("heavy_artifact") or {}).get("rows")
        )
        diagnostics["heavy_rows_hydrated"] = True
        diagnostics["artifact_quality_reasons"] = list(
            artifact_result.get("quality_reasons") or []
        )
        session = self._build_session_from_rows(
            profile=profile,
            rows=rows,
            diagnostics=diagnostics,
            candidate_snapshot=dict(row_builder_meta.get("candidate_snapshot") or {}),
        )
        server._trace_put(trace, "candidate_counts", "recommend.rows_emitted", len(rows))
        server._trace_put(
            trace,
            "candidate_counts",
            "recommend.total_items",
            sum((diagnostics.get("row_item_counts") or {}).values()),
        )
        server._trace_put(
            trace,
            "source_counts",
            "recommend.row_item_counts",
            diagnostics.get("row_item_counts") or {},
        )
        server._trace_put(
            trace,
            "source_counts",
            "recommend.row_source_counts",
            diagnostics.get("row_source_counts") or {},
        )
        server._trace_put(trace, "ranking_meta", "recommend.profile_key", profile_key)
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.home_model_key",
            diagnostics.get("home_ranking_model_key") or "",
        )
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.home_model_version",
            diagnostics.get("home_ranking_model_version") or "",
        )
        if not bool(diagnostics.get("snapshot_cacheable")):
            invalidate_home_snapshots(
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile_key=profile_key,
                include_artifacts=False,
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.snapshot_invalidated",
                list(diagnostics.get("snapshot_quality_reasons") or []),
            )
        return session

    def _row_slice(self, row: Dict[str, Any], offset: int, page_size: int) -> Dict[str, Any]:
        items = list(row.get("items") or [])
        bounded_offset = max(int(offset or 0), 0)
        page_limit = max(1, min(page_size, 12))
        visible = items[bounded_offset: bounded_offset + page_limit]
        next_offset = bounded_offset + len(visible)
        can_extend = bool(row.get("can_extend"))
        return {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "item_type": row.get("item_type") or "track",
            "row_style": row.get("row_style") or "",
            "meta": dict(row.get("meta") or {}),
            "items": visible,
            "next_offset": next_offset,
            "has_more": next_offset < len(items) or can_extend,
        }

    def _feed_response_v41(self, session: Dict[str, Any]) -> Dict[str, Any]:
        initial_rows = [
            self._row_slice(row, 0, self._server.RECOMMENDATION_ROW_PAGE_SIZE)
            for row in session.get("rows") or []
        ]
        flatten_priority = {
            "todays_pick": -2,
            "mixed_for_you": -1,
            "continue_listening": 0,
            "because_you_played": 1,
            "listeners_like_you": 2,
            "rediscover": 3,
            "deep_cuts": 4,
            "trending_by_genre": 5,
            "offline_ready": 6,
            "trending_for_you": 7,
            "quiet_picks": 8,
            "frequently_listened": 9,
        }
        flattened = []
        for row in sorted(
            initial_rows,
            key=lambda item: flatten_priority.get(item.get("kind"), 50),
        ):
            if str(row.get("item_type") or "track") != "track":
                continue
            flattened.extend(row.get("items") or [])
            if len(flattened) >= 18:
                break
        return {
            "status": "success",
            "session_id": session["session_id"],
            "generated_at": session["generated_at"],
            "expires_at": session["expires_at"],
            "rows": initial_rows,
            "recommendations": flattened[:18],
            "has_more": any(row["has_more"] for row in initial_rows),
            "next_offset": sum(len(row.get("items") or []) for row in initial_rows),
            "diagnostics": session.get("diagnostics") or {},
        }

    def _row_page_response_v41(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        prune_feed_cache(server)
        session_id = server._recommendation_trim_text(req.session_id)
        row_id = server._recommendation_trim_text(req.row_id)
        if not session_id or not row_id:
            raise HTTPException(status_code=400, detail="session_id and row_id are required")
        load_started_at = time.perf_counter()
        session = load_feed_session(server, session_id)
        server._trace_stage(trace, "recommend.session_lookup", load_started_at)
        if session is None:
            raise HTTPException(status_code=404, detail="Recommendation session expired")
        if session.get("user_scope_id") != server._recommendation_trim_text(req.user_scope_id or "guest"):
            raise HTTPException(status_code=403, detail="Recommendation session scope mismatch")
        stored_rows = list(session.get("rows") or [])
        for index, row in enumerate(stored_rows):
            if row.get("id") != row_id:
                continue
            target_row = row
            row_context = server._recommendation_trim_text(getattr(req, "row_context", ""))
            original_item_count = len(list(target_row.get("items") or []))
            if row_context.startswith("genre_tab:") and str(target_row.get("kind") or "") == "trending_by_genre":
                candidate_snapshot = self._resolve_candidate_snapshot_for_session(session)
                if candidate_snapshot:
                    requested_tab_id = row_context.split(":", 1)[1].strip()
                    refreshed_row = refresh_trending_by_genre_row(
                        server=server,
                        row=target_row,
                        profile=session.get("profile") or {},
                        snapshot=candidate_snapshot,
                        tab_id=requested_tab_id,
                    )
                    stored_rows[index] = refreshed_row
                    session["rows"] = stored_rows
                    store_feed_session(server, session)
                    target_row = refreshed_row
                sliced = self._row_slice(
                    target_row,
                    0,
                    max(
                        len(list(target_row.get("items") or [])),
                        req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                    ),
                )
                sliced["has_more"] = False
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.row_page_items",
                    len(sliced.get("items") or []),
                )
                return {
                    "status": "success",
                    "session_id": session["session_id"],
                    "generated_at": session["generated_at"],
                    "expires_at": session["expires_at"],
                    "row": sliced,
                    "diagnostics": session.get("diagnostics") or {},
                }
            if row_context == "live_refresh" and str(target_row.get("kind") or "") == "continue_listening":
                live_refresh_started_at = time.perf_counter()
                refreshed_row = None
                refreshed_profile = session.get("profile") or {}
                try:
                    _legacy_req, refreshed_profile = build_home_profile(req)
                    refreshed_snapshot = trim_home_candidate_snapshot(
                        build_home_candidate_snapshot_fast_fallback(
                            server=server,
                            profile=refreshed_profile,
                        )
                    )
                    refreshed_row = build_continue_listening_row(
                        server=server,
                        profile=refreshed_profile,
                        snapshot=refreshed_snapshot,
                    )
                except Exception:
                    refreshed_row = None
                live_refresh_ms = int(
                    (time.perf_counter() - live_refresh_started_at) * 1000
                )
                if isinstance(refreshed_row, dict):
                    refreshed_row["id"] = target_row.get("id") or refreshed_row.get("id") or "continue_listening"
                    refreshed_row["title"] = target_row.get("title") or refreshed_row.get("title") or "Continue listening"
                    refreshed_row["kind"] = target_row.get("kind") or refreshed_row.get("kind") or "continue_listening"
                    stored_rows[index] = refreshed_row
                    session["rows"] = stored_rows
                    session["profile"] = refreshed_profile
                    diagnostics = dict(session.get("diagnostics") or {})
                    diagnostics["request_mode"] = "live_row_refresh"
                    diagnostics["live_row_refresh"] = True
                    diagnostics["live_row_refresh_ms"] = live_refresh_ms
                    session["diagnostics"] = diagnostics
                    store_feed_session(server, session)
                    target_row = refreshed_row
                sliced = self._row_slice(
                    target_row,
                    0,
                    max(
                        len(list(target_row.get("items") or [])),
                        req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                    ),
                )
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.row_page_items",
                    len(sliced.get("items") or []),
                )
                return {
                    "status": "success",
                    "session_id": session["session_id"],
                    "generated_at": session["generated_at"],
                    "expires_at": session["expires_at"],
                    "row": sliced,
                    "diagnostics": session.get("diagnostics") or {},
                }
            if should_extend_row(
                target_row,
                req.offset,
                req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
            ):
                candidate_snapshot = self._resolve_candidate_snapshot_for_session(session)
                if candidate_snapshot:
                    extended_row = extend_row_from_snapshot(
                        server=server,
                        row=target_row,
                        profile=session.get("profile") or {},
                        snapshot=candidate_snapshot,
                        page_size=max(req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE, 10),
                    )
                else:
                    schedule_home_warmup(
                        user_scope_id=session.get("user_scope_id") or "guest",
                        profile=session.get("profile") or {},
                    )
                    extended_row = dict(target_row)
                    extended_row["can_extend"] = False
                if (
                    len(list(extended_row.get("items") or [])) <= original_item_count
                    and int(req.offset or 0) >= original_item_count
                ):
                    extended_row["can_extend"] = False
                stored_rows[index] = extended_row
                session["rows"] = stored_rows
                store_feed_session(server, session)
                target_row = extended_row
            sliced = self._row_slice(
                target_row,
                req.offset,
                req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
            )
            if (
                not list(sliced.get("items") or [])
                and int(req.offset or 0) >= len(list(target_row.get("items") or []))
            ):
                sliced["has_more"] = False
            server._trace_put(
                trace,
                "candidate_counts",
                "recommend.row_page_items",
                len(sliced.get("items") or []),
            )
            return {
                "status": "success",
                "session_id": session["session_id"],
                "generated_at": session["generated_at"],
                "expires_at": session["expires_at"],
                "row": sliced,
                "diagnostics": session.get("diagnostics") or {},
            }
        raise HTTPException(status_code=404, detail="Recommendation row not found")

    def _resolve_candidate_snapshot_for_session(
        self,
        session: Dict[str, Any],
    ) -> Dict[str, Any]:
        server = self._server
        candidate_snapshot = dict(session.get("candidate_snapshot") or {})
        if candidate_snapshot:
            return candidate_snapshot
        warm_snapshot = get_home_snapshot(
            user_scope_id=session.get("user_scope_id") or "guest",
            server=server,
        )
        if isinstance(warm_snapshot, dict):
            candidate_snapshot = dict(warm_snapshot.get("candidate_snapshot") or {})
            if candidate_snapshot:
                candidate_snapshot["resolved_from"] = (
                    warm_snapshot.get("resolved_from") or "user_scope"
                )
                return candidate_snapshot
        profile_snapshot = get_home_snapshot_for_profile(
            profile_key=session.get("profile_key") or "",
            server=server,
        )
        if isinstance(profile_snapshot, dict):
            candidate_snapshot = dict(profile_snapshot.get("candidate_snapshot") or {})
            if candidate_snapshot:
                candidate_snapshot["resolved_from"] = (
                    profile_snapshot.get("resolved_from") or "profile_key"
                )
                return candidate_snapshot
        return {}

    def recommend(self, req):
        server = self._server
        trace = server._trace_start(
            "recommend",
            user_scope_id=req.user_scope_id or "guest",
            surface=req.surface or "home_feed",
            query=req.query or "",
        )
        try:
            request_started_at = time.perf_counter()
            with request_store_runtime(allow_persistent_reads=False):
                parse_started_at = time.perf_counter()
                is_row_page_request = bool(
                    server._recommendation_trim_text(req.session_id)
                    and server._recommendation_trim_text(req.row_id)
                )
                is_prepare_only_request = bool(
                    getattr(req, "prepare_next_session", False)
                ) and not is_row_page_request
                if is_row_page_request:
                    request_mode = "row_page"
                elif is_prepare_only_request:
                    request_mode = "background_prepare"
                else:
                    request_mode = "full_feed"
                recommender_impl = "v1"
                server._trace_put(trace, "ranking_meta", "recommend.request_mode", request_mode)
                server._trace_put(trace, "ranking_meta", "recommend.impl", recommender_impl)
                print(
                    "[EBB:recommend][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=request_parse mode={request_mode} impl={recommender_impl}",
                    flush=True,
                )
                server._trace_stage(trace, "recommend.request_parse", parse_started_at)

                if is_row_page_request:
                    response = self._row_page_response_v41(req, trace=trace)
                elif is_prepare_only_request:
                    response = self._prepare_next_session_response(req, trace=trace)
                else:
                    print(
                        "[EBB:recommend][progress] "
                        f"request_id={trace.get('request_id') or ''} "
                        "stage=build_session start",
                        flush=True,
                    )
                    session = self._build_session_v41(req, trace=trace)
                    print(
                        "[EBB:recommend][progress] "
                        f"request_id={trace.get('request_id') or ''} "
                        f"stage=build_session done rows={len(session.get('rows') or [])}",
                        flush=True,
                    )
                    response = self._feed_response_v41(session)

            serialize_started_at = time.perf_counter()
            server._trace_stage(trace, "recommend.serialize", serialize_started_at)

            diagnostics = response.get("diagnostics")
            if isinstance(diagnostics, dict):
                request_ms = int((time.perf_counter() - request_started_at) * 1000)
                diagnostics.setdefault("request_ms", request_ms)
                diagnostics.update(
                    server._trace_diagnostics(server._trace_finalize(trace, status="success"))
                )
                response["request_id"] = diagnostics.get("request_id") or trace.get("request_id")
                if request_ms >= 6000:
                    slow_row_status = diagnostics.get("row_status") or {}
                    if isinstance(slow_row_status, dict):
                        slow_row_status = {
                            key: (value or {}).get("status")
                            for key, value in slow_row_status.items()
                        }
                    print(
                        "[EBB:recommend][slow] "
                        f"request_id={diagnostics.get('request_id') or ''} "
                        f"request_ms={request_ms} "
                        f"profile_build_ms={diagnostics.get('profile_build_ms') or 0} "
                        f"row_assembly_ms={diagnostics.get('row_assembly_ms') or 0} "
                        f"stage_timings={diagnostics.get('stage_timings_ms') or {}} "
                        f"row_status={slow_row_status}"
                    )
            else:
                server._trace_finalize(trace, status="success")

            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
                session_id=server._recommendation_trim_text(response.get("session_id")),
                model_version=server._recommendation_trim_text(
                    (response.get("diagnostics") or {}).get("home_ranking_model_version")
                    or (response.get("diagnostics") or {}).get("collaborative_model_id")
    ),
)
            return response
        except HTTPException:
            server._trace_finalize(trace, status="failed")
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise
        except Exception as exc:
            server._trace_finalize(trace, status="failed", error=str(exc))
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise HTTPException(status_code=500, detail=str(exc))

    def recommended_artists(self, req):
        return recommended_artists_runtime(self._server, req)

    def interaction_event(self, req):
        return interaction_event_runtime(self._server, req)

    def search_interaction(self, req):
        return search_interaction_runtime(self._server, req)

    def model_status(self):
        return model_status_runtime(self._server)

    def model_versions(self):
        return model_versions_runtime(self._server)

    def experiments(self, *, window_hours: int):
        return experiments_runtime(self._server, window_hours=window_hours)

    def evaluate_experiments(self, *, force_promote: bool, window_hours: int):
        return evaluate_experiments_runtime(
            self._server,
            force_promote=force_promote,
            window_hours=window_hours,
        )

    def train_model(self, req):
        return train_model_runtime(self._server, req)

    def model_registry_versions(self, *, model_key: str, limit: int = 20):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        return {
            "status": "success",
            "model_key": normalized_key,
            "versions": list_model_versions(
                model_key=normalized_key,
                limit=limit,
            ),
        }

    def model_registry_activate(
        self,
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        normalized_version = self._server._recommendation_trim_text(version)
        if not normalized_key or not normalized_version:
            raise HTTPException(status_code=400, detail="model_key and version are required")
        ok = activate_model_version(
            model_key=normalized_key,
            version=normalized_version,
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not ok:
            raise HTTPException(status_code=404, detail="model version not found")
        return {
            "status": "success",
            "model_key": normalized_key,
            "active_version": normalized_version,
        }

    def model_registry_rollback(
        self,
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        result = rollback_model_version(
            model_key=normalized_key,
            target_version=self._server._recommendation_trim_text(target_version),
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not bool((result or {}).get("ok")):
            raise HTTPException(
                status_code=400,
                detail=f"rollback failed: {(result or {}).get('reason') or 'unknown'}",
            )
        return {
            "status": "success",
            "model_key": normalized_key,
            "rollback": result,
        }

    def model_rollout_events(self, *, model_key: str = "", limit: int = 50):
        normalized_key = self._server._recommendation_trim_text(model_key)
        return {
            "status": "success",
            "model_key": normalized_key,
            "events": list_rollout_events(
                model_key=normalized_key,
                limit=limit,
            ),
        }
