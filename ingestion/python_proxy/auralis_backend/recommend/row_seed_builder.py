from __future__ import annotations

import time
from typing import Any, Dict, Sequence, Tuple

from .allocator import build_row_allocation_plan
from .policy import apply_required_row_fallback_policy, row_title
from .row_seed_dispatch import (
    build_allocated_row_seed,
    build_required_fallback_seed_with_overrides,
    build_specialized_row_seed,
)
from .snapshot_builder import (
    build_home_candidate_snapshot_fast_fallback,
    snapshot_substrate_mode,
    trim_home_candidate_snapshot,
)
from .specialized_row_builders import refresh_trending_by_genre_row_builder
from .pool_runtime import (
    _combine_pools,
    _merge_pool_order,
    _post_filter_row_candidates,
    _prefilter_pool_order,
    _prefiltered_pool_name,
    _quiet_primary_pool_order,
    _trending_primary_pool_order,
)
from .specialized_row_seed_runtime import (
    HOME_ALBUM_CAP,
    HOME_ARTIST_CAP,
    build_continue_listening_seed,
    build_mixed_for_you_seed,
    build_todays_pick_seed,
    build_trending_by_genre_seed,
    build_trending_genre_tabs,
)


def build_continue_listening_row(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any] | None = None,
    build_continue_listening_seed_fn=None,
) -> Dict[str, Any] | None:
    build_continue_listening_seed_fn = (
        build_continue_listening_seed_fn or build_continue_listening_seed
    )
    resolved_snapshot = dict(snapshot or {})
    if not resolved_snapshot:
        resolved_snapshot = trim_home_candidate_snapshot(
            server,
            build_home_candidate_snapshot_fast_fallback(
                server=server,
                profile=profile,
            ),
        )
    row = build_continue_listening_seed_fn(
        server=server,
        profile=profile,
        snapshot=resolved_snapshot,
        title=row_title("continue_listening", profile),
    )
    if isinstance(row, dict):
        row["id"] = "continue_listening"
    return row


def refresh_trending_by_genre_row(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    tab_id: str,
    build_trending_genre_tabs_fn=None,
) -> Dict[str, Any]:
    build_trending_genre_tabs_fn = (
        build_trending_genre_tabs_fn or build_trending_genre_tabs
    )
    return refresh_trending_by_genre_row_builder(
        row=row,
        tab_id=tab_id,
        trending_genre_tabs_fn=lambda **kwargs: build_trending_genre_tabs_fn(
            server=server,
            profile=profile,
            snapshot=snapshot,
            **kwargs,
        ),
    )


def build_row_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    relaxed_filter: bool = False,
    pool_names_override: Sequence[str] | None = None,
    candidate_limit_override: int | None = None,
    allow_empty_diagnostics: bool = False,
    launch_tier_only: bool = False,
    full_refinement: bool = False,
    existing_row: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    title = row_title(row_kind, profile)
    row_started_at = time.perf_counter()
    specialized_row_seed = build_specialized_row_seed(
        row_kind=row_kind,
        title=title,
        snapshot=snapshot,
        row_started_at=row_started_at,
        specialized_builders={
            "todays_pick": lambda: build_todays_pick_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
                launch_tier_only=launch_tier_only,
            ),
            "mixed_for_you": lambda: build_mixed_for_you_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
                launch_tier_only=launch_tier_only,
                full_refinement=full_refinement,
                existing_row=existing_row,
            ),
            "trending_by_genre": lambda: build_trending_by_genre_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
                full_refinement=full_refinement,
                existing_row=existing_row,
            ),
            "continue_listening": lambda: build_continue_listening_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
            ),
        },
        home_album_cap=HOME_ALBUM_CAP,
        home_artist_cap=HOME_ARTIST_CAP,
    )
    if isinstance(specialized_row_seed, dict):
        return specialized_row_seed
    return build_allocated_row_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        title=title,
        row_started_at=row_started_at,
        relaxed_filter=relaxed_filter,
        pool_names_override=pool_names_override,
        candidate_limit_override=candidate_limit_override,
        allow_empty_diagnostics=allow_empty_diagnostics,
        launch_tier_only=launch_tier_only,
        build_row_allocation_plan_fn=build_row_allocation_plan,
        prefiltered_pool_name_fn=_prefiltered_pool_name,
        quiet_primary_pool_order_fn=_quiet_primary_pool_order,
        trending_primary_pool_order_fn=_trending_primary_pool_order,
        prefilter_pool_order_fn=_prefilter_pool_order,
        merge_pool_order_fn=_merge_pool_order,
        combine_pools_fn=_combine_pools,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
    )


def build_required_fallback_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    build_row_seed_fn=None,
) -> Dict[str, Any] | None:
    build_row_seed_fn = build_row_seed_fn or build_row_seed
    row_seed = build_row_seed_fn(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        relaxed_filter=True,
        launch_tier_only=False,
    )
    if not isinstance(row_seed, dict):
        row_seed = build_required_fallback_seed_with_overrides(
            row_kind=row_kind,
            snapshot=snapshot,
            build_row_seed_fn=lambda **kwargs: build_row_seed_fn(
                server=server,
                row_kind=row_kind,
                profile=profile,
                snapshot=snapshot,
                **kwargs,
            ),
            apply_required_row_fallback_policy_fn=apply_required_row_fallback_policy,
        )
    if not isinstance(row_seed, dict):
        return None
    return apply_required_row_fallback_policy(row_seed)
