from __future__ import annotations

import time
from typing import Any, Callable, Dict, Sequence


def build_specialized_row_seed(
    *,
    row_kind: str,
    title: str,
    snapshot: Dict[str, Any],
    row_started_at: float,
    specialized_builders: Dict[str, Callable[[], Dict[str, Any] | None]],
    home_album_cap: int,
    home_artist_cap: int,
) -> Dict[str, Any] | None:
    specialized_builder = specialized_builders.get(row_kind)
    if callable(specialized_builder):
        row = specialized_builder()
        if not isinstance(row, dict):
            return None
        row["allocator_ms"] = int((time.perf_counter() - row_started_at) * 1000)
        return row
    if row_kind == "recommended_albums":
        items = list(snapshot.get("albums") or [])[:home_album_cap]
        if not items:
            return None
        return {
            "title": title,
            "kind": row_kind,
            "item_type": "album",
            "items": items[:18],
            "row_strategy": "personalized",
            "fallback_reason": "",
            "source_pool_counts": {"albums": len(items)},
            "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
        }
    if row_kind == "recommended_artists":
        items = list(snapshot.get("artists") or [])[:home_artist_cap]
        if not items:
            return None
        artist_meta = dict(snapshot.get("artist_artifact_meta") or {})
        return {
            "title": title,
            "kind": row_kind,
            "item_type": "artist",
            "items": items[:home_artist_cap],
            "meta": artist_meta,
            "row_strategy": "personalized",
            "fallback_reason": "",
            "source_pool_counts": {"artists": len(items)},
            "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
        }
    return None


