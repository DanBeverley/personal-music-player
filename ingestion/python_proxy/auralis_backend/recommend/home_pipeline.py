from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import time

from ..domain.artist_recommendations import ArtistRecommendationService
from .allocator import build_row_allocation_plan
from .home_config import (
    _HOME_GENRE_CANDIDATE_CAP,
    _HOME_GENRE_TAB_LIMIT,
    _HOME_GENRE_TRACK_CAP,
    _HOME_LAUNCH_ROW_CANDIDATE_CAP,
    _HOME_LAUNCH_MIX_TRACK_CAP,
    _HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP,
    _HOME_MIX_ACCENTS,
    _HOME_MIX_MAX_COUNT,
    _HOME_MIX_MIN_COUNT,
    _HOME_MIX_TRACK_CAP,
    _HOME_POOL_CANDIDATE_CAP,
    _HOME_TODAYS_PICK_CANDIDATE_CAP,
    _ROW_TRACK_PAGE_SIZE,
)
from . import pool_runtime as _pool_runtime
from . import specialized_row_helpers_runtime as _specialized_row_helpers
from .policy import apply_required_row_fallback_policy, row_kinds, row_title
from . import row_item_finalizer as _row_item_finalizer
from .row_item_finalizer import (
    _artist_family_identity,
    _track_authenticity_penalty,
    apply_quiet_row_runtime_fields,
    apply_track_row_runtime_fields,
)
from .row_ranking import track_score
from .specialized_row_builders import (
    build_mixed_for_you_seed as build_mixed_for_you_seed_stage,
)
from .source_runtime import (
    _recommendation_home_fallback_tracks,
    _recommendation_taste_filtered_tracks,
)
from ..search.runtime import search_artist_seed_tracks

_ARTIST_RECOMMENDATION_SERVICE = ArtistRecommendationService()


def _artist_recommendation_service(server: Any) -> ArtistRecommendationService:
    _ARTIST_RECOMMENDATION_SERVICE._server = server
    return _ARTIST_RECOMMENDATION_SERVICE


def _prefiltered_pool_name(row_kind: str) -> str:
    return _pool_runtime._prefiltered_pool_name(row_kind)


def _prefilter_pool_order(row_kind: str) -> Tuple[str, ...]:
    return _pool_runtime._prefilter_pool_order(row_kind)

def _candidate_signature(server: Any, candidate: Dict[str, Any]) -> str:
    return _pool_runtime._candidate_signature(server, candidate)


