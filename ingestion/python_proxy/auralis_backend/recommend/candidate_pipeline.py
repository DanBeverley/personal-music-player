from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from .home_pipeline import (
    build_home_candidate_snapshot,
    build_home_candidate_snapshot_fast_fallback,
    build_home_candidate_snapshot_fallback,
    snapshot_substrate_mode,
)
from .policy import row_kinds as policy_row_kinds
from .row_registry import launch_row_kinds, rich_snapshot_row_kinds, thin_snapshot_row_kinds
from .row_seed_pipeline import apply_required_row_policy, collect_row_seeds

ROW_KIND_ORDER = {
    row_kind: index
    for index, row_kind in enumerate(policy_row_kinds())
}


@dataclass(frozen=True)
class CandidateSnapshotResolution:
    candidate_snapshot: Dict[str, Any]
    precompute_hit: bool
    allow_required_fallback: bool
    substrate_mode: str
    selected_row_kinds: Tuple[str, ...]
    deferred_row_kinds: Tuple[str, ...]
    row_builder_mode: str
    launch_tier_only: bool = False


@dataclass
class CandidatePipelineExecution:
    rows: List[Dict[str, Any]]
    generator_timings: Dict[str, int]
    row_diagnostics: Dict[str, Dict[str, Any]]
    builder_meta: Dict[str, Any]

    def as_legacy_tuple(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        return (
            self.rows,
            self.generator_timings,
            self.row_diagnostics,
            self.builder_meta,
        )


def resolve_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
    precompute_snapshot: Dict[str, Any] | None = None,
    allow_live_snapshot_build: bool = False,
    force_rich_rows: bool = False,
    launch_tier_only: bool = False,
    trace: Dict[str, Any] | None = None,
) -> CandidateSnapshotResolution:
    candidate_snapshot = None
    precompute_hit = False
    if isinstance(precompute_snapshot, dict):
        candidate_snapshot = precompute_snapshot.get("candidate_snapshot")
        precompute_hit = isinstance(candidate_snapshot, dict) and bool(candidate_snapshot)
        if precompute_hit:
            candidate_snapshot = dict(candidate_snapshot or {})
            candidate_snapshot["resolved_from"] = (
                precompute_snapshot.get("resolved_from")
                or candidate_snapshot.get("resolved_from")
                or "nearline"
            )
    if not isinstance(candidate_snapshot, dict) or not candidate_snapshot:
        if allow_live_snapshot_build:
            try:
                candidate_snapshot = build_home_candidate_snapshot(
                    server=server,
                    profile=profile,
                )
                candidate_snapshot["resolved_from"] = "request_live"
            except Exception as exc:
                server._trace_put(
                    trace,
                    "errors",
                    "recommend.live_snapshot_error",
                    str(exc)[:240],
                )
                candidate_snapshot = build_home_candidate_snapshot_fallback(
                    server=server,
                    profile=profile,
                )
                candidate_snapshot["resolved_from"] = "request_fallback_after_live_error"
        else:
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.live_snapshot_deferred",
                True,
            )
            if launch_tier_only:
                candidate_snapshot = build_home_candidate_snapshot_fast_fallback(
                    server=server,
                    profile=profile,
                )
                candidate_snapshot["resolved_from"] = "request_launch_fast_fallback"
            else:
                candidate_snapshot = build_home_candidate_snapshot_fallback(
                    server=server,
                    profile=profile,
                )
                candidate_snapshot["resolved_from"] = "request_fallback_deferred"
    resolved_from = str(candidate_snapshot.get("resolved_from") or "")
    allow_required_fallback = "fallback" in resolved_from or "error" in resolved_from
    substrate_mode = snapshot_substrate_mode(candidate_snapshot)
    selected_row_kinds = (
        tuple(launch_row_kinds())
        if launch_tier_only
        else (
            tuple(rich_snapshot_row_kinds())
            if substrate_mode == "rich_personalized" or force_rich_rows
            else tuple(thin_snapshot_row_kinds())
        )
    )
    deferred_row_kinds = tuple(
        row_kind
        for row_kind in policy_row_kinds()
        if row_kind not in set(selected_row_kinds)
    )
    row_builder_mode = (
        "candidate_snapshot_launch_fast_v1"
        if launch_tier_only
        else (
            "candidate_snapshot_rich_forced_v1"
            if force_rich_rows and substrate_mode != "rich_personalized"
            else (
                "candidate_snapshot_rich_v1"
                if substrate_mode == "rich_personalized"
                else "candidate_snapshot_thin_core_v1"
            )
        )
    )
    return CandidateSnapshotResolution(
        candidate_snapshot=dict(candidate_snapshot),
        precompute_hit=precompute_hit,
        allow_required_fallback=allow_required_fallback,
        substrate_mode=substrate_mode,
        selected_row_kinds=selected_row_kinds,
        deferred_row_kinds=deferred_row_kinds,
        row_builder_mode=row_builder_mode,
        launch_tier_only=launch_tier_only,
    )


