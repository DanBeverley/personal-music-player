from __future__ import annotations

import os
import time
import uuid
from typing import Any
from typing import Dict, List, Tuple

from fastapi import HTTPException

from ..domain.features import build_home_profile
from ..legacy import get_server
from .home_pipeline import (
    build_home_candidate_snapshot_fallback,
    extend_row_from_snapshot,
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
    schedule_home_artifact_warmup,
    runtime_snapshot as precompute_runtime_snapshot,
    store_home_serving_artifacts,
    schedule_home_warmup,
)
from .feature_store import request_store_runtime
from .freshness_runtime import freshen_launch_rows, visible_impression_rows
from .quality import snapshot_quality_reasons as compute_snapshot_quality_reasons
from .row_runtime import (
    QUALITY_CRITICAL_ROWS,
    build_rows_v41 as build_rows_for_snapshot,
    merge_home_rows,
)
from .session_runtime import load_feed_session, prune_feed_cache, store_feed_session

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


class RecommendationService:
    def __init__(self, server: Any | None = None) -> None:
        self._server = server or get_server()

    def _build_rows_v41(
        self,
        profile: Dict[str, Any],
        *,
        precompute_snapshot: Dict[str, Any] | None = None,
        trace: Dict[str, Any] | None = None,
        allow_live_snapshot_build: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        return build_rows_for_snapshot(
            server=self._server,
            profile=profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=allow_live_snapshot_build,
        )

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
        launch_rows = freshen_launch_rows(
            server,
            profile,
            list(launch_artifact.get("rows") or []),
            aggressive_refresh=aggressive_refresh,
            refresh_token=refresh_token,
        )
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
            "artifact_age_ms": launch_age_ms,
            "promotion_status": launch_artifact.get("promotion_status") or "promoted",
            "background_refresh_scheduled": bool(background_refresh_scheduled),
            "heavy_rows_pending": not bool(heavy_rows),
            "heavy_rows_hydrated": bool(heavy_rows),
            "heavy_rows_source": (heavy_artifact or {}).get("resolved_from") or "",
            "heavy_rows_promotion_status": (heavy_artifact or {}).get("promotion_status") or "",
            "launch_freshness_applied": True,
            "launch_freshness_mode": "manual_refresh" if aggressive_refresh else "launch",
            "candidate_snapshot_source": "launch_artifact",
            "candidate_pool_counts": {},
            "candidate_stage_timings_ms": {},
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
        user_scope_id = profile.get("user_scope_id") or "guest"
        profile_key = profile.get("profile_key") or ""
        launch_artifact = get_home_launch_artifact(
            user_scope_id=user_scope_id,
            include_usable=True,
        )
        if not isinstance(launch_artifact, dict):
            launch_artifact = get_home_launch_artifact_for_profile(
                profile_key=profile_key,
                include_usable=True,
            )
        if not isinstance(launch_artifact, dict):
            return None
        heavy_artifact = None
        if bool(req.hydrate_heavy_rows):
            heavy_artifact = get_home_heavy_artifact(
                user_scope_id=user_scope_id,
                include_usable=True,
            )
            if not isinstance(heavy_artifact, dict):
                heavy_artifact = get_home_heavy_artifact_for_profile(
                    profile_key=profile_key,
                    include_usable=True,
                )
        should_refresh = True
        refresh_scheduled = False
        if should_refresh:
            refresh_scheduled = schedule_home_artifact_warmup(
                user_scope_id=user_scope_id,
                profile=profile,
                force=True,
            )
        server = self._server
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.launch_artifact_source",
            launch_artifact.get("resolved_from") or "",
        )
        return self._session_from_launch_artifact(
            req=req,
            profile=profile,
            launch_artifact=launch_artifact,
            heavy_artifact=heavy_artifact if isinstance(heavy_artifact, dict) else None,
            background_refresh_scheduled=refresh_scheduled,
            aggressive_refresh=bool(getattr(req, "prefer_fresh_rows", False)),
            refresh_token=str(getattr(req, "refresh_token", "") or ""),
        )

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
        artifact_started_at = time.perf_counter()
        if not bool(req.force_refresh):
            artifact_session = self._try_launch_artifact_session(
                req,
                profile=profile,
                trace=trace,
            )
            artifact_load_ms = int((time.perf_counter() - artifact_started_at) * 1000)
            if artifact_session is not None:
                diagnostics = dict(artifact_session.get("diagnostics") or {})
                diagnostics["profile_build_ms"] = profile_ms
                diagnostics["artifact_load_ms"] = artifact_load_ms
                diagnostics["request_mode"] = (
                    "refresh_artifact"
                    if bool(getattr(req, "prefer_fresh_rows", False))
                    else "launch_artifact"
                )
                artifact_session["diagnostics"] = diagnostics
                server._trace_put(
                    trace,
                    "timings_ms",
                    "recommend.artifact_load_ms",
                    artifact_load_ms,
                )
                print(
                    "[EBB:recommend][progress] "
                    f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
                    f"stage=artifact_load done artifact_ms={artifact_load_ms} "
                    f"rows={len(artifact_session.get('rows') or [])}",
                    flush=True,
                )
                return artifact_session
        else:
            artifact_load_ms = 0
        precompute_snapshot = None
        precompute_hit = False
        if not bool(req.force_refresh):
            precompute_snapshot = get_home_snapshot(
                user_scope_id=profile.get("user_scope_id") or "guest",
            )
            if isinstance(precompute_snapshot, dict):
                precompute_hit = bool(
                    precompute_snapshot.get("candidate_snapshot")
                    or precompute_snapshot.get("rows")
                )
        if not precompute_hit and not bool(req.force_refresh):
            precompute_snapshot = get_home_snapshot_for_profile(
                profile_key=profile_key,
            )
            if isinstance(precompute_snapshot, dict):
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
        precompute_stale = bool((precompute_snapshot or {}).get("stale"))
        if bool(req.force_refresh):
            try:
                precompute_snapshot = build_home_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    force=True,
                    profile=profile,
                )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
            except Exception:
                precompute_snapshot = None
        if not precompute_hit:
            schedule_home_artifact_warmup(
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile=profile,
                force=True,
            )
        elif precompute_stale:
            schedule_home_artifact_warmup(
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile=profile,
                force=True,
            )
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=precompute resolved_hit={precompute_hit} "
            f"stale={precompute_stale} "
            f"source={((precompute_snapshot or {}).get('resolved_from') or '')}",
            flush=True,
        )

        row_started_at = time.perf_counter()
        rows, generator_timings, row_diagnostics, row_builder_meta = self._build_rows_v41(
            profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=bool(req.force_refresh)
            or RECOMMEND_LIVE_SNAPSHOT_ON_MISS,
        )
        row_ms = int((time.perf_counter() - row_started_at) * 1000)
        server._trace_stage(trace, "recommend.row_assembly", row_started_at)
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=row_assembly done row_ms={row_ms} rows={len(rows or [])}",
            flush=True,
        )

        row_item_counts = {
            row["id"]: len(row.get("items") or [])
            for row in rows
        }
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
        snapshot_quality_reasons = compute_snapshot_quality_reasons(
            row_diagnostics,
            critical_rows=QUALITY_CRITICAL_ROWS,
        )
        snapshot_cacheable = not bool(snapshot_quality_reasons)

        home_ranking_model_key = "home_global_ranker_v4"
        home_ranking_model_version = server._ranking_model_version(home_ranking_model_key)
        diagnostics = {
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
            "snapshot_cacheable": snapshot_cacheable,
            "snapshot_quality_reasons": list(snapshot_quality_reasons),
            "row_assembly_ms": row_ms,
            "generator_timings_ms": generator_timings,
            "row_status": row_diagnostics,
            "row_order": [row["id"] for row in rows],
            "row_item_counts": row_item_counts,
            "row_source_counts": row_source_counts,
            "row_selected_source_counts": row_selected_source_counts,
            "row_model_versions": row_model_versions,
            "profile_key": profile_key,
            "row_builder_mode": row_builder_meta.get("row_builder_mode") or "candidate_snapshot_v42",
            "candidate_snapshot_source": row_builder_meta.get("candidate_snapshot_source") or "",
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
            "nearline_precompute": {
                "hit": precompute_hit,
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
            },
            "row_builder_budget_ms": {
                "required": int(RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS * 1000),
                "optional": int(RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS * 1000),
                "finalize": int(RECOMMEND_ROW_FINALIZE_BUDGET_SECONDS * 1000),
                "total": int(RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS * 1000),
                "disabled": bool(RECOMMEND_DISABLE_TIMEOUTS),
            },
            "total_build_ms": int((time.perf_counter() - started_at) * 1000),
        }
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
            sum(row_item_counts.values()),
        )
        server._trace_put(trace, "source_counts", "recommend.row_item_counts", row_item_counts)
        server._trace_put(trace, "source_counts", "recommend.row_source_counts", row_source_counts)
        server._trace_put(trace, "ranking_meta", "recommend.profile_key", profile_key)
        server._trace_put(trace, "ranking_meta", "recommend.home_model_key", home_ranking_model_key)
        server._trace_put(trace, "ranking_meta", "recommend.home_model_version", home_ranking_model_version)
        if not snapshot_cacheable:
            invalidate_home_snapshots(
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile_key=profile_key,
                include_artifacts=False,
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.snapshot_invalidated",
                list(snapshot_quality_reasons),
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
            "continue_listening": 0,
            "because_you_played": 1,
            "listeners_like_you": 2,
            "rediscover": 3,
            "deep_cuts": 4,
            "offline_ready": 5,
            "trending_for_you": 6,
            "quiet_picks": 7,
            "frequently_listened": 8,
        }
        flattened = []
        for row in sorted(
            initial_rows,
            key=lambda item: flatten_priority.get(item.get("kind"), 50),
        ):
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
            original_item_count = len(list(target_row.get("items") or []))
            if should_extend_row(
                target_row,
                req.offset,
                req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
            ):
                candidate_snapshot = dict(session.get("candidate_snapshot") or {})
                if not candidate_snapshot:
                    warm_snapshot = get_home_snapshot(
                        user_scope_id=session.get("user_scope_id") or "guest",
                    )
                    if isinstance(warm_snapshot, dict):
                        candidate_snapshot = dict(
                            warm_snapshot.get("candidate_snapshot") or {}
                        )
                        if candidate_snapshot:
                            candidate_snapshot["resolved_from"] = (
                                warm_snapshot.get("resolved_from") or "user_scope"
                            )
                if not candidate_snapshot:
                    profile_snapshot = get_home_snapshot_for_profile(
                        profile_key=session.get("profile_key") or "",
                    )
                    if isinstance(profile_snapshot, dict):
                        candidate_snapshot = dict(
                            profile_snapshot.get("candidate_snapshot") or {}
                        )
                        if candidate_snapshot:
                            candidate_snapshot["resolved_from"] = (
                                profile_snapshot.get("resolved_from") or "profile_key"
                            )
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
                    fallback_snapshot = build_home_candidate_snapshot_fallback(
                        server=server,
                        profile=session.get("profile") or {},
                    )
                    if isinstance(fallback_snapshot, dict) and fallback_snapshot:
                        fallback_snapshot["resolved_from"] = "row_page_fallback"
                        session["candidate_snapshot"] = dict(fallback_snapshot)
                        extended_row = extend_row_from_snapshot(
                            server=server,
                            row=target_row,
                            profile=session.get("profile") or {},
                            snapshot=fallback_snapshot,
                            page_size=max(req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE, 10),
                        )
                    else:
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
                request_mode = "row_page" if is_row_page_request else "full_feed"
                server._trace_put(trace, "ranking_meta", "recommend.request_mode", request_mode)
                print(
                    "[EBB:recommend][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=request_parse mode={request_mode}",
                    flush=True,
                )
                server._trace_stage(trace, "recommend.request_parse", parse_started_at)

                if is_row_page_request:
                    response = self._row_page_response_v41(req, trace=trace)
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
        return self._server.recommended_artists(req)

    def interaction_event(self, req):
        return self._server.recommendation_interaction_event(req)

    def search_interaction(self, req):
        return self._server.recommendation_search_interaction(req)

    def model_status(self):
        return self._server.recommendation_model_status()

    def model_versions(self):
        return self._server.recommendation_model_versions()

    def experiments(self, *, window_hours: int):
        return self._server.recommendation_experiments(window_hours=window_hours)

    def evaluate_experiments(self, *, force_promote: bool, window_hours: int):
        return self._server.recommendation_experiments_evaluate(
            force_promote=force_promote,
            window_hours=window_hours,
        )

    def train_model(self, req):
        return self._server.recommendation_model_train(req)

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
