from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .home_pipeline import build_required_fallback_seed, build_row_seed
from .policy import is_required_row, required_row_kinds


@dataclass
class RowSeedCollection:
    row_seeds: Dict[str, Dict[str, Any]]
    generator_timings: Dict[str, int]
    row_diagnostics: Dict[str, Dict[str, Any]]
    metadata_enrich_limit: int | None


def collect_row_seeds(
    *,
    server: Any,
    profile: Dict[str, Any],
    candidate_snapshot: Dict[str, Any],
    selected_row_kinds: Sequence[str],
    deferred_row_kinds: Sequence[str],
    substrate_mode: str,
    precompute_hit: bool,
    launch_tier_only: bool = False,
) -> RowSeedCollection:
    generator_timings: Dict[str, int] = {}
    row_diagnostics: Dict[str, Dict[str, Any]] = {}
    row_seeds: Dict[str, Dict[str, Any]] = {}

    for row_kind in deferred_row_kinds:
        row_diagnostics[row_kind] = {
            "builder": "substrate_mode",
            "builder_ms": 0,
            "status": "deferred_substrate",
            "required": is_required_row(server, row_kind),
            "row_strategy": substrate_mode,
            "fallback_reason": "thin_snapshot_mode",
        }

    for row_kind in selected_row_kinds:
        row_seed = build_row_seed(
            server=server,
            row_kind=row_kind,
            profile=profile,
            snapshot=candidate_snapshot,
            allow_empty_diagnostics=True,
            launch_tier_only=launch_tier_only,
        )
        if not isinstance(row_seed, dict):
            row_diagnostics[row_kind] = {
                "builder": f"candidate_snapshot:{row_kind}",
                "builder_ms": 0,
                "status": "empty",
                "required": is_required_row(server, row_kind),
                "row_strategy": "personalized",
                "fallback_reason": "",
            }
            continue
        row_seeds[row_kind] = row_seed
        generator_timings[f"candidate_snapshot:{row_kind}"] = int(
            row_seed.get("allocator_ms") or 0
        )

    resolved_from = candidate_snapshot.get("resolved_from") or ""
    metadata_enrich_limit = None
    if launch_tier_only:
        metadata_enrich_limit = 0
    elif not precompute_hit and "fallback" in resolved_from:
        metadata_enrich_limit = 0
    elif not precompute_hit:
        metadata_enrich_limit = 2

    return RowSeedCollection(
        row_seeds=row_seeds,
        generator_timings=generator_timings,
        row_diagnostics=row_diagnostics,
        metadata_enrich_limit=metadata_enrich_limit,
    )


def apply_required_row_policy(
    *,
    server: Any,
    profile: Dict[str, Any],
    candidate_snapshot: Dict[str, Any],
    selected_row_kinds: Sequence[str],
    allow_required_fallback: bool,
    rows: List[Dict[str, Any]],
    row_diagnostics: Dict[str, Dict[str, Any]],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int],
    embedding_lookup: Dict[str, List[float]] | None,
    metadata_enrich_limit: int | None,
    finalize_row_seed: Callable[..., Tuple[Dict[str, Any] | None, Dict[str, Any]]],
) -> None:
    existing_row_ids = {
        row.get("id")
        for row in rows
        if isinstance(row, dict)
    }
    expected_required_rows = [
        row_kind
        for row_kind in required_row_kinds(server)
        if row_kind in set(selected_row_kinds)
    ]
    for required_row_kind in expected_required_rows:
        if required_row_kind in existing_row_ids:
            continue
        if not allow_required_fallback:
            existing_diagnostics = dict(row_diagnostics.get(required_row_kind) or {})
            existing_status = str(existing_diagnostics.get("status") or "").strip().lower()
            if existing_status in {
                "seed_pool_empty",
                "post_filter_empty",
                "finalize_filtered_out",
                "filtered_out",
                "empty",
            }:
                existing_diagnostics["required"] = True
                existing_diagnostics.setdefault("row_strategy", "quality_first")
                existing_diagnostics.setdefault("fallback_reason", "required_row_missing")
                row_diagnostics[required_row_kind] = existing_diagnostics
                continue
            row_diagnostics[required_row_kind] = {
                "builder": "required_row_policy",
                "builder_ms": 0,
                "status": "missing_no_fallback",
                "required": True,
                "row_strategy": "quality_first",
                "fallback_reason": "required_row_missing",
            }
            continue
        fallback_seed = build_required_fallback_seed(
            server=server,
            row_kind=required_row_kind,
            profile=profile,
            snapshot=candidate_snapshot,
        )
        if not isinstance(fallback_seed, dict):
            row_diagnostics[required_row_kind] = {
                "builder": "required_row_policy",
                "builder_ms": 0,
                "status": "fallback_unavailable",
                "required": True,
                "row_strategy": "fallback",
                "fallback_reason": "required_row_missing",
            }
            continue
        fallback_row, fallback_diag = finalize_row_seed(
            server=server,
            profile=profile,
            row_seed=fallback_seed,
            used_track_ids=used_track_ids,
            used_artist_counts=used_artist_counts,
            embedding_lookup=embedding_lookup,
            metadata_enrich_limit=metadata_enrich_limit,
            enforce_feed_artist_cap=False,
        )
        if fallback_row is None:
            fallback_diag["status"] = "fallback_filtered_out"
            row_diagnostics[required_row_kind] = fallback_diag
            continue
        fallback_diag["builder"] = "required_row_policy"
        fallback_diag["status"] = "fallback_emitted"
        row_diagnostics[required_row_kind] = fallback_diag
        rows.append(fallback_row)