def execute_candidate_pipeline(
    *,
    server: Any,
    profile: Dict[str, Any],
    resolution: CandidateSnapshotResolution,
    trace: Dict[str, Any] | None = None,
    prepare_embeddings: Callable[..., Dict[str, List[float]]],
    finalize_row_seed: Callable[..., Tuple[Dict[str, Any] | None, Dict[str, Any]]],
) -> CandidatePipelineExecution:
    rows: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
    used_artist_counts: Dict[str, int] = {}
    candidate_snapshot = dict(resolution.candidate_snapshot or {})
    collected = collect_row_seeds(
        server=server,
        profile=profile,
        candidate_snapshot=candidate_snapshot,
        selected_row_kinds=resolution.selected_row_kinds,
        deferred_row_kinds=resolution.deferred_row_kinds,
        substrate_mode=resolution.substrate_mode,
        precompute_hit=resolution.precompute_hit,
        launch_tier_only=resolution.launch_tier_only,
    )
    embedding_lookup = prepare_embeddings(
        server=server,
        row_seeds=list(collected.row_seeds.values()),
    )

    for row_kind in resolution.selected_row_kinds:
        row_seed = collected.row_seeds.get(row_kind)
        if not isinstance(row_seed, dict):
            continue
        row, diagnostics = finalize_row_seed(
            server=server,
            profile=profile,
            row_seed=row_seed,
            used_track_ids=used_track_ids,
            used_artist_counts=used_artist_counts,
            embedding_lookup=embedding_lookup,
            metadata_enrich_limit=collected.metadata_enrich_limit,
        )
        collected.row_diagnostics[row_kind] = diagnostics
        if row is not None:
            rows.append(row)
    apply_required_row_policy(
        server=server,
        profile=profile,
        candidate_snapshot=candidate_snapshot,
        selected_row_kinds=resolution.selected_row_kinds,
        allow_required_fallback=resolution.allow_required_fallback,
        rows=rows,
        row_diagnostics=collected.row_diagnostics,
        used_track_ids=used_track_ids,
        used_artist_counts=used_artist_counts,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=collected.metadata_enrich_limit,
        finalize_row_seed=finalize_row_seed,
    )

    rows.sort(key=lambda row: ROW_KIND_ORDER.get(row.get("kind"), 100))
    server._trace_put(
        trace,
        "ranking_meta",
        "recommend.row_builder_mode",
        resolution.row_builder_mode,
    )
    builder_meta = {
        "row_builder_mode": resolution.row_builder_mode,
        "candidate_snapshot_source": candidate_snapshot.get("resolved_from") or "",
        "candidate_pool_counts": dict(candidate_snapshot.get("pool_counts") or {}),
        "candidate_stage_timings_ms": dict(candidate_snapshot.get("stage_timings_ms") or {}),
        "candidate_snapshot_generated_at": float(candidate_snapshot.get("generated_at") or 0.0),
        "candidate_snapshot_build_ms": int(candidate_snapshot.get("build_ms") or 0),
        "candidate_snapshot_albums_count": int(candidate_snapshot.get("albums_count") or 0),
        "candidate_snapshot_hit": bool(resolution.precompute_hit),
        "metadata_enrich_limit": (
            collected.metadata_enrich_limit
            if collected.metadata_enrich_limit is not None
            else -1
        ),
        "candidate_snapshot_substrate_mode": resolution.substrate_mode,
        "force_rich_rows": bool(
            resolution.row_builder_mode == "candidate_snapshot_rich_forced_v1"
        ),
        "launch_tier_only": bool(resolution.launch_tier_only),
        "selected_row_kinds": list(resolution.selected_row_kinds),
        "deferred_row_kinds": list(resolution.deferred_row_kinds),
        "candidate_snapshot": dict(candidate_snapshot),
    }
    return CandidatePipelineExecution(
        rows=rows,
        generator_timings=collected.generator_timings,
        row_diagnostics=collected.row_diagnostics,
        builder_meta=builder_meta,
    )
