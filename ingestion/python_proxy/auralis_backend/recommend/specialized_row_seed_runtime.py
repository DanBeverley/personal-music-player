from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .allocator import build_row_allocation_plan
from .home_config import (
    _HOME_ALBUM_CAP,
    _HOME_ARTIST_CAP,
    _HOME_GENRE_CANDIDATE_CAP,
    _HOME_GENRE_TAB_LIMIT,
    _HOME_GENRE_TRACK_CAP,
    _HOME_LAUNCH_MIX_TRACK_CAP,
    _HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP,
    _HOME_MIX_ACCENTS,
    _HOME_MIX_MAX_COUNT,
    _HOME_MIX_TRACK_CAP,
    _HOME_TODAYS_PICK_CANDIDATE_CAP,
)
from .specialized_row_builders import (
    build_continue_listening_seed as build_continue_listening_seed_stage,
    build_mixed_for_you_seed as build_mixed_for_you_seed_stage,
    build_todays_pick_seed as build_todays_pick_seed_stage,
    build_trending_by_genre_seed as build_trending_by_genre_seed_stage,
    build_trending_genre_tabs as build_trending_genre_tabs_stage,
)
from .pool_runtime import _combine_pools, _trending_primary_pool_order
from .specialized_row_helpers_runtime import (
    custom_row_candidates,
    finalize_custom_track_items,
    genre_tab_identifier,
    mix_artist_line,
    mix_blueprint_candidates,
    mix_blueprints,
    select_mix_blueprints,
    theme_accent,
    track_artist_label,
    trending_facet_candidates,
    trending_taste_facets,
)
from .snapshot_builder import snapshot_substrate_mode

HOME_ALBUM_CAP = _HOME_ALBUM_CAP
HOME_ARTIST_CAP = _HOME_ARTIST_CAP


def build_continue_listening_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    combine_pools_fn=None,
    snapshot_substrate_mode_fn=None,
) -> Dict[str, Any] | None:
    combine_pools_fn = combine_pools_fn or _combine_pools
    snapshot_substrate_mode_fn = snapshot_substrate_mode_fn or snapshot_substrate_mode
    return build_continue_listening_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        combine_pools_fn=combine_pools_fn,
        snapshot_substrate_mode_fn=snapshot_substrate_mode_fn,
    )


def build_todays_pick_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
    custom_row_candidates_fn=None,
    finalize_custom_track_items_fn=None,
    track_artist_label_fn=None,
    theme_accent_fn=None,
) -> Dict[str, Any] | None:
    custom_row_candidates_fn = custom_row_candidates_fn or custom_row_candidates
    finalize_custom_track_items_fn = (
        finalize_custom_track_items_fn or finalize_custom_track_items
    )
    track_artist_label_fn = track_artist_label_fn or track_artist_label
    theme_accent_fn = theme_accent_fn or theme_accent
    return build_todays_pick_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        home_todays_pick_candidate_cap=(
            _HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP
            if launch_tier_only
            else _HOME_TODAYS_PICK_CANDIDATE_CAP
        ),
        custom_row_candidates_fn=custom_row_candidates_fn,
        finalize_custom_track_items_fn=finalize_custom_track_items_fn,
        track_artist_label_fn=track_artist_label_fn,
        theme_accent_fn=theme_accent_fn,
    )