def build_allocated_row_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    row_started_at: float,
    relaxed_filter: bool,
    pool_names_override: Sequence[str] | None,
    candidate_limit_override: int | None,
    allow_empty_diagnostics: bool,
    launch_tier_only: bool = False,
    build_row_allocation_plan_fn: Callable[..., Dict[str, Any] | None],
    prefiltered_pool_name_fn: Callable[[str], str],
    quiet_primary_pool_order_fn: Callable[[Dict[str, Any], Dict[str, Any]], Sequence[str]],
    trending_primary_pool_order_fn: Callable[[Dict[str, Any], Dict[str, Any]], Sequence[str]],
    prefilter_pool_order_fn: Callable[[str], Sequence[str]],
    merge_pool_order_fn: Callable[[Sequence[str], Sequence[str], Dict[str, Any]], Sequence[str]],
    combine_pools_fn: Callable[..., tuple[list[Dict[str, Any]], Dict[str, int]]],
    post_filter_row_candidates_fn: Callable[..., list[Dict[str, Any]]],
) -> Dict[str, Any] | None:
    allocation_plan = build_row_allocation_plan_fn(
        server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
    )
    if not isinstance(allocation_plan, dict):
        return None
    preferred_pool_name = prefiltered_pool_name_fn(row_kind)
    pool_names = list(pool_names_override or allocation_plan.get("pool_names") or ())
    if row_kind == "quiet_picks" and not pool_names_override:
        pool_names = list(quiet_primary_pool_order_fn(snapshot, allocation_plan))
    if row_kind == "trending_for_you" and not pool_names_override:
        pool_names = list(trending_primary_pool_order_fn(snapshot, allocation_plan))
    if preferred_pool_name and preferred_pool_name in dict(snapshot.get("pools") or {}):
        pool_names = [
            preferred_pool_name,
            *[pool_name for pool_name in pool_names if pool_name != preferred_pool_name],
        ]
    candidate_limit = int(candidate_limit_override or allocation_plan.get("candidate_limit") or 18)
    if launch_tier_only:
        candidate_limit = min(candidate_limit, 28)
    elif row_kind == "quiet_picks":
        candidate_limit = max(candidate_limit, 120)
    elif row_kind == "trending_for_you":
        candidate_limit = max(candidate_limit, 96)
    candidates, source_pool_counts = combine_pools_fn(
        server,
        snapshot,
        tuple(pool_names),
        limit=candidate_limit,
    )
    raw_candidate_count = len(list(candidates or []))
    candidates = post_filter_row_candidates_fn(
        server,
        row_kind,
        profile,
        candidates,
        relaxed=relaxed_filter,
    )
    filtered_candidate_count = len(list(candidates or []))
    if (
        row_kind == "quiet_picks"
        and not launch_tier_only
        and not candidates
        and not pool_names_override
    ):
        adaptive_pool_names = list(
            merge_pool_order_fn(
                prefilter_pool_order_fn("quiet_picks"),
                pool_names,
                dict(snapshot.get("pools") or {}),
            )
        )
        if adaptive_pool_names:
            adaptive_candidates, adaptive_counts = combine_pools_fn(
                server,
                snapshot,
                tuple(adaptive_pool_names),
                limit=max(candidate_limit, 144),
            )
            adaptive_raw_count = len(list(adaptive_candidates or []))
            adaptive_candidates = post_filter_row_candidates_fn(
                server,
                row_kind,
                profile,
                adaptive_candidates,
                relaxed=True,
            )
            if adaptive_candidates:
                candidates = adaptive_candidates
                filtered_candidate_count = len(list(adaptive_candidates or []))
                raw_candidate_count = adaptive_raw_count
                pool_names = adaptive_pool_names
                source_pool_counts = adaptive_counts
            else:
                raw_candidate_count = max(raw_candidate_count, adaptive_raw_count)
    if (
        row_kind == "trending_for_you"
        and not launch_tier_only
        and not candidates
        and not pool_names_override
    ):
        adaptive_pool_names = list(
            merge_pool_order_fn(
                (
                    "peer_scene",
                    "genre_subgenre",
                    "era_neighbors",
                    "language_safe",
                    "popularity_taste",
                    "collaborative",
                    "artist_neighbors",
                    "primary_anchor_neighbors",
                    "anchor_neighbors",
                    "history_top",
                    "history_recent",
                    "taste_fallback",
                    "exploration",
                ),
                pool_names,
                dict(snapshot.get("pools") or {}),
            )
        )
        if adaptive_pool_names:
            adaptive_candidates, adaptive_counts = combine_pools_fn(
                server,
                snapshot,
                tuple(adaptive_pool_names),
                limit=max(candidate_limit, 120),
            )
            adaptive_raw_count = len(list(adaptive_candidates or []))
            adaptive_candidates = post_filter_row_candidates_fn(
                server,
                row_kind,
                profile,
                adaptive_candidates,
                relaxed=True,
            )
            if adaptive_candidates:
                candidates = adaptive_candidates
                pool_names = adaptive_pool_names
                source_pool_counts = adaptive_counts
                raw_candidate_count = adaptive_raw_count
                filtered_candidate_count = len(list(adaptive_candidates or []))
                allocation_plan = dict(allocation_plan)
                allocation_plan["row_strategy"] = "personalized"
                allocation_plan["fallback_reason"] = ""
    if not candidates:
        if allow_empty_diagnostics:
            failure_stage = "post_filter_empty" if raw_candidate_count > 0 else "seed_pool_empty"
            return {
                "title": title,
                "kind": row_kind,
                "candidates": [],
                "row_strategy": allocation_plan.get("row_strategy") or "fallback",
                "fallback_reason": allocation_plan.get("fallback_reason") or "",
                "source_pool_counts": source_pool_counts,
                "allocator_model": {
                    "key": allocation_plan.get("model_key") or "",
                    "version": allocation_plan.get("model_version") or "",
                },
                "allocator_pool_order": list(pool_names),
                "allocator_pool_scores": list(allocation_plan.get("pool_scores") or []),
                "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
                "seed_failure_stage": failure_stage,
                "candidate_count_input": int(raw_candidate_count),
                "candidate_count_filtered": int(filtered_candidate_count),
            }
        return None
    row_seed = {
        "title": title,
        "kind": row_kind,
        "candidates": candidates,
        "row_strategy": allocation_plan.get("row_strategy") or "fallback",
        "fallback_reason": allocation_plan.get("fallback_reason") or "",
        "source_pool_counts": source_pool_counts,
        "allocator_model": {
            "key": allocation_plan.get("model_key") or "",
            "version": allocation_plan.get("model_version") or "",
        },
        "allocator_pool_order": list(pool_names),
        "allocator_pool_scores": list(allocation_plan.get("pool_scores") or []),
        "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
        "candidate_count_input": int(raw_candidate_count),
        "candidate_count_filtered": int(filtered_candidate_count),
    }
    if row_kind == "quiet_picks":
        row_seed["quiet_query"] = snapshot.get("quiet_seed") or ""
        row_seed["used_queries"] = []
    return row_seed


def build_required_fallback_seed_with_overrides(
    *,
    row_kind: str,
    snapshot: Dict[str, Any],
    build_row_seed_fn: Callable[..., Dict[str, Any] | None],
    apply_required_row_fallback_policy_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any] | None:
    fallback_pool_order = {
        "trending_for_you": (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "collaborative",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "history_top",
            "history_recent",
            "taste_fallback",
            "exploration",
        ),
        "quiet_picks": (
            "quiet_prefiltered",
            "peer_scene",
            "genre_subgenre",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "collaborative",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "rediscovery",
            "history_top",
            "history_recent",
            "same_artist",
            "offline_library",
            "taste_fallback",
            "exploration",
        ),
    }.get(row_kind)
    if not fallback_pool_order:
        return None
    fallback_limit = max(
        32,
        int((snapshot.get("pool_counts") or {}).get("history_top") or 0),
        24,
    )
    row_seed = build_row_seed_fn(
        relaxed_filter=True,
        pool_names_override=fallback_pool_order,
        candidate_limit_override=fallback_limit,
    )
    if not isinstance(row_seed, dict):
        return None
    return apply_required_row_fallback_policy_fn(row_seed)