def _candidate_copy(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return _pool_runtime._candidate_copy(candidate)


def _trim_candidate_pool(server: Any, candidates: Sequence[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    return _pool_runtime._trim_candidate_pool(
        server,
        candidates,
        limit=limit,
    )


def _feature_pool_candidate(
    candidate: Dict[str, Any],
    *,
    pool_name: str,
    score: float,
    reason: str,
) -> Dict[str, Any]:
    return _pool_runtime._feature_pool_candidate(
        candidate,
        pool_name=pool_name,
        score=score,
        reason=reason,
    )


def _merge_pool_order(
    preferred_pool_names: Sequence[str],
    allocator_pool_names: Sequence[str],
    available_pools: Dict[str, Any],
) -> List[str]:
    return _pool_runtime._merge_pool_order(
        preferred_pool_names,
        allocator_pool_names,
        available_pools,
    )


def _build_feature_aware_pools(
    server: Any,
    profile: Dict[str, Any],
    base_pools: Dict[str, Sequence[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    return _pool_runtime._build_feature_aware_pools(
        server,
        profile,
        base_pools,
        pool_candidate_cap=_HOME_POOL_CANDIDATE_CAP,
    )


def _quiet_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any],
) -> List[str]:
    return _pool_runtime._quiet_primary_pool_order(snapshot, allocation_plan)


def _track_list_to_candidates(
    server: Any,
    tracks: Iterable[Dict[str, Any]],
    *,
    generator_name: str,
    base_score: float,
    reason: str,
) -> List[Dict[str, Any]]:
    return _pool_runtime._track_list_to_candidates(
        server,
        tracks,
        generator_name=generator_name,
        base_score=base_score,
        reason=reason,
    )


def _extend_pool(
    server: Any,
    pool: List[Dict[str, Any]],
    candidates: Iterable[Dict[str, Any]],
    *,
    limit: int,
) -> None:
    existing_signatures = {
        signature
        for signature in (
            _candidate_signature(server, candidate)
            for candidate in pool
        )
        if signature
    }
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in existing_signatures:
            continue
        pool.append(copied)
        existing_signatures.add(signature)
        if len(pool) >= limit:
            break


def _home_artist_request(server: Any, profile: Dict[str, Any], *, limit: int) -> Any:
    from .snapshot_support_runtime import home_artist_request as _home_artist_request_impl

    return _home_artist_request_impl(
        server,
        profile,
        limit=limit,
    )


def _artist_rotation_offset(server: Any, profile: Dict[str, Any], item_count: int) -> int:
    from .snapshot_support_runtime import (
        artist_rotation_offset as _artist_rotation_offset_impl,
    )

    return _artist_rotation_offset_impl(server, profile, item_count)


def _recommended_artist_memory_key(user_scope_id: str) -> str:
    from .snapshot_support_runtime import (
        recommended_artist_memory_key as _recommended_artist_memory_key_impl,
    )

    return _recommended_artist_memory_key_impl(user_scope_id)


def _load_recent_artist_memory(server: Any, profile: Dict[str, Any]) -> Set[str]:
    from .snapshot_support_runtime import (
        load_recent_artist_memory as _load_recent_artist_memory_impl,
    )

    return _load_recent_artist_memory_impl(server, profile)


def _store_recent_artist_memory(server: Any, profile: Dict[str, Any], artist_keys: Sequence[str]) -> None:
    from .snapshot_support_runtime import (
        store_recent_artist_memory as _store_recent_artist_memory_impl,
    )

    return _store_recent_artist_memory_impl(server, profile, artist_keys)


def _select_rotated_artists(
    server: Any,
    profile: Dict[str, Any],
    artists: Sequence[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    from .snapshot_support_runtime import (
        select_rotated_artists as _select_rotated_artists_impl,
    )

    return _select_rotated_artists_impl(
        server,
        profile,
        artists,
        limit=limit,
    )


def _build_artist_artifacts(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    from .snapshot_support_runtime import (
        build_artist_artifacts as _build_artist_artifacts_impl,
    )

    return _build_artist_artifacts_impl(
        server,
        profile,
        recommendation_service=_ARTIST_RECOMMENDATION_SERVICE,
        search_artist_seed_tracks_fn=search_artist_seed_tracks,
        recommended_artist_items_fn=_build_profile_artist_items,
    )


def _build_album_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    from .snapshot_support_runtime import build_album_items as _build_album_items_impl

    return _build_album_items_impl(server, profile)


def _build_recommended_artist_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((_build_artist_artifacts(server, profile).get("artists") or []))


def _build_profile_artist_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    from .snapshot_support_runtime import (
        build_profile_artist_items as _build_profile_artist_items_impl,
    )

    return _build_profile_artist_items_impl(server, profile)


def _timed_call(fn, *args, **kwargs):
    from .snapshot_support_runtime import timed_call as _timed_call_impl

    return _timed_call_impl(fn, *args, **kwargs)


def _fetch_anchor_candidate_pools(
    server: Any,
    anchor_tracks: Sequence[Dict[str, Any]],
    recent_track_ids: set[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    from .snapshot_support_runtime import (
        fetch_anchor_candidate_pools as _fetch_anchor_candidate_pools_impl,
    )

    return _fetch_anchor_candidate_pools_impl(
        server,
        anchor_tracks,
        recent_track_ids,
    )


def build_home_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    from .snapshot_builder import build_home_candidate_snapshot as _build_home_candidate_snapshot

    return _build_home_candidate_snapshot(
        server=server,
        profile=profile,
    )


def build_home_candidate_snapshot_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    from .snapshot_builder import (
        build_home_candidate_snapshot_fallback as _build_home_candidate_snapshot_fallback,
    )

    return _build_home_candidate_snapshot_fallback(
        server=server,
        profile=profile,
    )


def build_home_candidate_snapshot_fast_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    from .snapshot_builder import (
        build_home_candidate_snapshot_fast_fallback as _build_home_candidate_snapshot_fast_fallback,
    )

    return _build_home_candidate_snapshot_fast_fallback(
        server=server,
        profile=profile,
    )


def trim_home_candidate_snapshot(
    server: Any,
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    from .snapshot_builder import trim_home_candidate_snapshot as _trim_home_candidate_snapshot

    return _trim_home_candidate_snapshot(
        server,
        snapshot,
    )


def _combine_pools(
    server: Any,
    snapshot: Dict[str, Any],
    pool_names: Sequence[str],
    *,
    limit: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    return _pool_runtime._combine_pools(
        server,
        snapshot,
        pool_names,
        limit=limit,
    )


def _script_bucket(text: str) -> str:
    return _pool_runtime._script_bucket(text)


def _row_affinity_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    return _pool_runtime._row_affinity_profile(server, profile)


def _row_candidate_evidence(
    server: Any,
    profile: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    return _pool_runtime._row_candidate_evidence(server, profile, candidate)


def _post_filter_row_candidates(
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    *,
    relaxed: bool = False,
) -> List[Dict[str, Any]]:
    return _pool_runtime._post_filter_row_candidates(
        server,
        row_kind,
        profile,
        candidates,
        relaxed=relaxed,
    )


def _quiet_extension_pool_names(row_seed: Dict[str, Any]) -> List[str]:
    return _pool_runtime._quiet_extension_pool_names(row_seed)


def _row_extension_pool_names(row_kind: str, row_seed: Dict[str, Any]) -> List[str]:
    return _pool_runtime._row_extension_pool_names(row_kind, row_seed)


def _display_token(value: str) -> str:
    return _specialized_row_helpers.display_token(value)


def _genre_tab_identifier(label: str) -> str:
    return _specialized_row_helpers.genre_tab_identifier(label)


def _trending_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any] | None = None,
) -> List[str]:
    return _pool_runtime._trending_primary_pool_order(snapshot, allocation_plan)


def _sorted_display_tokens(values: Iterable[str], *, limit: int) -> List[str]:
    return _specialized_row_helpers.sorted_display_tokens(values, limit=limit)


def _trending_taste_facets(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.trending_taste_facets(server, profile)


def _trending_facet_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    facet: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.trending_facet_candidates(
        server=server,
        profile=profile,
        facet=facet,
        candidates=candidates,
    )


def _track_artist_label(track: Dict[str, Any]) -> str:
    return _specialized_row_helpers.track_artist_label(track)


def _mix_artist_line(tracks: Sequence[Dict[str, Any]], *, limit: int = 3) -> str:
    return _specialized_row_helpers.mix_artist_line(tracks, limit=limit)


def _theme_accent(seed: str, palette: Sequence[str]) -> str:
    return _specialized_row_helpers.theme_accent(seed, palette)


def _mix_rotation_seed(profile: Dict[str, Any]) -> int:
    return _specialized_row_helpers.mix_rotation_seed(profile)


def _mix_anchor_artists(server: Any, profile: Dict[str, Any], *, limit: int = 5) -> List[str]:
    return _specialized_row_helpers.mix_anchor_artists(server, profile, limit=limit)


def _mix_anchor_genres(server: Any, profile: Dict[str, Any], *, limit: int = 4) -> List[str]:
    return _specialized_row_helpers.mix_anchor_genres(server, profile, limit=limit)


def _mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.mix_blueprints(server, profile)


def _select_mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
    blueprints: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.select_mix_blueprints(
        server,
        profile,
        blueprints,
    )


def _mix_blueprint_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    blueprint: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.mix_blueprint_candidates(
        server=server,
        profile=profile,
        snapshot=snapshot,
        blueprint=blueprint,
        custom_row_candidates_fn=_custom_row_candidates,
    )


def _custom_row_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    row_kind: str,
    pool_names: Sequence[str],
    limit: int,
    relaxed: bool = False,
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.custom_row_candidates(
        server=server,
        profile=profile,
        snapshot=snapshot,
        row_kind=row_kind,
        pool_names=pool_names,
        limit=limit,
        relaxed=relaxed,
        combine_pools_fn=_combine_pools,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
    )


def _finalize_custom_track_items(
    *,
    server: Any,
    profile: Dict[str, Any],
    ranking_row_kind: str,
    title: str,
    candidates: Sequence[Dict[str, Any]],
    limit: int,
    used_track_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    return _specialized_row_helpers.finalize_custom_track_items(
        server=server,
        profile=profile,
        ranking_row_kind=ranking_row_kind,
        title=title,
        candidates=candidates,
        limit=limit,
        used_track_ids=used_track_ids,
        finalize_row_items_fn=finalize_row_items,
    )


def snapshot_substrate_mode(snapshot: Dict[str, Any] | None) -> str:
    from .snapshot_builder import snapshot_substrate_mode as _snapshot_substrate_mode

    return _snapshot_substrate_mode(snapshot)


def _build_continue_listening_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
) -> Dict[str, Any] | None:
    from .specialized_row_seed_runtime import (
        build_continue_listening_seed as _build_continue_listening_seed_impl,
    )

    return _build_continue_listening_seed_impl(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        combine_pools_fn=_combine_pools,
        snapshot_substrate_mode_fn=snapshot_substrate_mode,
    )


def build_continue_listening_row(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    from .row_seed_builder import build_continue_listening_row as _build_continue_listening_row

    return _build_continue_listening_row(
        server=server,
        profile=profile,
        snapshot=snapshot,
        build_continue_listening_seed_fn=_build_continue_listening_seed,
    )


def _build_todays_pick_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
) -> Dict[str, Any] | None:
    from .specialized_row_seed_runtime import (
        build_todays_pick_seed as _build_todays_pick_seed_impl,
    )

    return _build_todays_pick_seed_impl(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        launch_tier_only=launch_tier_only,
        custom_row_candidates_fn=_custom_row_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        track_artist_label_fn=_track_artist_label,
        theme_accent_fn=_theme_accent,
    )


def _build_mixed_for_you_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
) -> Dict[str, Any] | None:
    from .specialized_row_seed_runtime import (
        build_mixed_for_you_seed as _build_mixed_for_you_seed_impl,
    )

    return _build_mixed_for_you_seed_impl(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        launch_tier_only=launch_tier_only,
        snapshot_substrate_mode_fn=snapshot_substrate_mode,
        select_mix_blueprints_fn=_select_mix_blueprints,
        mix_blueprints_fn=_mix_blueprints,
        mix_blueprint_candidates_fn=_mix_blueprint_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        theme_accent_fn=_theme_accent,
        mix_artist_line_fn=_mix_artist_line,
    )


def _build_trending_by_genre_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
) -> Dict[str, Any] | None:
    from .specialized_row_seed_runtime import (
        build_trending_by_genre_seed as _build_trending_by_genre_seed_impl,
    )

    return _build_trending_by_genre_seed_impl(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        trending_genre_tabs_fn=_build_trending_genre_tabs,
    )


def _build_trending_genre_tabs(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    preferred_tab_ids: Sequence[str] | None = None,
    selected_tab_id: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
    from .specialized_row_seed_runtime import (
        build_trending_genre_tabs as _build_trending_genre_tabs_impl,
    )

    return _build_trending_genre_tabs_impl(
        server=server,
        profile=profile,
        snapshot=snapshot,
        preferred_tab_ids=preferred_tab_ids,
        selected_tab_id=selected_tab_id,
        build_row_allocation_plan_fn=build_row_allocation_plan,
        trending_primary_pool_order_fn=_trending_primary_pool_order,
        combine_pools_fn=_combine_pools,
        trending_taste_facets_fn=_trending_taste_facets,
        trending_facet_candidates_fn=_trending_facet_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        mix_artist_line_fn=_mix_artist_line,
        genre_tab_identifier_fn=_genre_tab_identifier,
    )


def refresh_trending_by_genre_row(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    tab_id: str,
) -> Dict[str, Any]:
    from .row_seed_builder import refresh_trending_by_genre_row as _refresh_trending_by_genre_row

    return _refresh_trending_by_genre_row(
        server=server,
        row=row,
        profile=profile,
        snapshot=snapshot,
        tab_id=tab_id,
        build_trending_genre_tabs_fn=_build_trending_genre_tabs,
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
) -> Dict[str, Any] | None:
    from .row_seed_builder import build_row_seed as _build_row_seed

    return _build_row_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        relaxed_filter=relaxed_filter,
        pool_names_override=pool_names_override,
        candidate_limit_override=candidate_limit_override,
        allow_empty_diagnostics=allow_empty_diagnostics,
        launch_tier_only=launch_tier_only,
        full_refinement=full_refinement,
    )


def build_required_fallback_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, Any] | None:
    from .row_seed_builder import build_required_fallback_seed as _build_required_fallback_seed

    return _build_required_fallback_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        build_row_seed_fn=build_row_seed,
    )


def finalize_row_items(
    *,
    server: Any,
    row_kind: str,
    title: str,
    candidates: Iterable[Dict[str, Any]],
    profile: Dict[str, Any],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int] | None = None,
    enforce_feed_artist_cap: bool = True,
    max_items: int = 18,
    embedding_lookup: Dict[str, List[float]] | None = None,
    metadata_enrich_limit: int | None = None,
) -> Dict[str, Any] | None:
    return _row_item_finalizer.finalize_row_items(
        server=server,
        row_kind=row_kind,
        title=title,
        candidates=candidates,
        profile=profile,
        used_track_ids=used_track_ids,
        used_artist_counts=used_artist_counts,
        enforce_feed_artist_cap=enforce_feed_artist_cap,
        max_items=max_items,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
        track_score_fn=track_score,
    )


def _quiet_replenishment_query_plan(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    limit: int | None = None,
) -> List[str]:
    from .quiet_replenishment_runtime import (
        quiet_replenishment_query_plan as _quiet_replenishment_query_plan_impl,
    )

    return _quiet_replenishment_query_plan_impl(
        server=server,
        row=row,
        profile=profile,
        limit=limit,
    )


def _quiet_replenishment_candidates(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    page_size: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    from .quiet_replenishment_runtime import (
        quiet_replenishment_candidates as _quiet_replenishment_candidates_impl,
    )

    return _quiet_replenishment_candidates_impl(
        server=server,
        row=row,
        profile=profile,
        page_size=page_size,
    )


def extend_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
) -> Dict[str, Any]:
    from .row_extension_runtime import extend_row_from_snapshot as _extend_row_from_snapshot

    return _extend_row_from_snapshot(
        server=server,
        row=row,
        profile=profile,
        snapshot=snapshot,
        page_size=page_size,
        build_row_seed_fn=build_row_seed,
        finalize_row_items_fn=finalize_row_items,
        combine_pools_fn=_combine_pools,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
        row_extension_pool_names_fn=_row_extension_pool_names,
        candidate_signature_fn=_candidate_signature,
        quiet_replenishment_candidates_fn=_quiet_replenishment_candidates,
        quiet_replenishment_query_plan_fn=_quiet_replenishment_query_plan,
    )


def extend_quiet_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
) -> Dict[str, Any]:
    from .row_extension_runtime import (
        extend_quiet_row_from_snapshot as _extend_quiet_row_from_snapshot,
    )

    return _extend_quiet_row_from_snapshot(
        server=server,
        row=row,
        profile=profile,
        snapshot=snapshot,
        page_size=page_size,
        build_row_seed_fn=build_row_seed,
        finalize_row_items_fn=finalize_row_items,
        combine_pools_fn=_combine_pools,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
        row_extension_pool_names_fn=_row_extension_pool_names,
        candidate_signature_fn=_candidate_signature,
        quiet_replenishment_candidates_fn=_quiet_replenishment_candidates,
        quiet_replenishment_query_plan_fn=_quiet_replenishment_query_plan,
    )