def build_mixed_for_you_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
    snapshot_substrate_mode_fn=None,
    select_mix_blueprints_fn=None,
    mix_blueprints_fn=None,
    mix_blueprint_candidates_fn=None,
    finalize_custom_track_items_fn=None,
    theme_accent_fn=None,
    mix_artist_line_fn=None,
    full_refinement: bool = False,
    existing_row: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    snapshot_substrate_mode_fn = snapshot_substrate_mode_fn or snapshot_substrate_mode
    select_mix_blueprints_fn = select_mix_blueprints_fn or select_mix_blueprints
    mix_blueprints_fn = mix_blueprints_fn or mix_blueprints
    mix_blueprint_candidates_fn = (
        mix_blueprint_candidates_fn or mix_blueprint_candidates
    )
    finalize_custom_track_items_fn = (
        finalize_custom_track_items_fn or finalize_custom_track_items
    )
    theme_accent_fn = theme_accent_fn or theme_accent
    mix_artist_line_fn = mix_artist_line_fn or mix_artist_line
    if snapshot_substrate_mode_fn(snapshot) != "rich_personalized":
        return None
    return build_mixed_for_you_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        home_mix_track_cap=(
            min(_HOME_MIX_TRACK_CAP, _HOME_LAUNCH_MIX_TRACK_CAP)
            if launch_tier_only
            else _HOME_MIX_TRACK_CAP
        ),
        home_mix_max_count=_HOME_MIX_MAX_COUNT,
        home_mix_accents=_HOME_MIX_ACCENTS,
        select_mix_blueprints_fn=select_mix_blueprints_fn,
        mix_blueprints_fn=mix_blueprints_fn,
        mix_blueprint_candidates_fn=mix_blueprint_candidates_fn,
        finalize_custom_track_items_fn=finalize_custom_track_items_fn,
        theme_accent_fn=theme_accent_fn,
        mix_artist_line_fn=mix_artist_line_fn,
        full_refinement=full_refinement,
        existing_row=existing_row,
    )


def build_trending_by_genre_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    trending_genre_tabs_fn=None,
    full_refinement: bool = False,
    existing_row: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    trending_genre_tabs_fn = trending_genre_tabs_fn or build_trending_genre_tabs
    return build_trending_by_genre_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        trending_genre_tabs_fn=trending_genre_tabs_fn,
        full_refinement=full_refinement,
        existing_row=existing_row,
    )


def build_trending_genre_tabs(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    preferred_tab_ids: Sequence[str] | None = None,
    selected_tab_id: str = "",
    build_row_allocation_plan_fn=None,
    trending_primary_pool_order_fn=None,
    combine_pools_fn=None,
    trending_taste_facets_fn=None,
    trending_facet_candidates_fn=None,
    finalize_custom_track_items_fn=None,
    mix_artist_line_fn=None,
    genre_tab_identifier_fn=None,
    full_refinement: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
    build_row_allocation_plan_fn = (
        build_row_allocation_plan_fn or build_row_allocation_plan
    )
    trending_primary_pool_order_fn = (
        trending_primary_pool_order_fn or _trending_primary_pool_order
    )
    combine_pools_fn = combine_pools_fn or _combine_pools
    trending_taste_facets_fn = (
        trending_taste_facets_fn or trending_taste_facets
    )
    trending_facet_candidates_fn = (
        trending_facet_candidates_fn or trending_facet_candidates
    )
    finalize_custom_track_items_fn = (
        finalize_custom_track_items_fn or finalize_custom_track_items
    )
    mix_artist_line_fn = mix_artist_line_fn or mix_artist_line
    genre_tab_identifier_fn = genre_tab_identifier_fn or genre_tab_identifier
    return build_trending_genre_tabs_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        preferred_tab_ids=preferred_tab_ids,
        selected_tab_id=selected_tab_id,
        home_genre_candidate_cap=_HOME_GENRE_CANDIDATE_CAP,
        home_genre_tab_limit=_HOME_GENRE_TAB_LIMIT,
        home_genre_track_cap=_HOME_GENRE_TRACK_CAP,
        build_row_allocation_plan_fn=build_row_allocation_plan_fn,
        trending_primary_pool_order_fn=trending_primary_pool_order_fn,
        combine_pools_fn=combine_pools_fn,
        trending_taste_facets_fn=trending_taste_facets_fn,
        trending_facet_candidates_fn=trending_facet_candidates_fn,
        finalize_custom_track_items_fn=finalize_custom_track_items_fn,
        mix_artist_line_fn=mix_artist_line_fn,
        genre_tab_identifier_fn=genre_tab_identifier_fn,
        full_refinement=full_refinement,
    )
