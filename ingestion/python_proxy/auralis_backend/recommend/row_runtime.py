from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from .candidate_pipeline import execute_candidate_pipeline, resolve_candidate_snapshot
from .row_item_finalizer import (
    _artist_family_identity,
    _track_authenticity_penalty,
    apply_track_row_runtime_fields,
    apply_quiet_row_runtime_fields,
    finalize_row_items,
)
from .row_seed_builder import build_row_seed
from .row_finalization_pipeline import finalize_row_seed_execution
from .policy import is_required_row, required_row_kinds, row_kinds as policy_row_kinds


RECOMMEND_INITIAL_QUIET_ITEMS = max(
    12,
    int(os.environ.get("AURALIS_RECOMMEND_INITIAL_QUIET_ITEMS", "16")),
)
RECOMMEND_INITIAL_TRACK_BANK_ITEMS = max(
    20,
    int(os.environ.get("AURALIS_RECOMMEND_INITIAL_TRACK_BANK_ITEMS", "24")),
)
RECOMMEND_EXPANDED_QUIET_BANK_ITEMS = max(
    RECOMMEND_INITIAL_QUIET_ITEMS + 12,
    int(os.environ.get("AURALIS_RECOMMEND_EXPANDED_QUIET_BANK_ITEMS", "64")),
)
RECOMMEND_EXPANDED_TRACK_BANK_ITEMS = max(
    RECOMMEND_INITIAL_TRACK_BANK_ITEMS + 12,
    int(os.environ.get("AURALIS_RECOMMEND_EXPANDED_TRACK_BANK_ITEMS", "48")),
)
ROW_KIND_ORDER = {
    row_kind: index
    for index, row_kind in enumerate(policy_row_kinds())
}
QUALITY_CRITICAL_ROWS = (
    "continue_listening",
    "because_you_played",
    "trending_for_you",
)


def _expanded_bank_limit(row_kind: str) -> int:
    return (
        RECOMMEND_EXPANDED_QUIET_BANK_ITEMS
        if row_kind == "quiet_picks"
        else RECOMMEND_EXPANDED_TRACK_BANK_ITEMS
    )


def _track_signature_set(server: Any, items: List[Dict[str, Any]]) -> set[str]:
    return {
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in (items or [])
        )
        if signature
    }


def _track_artist_counts(server: Any, items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for track in items or []:
        if not isinstance(track, dict):
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        if artist_key:
            counts[artist_key] = int(counts.get(artist_key) or 0) + 1
    return counts


def _expand_row_bank(
    *,
    server: Any,
    row_kind: str,
    finalized: Dict[str, Any],
    row_seed: Dict[str, Any],
    profile: Dict[str, Any],
    embedding_lookup: Dict[str, List[float]] | None = None,
    metadata_enrich_limit: int | None = None,
) -> Dict[str, Any]:
    updated = dict(finalized or {})
    primary_items = list(updated.get("items") or [])
    bank_limit = _expanded_bank_limit(row_kind)
    if len(primary_items) >= bank_limit:
        return updated

    used_track_ids = _track_signature_set(server, primary_items)
    extra_finalized = finalize_row_items(
        server=server,
        row_kind=row_kind,
        title=updated.get("title") or row_seed.get("title") or "",
        candidates=row_seed.get("candidates") or [],
        profile=profile,
        used_track_ids=set(used_track_ids),
        used_artist_counts=_track_artist_counts(server, primary_items),
        enforce_feed_artist_cap=False,
        max_items=max(bank_limit - len(primary_items), 0),
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
    )
    if extra_finalized is None:
        return updated

    merged_items = list(primary_items)
    for track in list(extra_finalized.get("items") or []):
        signature = server._recommendation_track_signature(track)
        if not signature or signature in used_track_ids:
            continue
        used_track_ids.add(signature)
        merged_items.append(track)
        if len(merged_items) >= bank_limit:
            break
    if len(merged_items) <= len(primary_items):
        return updated

    updated["items"] = merged_items
    diagnostics = dict(updated.get("_diagnostics") or {})
    diagnostics["selected_count_primary"] = len(primary_items)
    diagnostics["selected_count_bank"] = len(merged_items)
    updated["_diagnostics"] = diagnostics
    return updated


def merge_home_rows(
    launch_rows: List[Dict[str, Any]] | None,
    heavy_rows: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    merged = [dict(row or {}) for row in list(launch_rows or [])]
    merged.extend(dict(row or {}) for row in list(heavy_rows or []))
    merged.sort(key=lambda row: ROW_KIND_ORDER.get(row.get("kind"), 100))
    return merged


def _filter_prebuilt_track_items(
    *,
    server: Any,
    items: List[Dict[str, Any]],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int],
    limit: int = 18,
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    artist_family_counts: Dict[str, int] = {}
    for track in list(items or []):
        if not isinstance(track, dict):
            continue
        signature = server._recommendation_track_signature(track)
        if not signature or signature in used_track_ids:
            continue
        if _track_authenticity_penalty(track) >= 0.75:
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        artist_family_key = _artist_family_identity(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        if artist_key and int(used_artist_counts.get(artist_key) or 0) >= 3:
            continue
        if (
            artist_family_key
            and int(artist_family_counts.get(artist_family_key) or 0) >= 2
        ):
            continue
        filtered.append(dict(track))
        used_track_ids.add(signature)
        if artist_key:
            used_artist_counts[artist_key] = int(used_artist_counts.get(artist_key) or 0) + 1
        if artist_family_key:
            artist_family_counts[artist_family_key] = (
                int(artist_family_counts.get(artist_family_key) or 0) + 1
            )
        if len(filtered) >= limit:
            break
    return filtered


def _finalize_row_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    row_seed: Dict[str, Any],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int],
    embedding_lookup: Dict[str, List[float]] | None = None,
    metadata_enrich_limit: int | None = None,
    enforce_feed_artist_cap: bool = True,
) -> Tuple[Dict[str, Any] | None, Dict[str, Any]]:
    return finalize_row_seed_execution(
        server=server,
        profile=profile,
        row_seed=row_seed,
        used_track_ids=used_track_ids,
        used_artist_counts=used_artist_counts,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
        enforce_feed_artist_cap=enforce_feed_artist_cap,
        initial_quiet_items=RECOMMEND_INITIAL_QUIET_ITEMS,
        initial_track_bank_items=RECOMMEND_INITIAL_TRACK_BANK_ITEMS,
        filter_prebuilt_track_items_fn=_filter_prebuilt_track_items,
        finalize_row_items_fn=finalize_row_items,
        expand_row_bank_fn=_expand_row_bank,
        apply_track_row_runtime_fields_fn=apply_track_row_runtime_fields,
        apply_quiet_row_runtime_fields_fn=apply_quiet_row_runtime_fields,
    )


def _prepare_candidate_embeddings(
    *,
    server: Any,
    row_seeds: List[Dict[str, Any]],
) -> Dict[str, List[float]]:
    tracks: List[Dict[str, Any]] = []
    seen_keys = set()
    for row_seed in row_seeds:
        if row_seed.get("item_type") in {"album", "artist"}:
            continue
        for raw_candidate in list(row_seed.get("candidates") or []):
            if not isinstance(raw_candidate, dict):
                continue
            raw_track = (
                raw_candidate.get("track")
                if isinstance(raw_candidate.get("track"), dict)
                else raw_candidate
            )
            normalized_track = server.normalize_recommendation_track(raw_track)
            if normalized_track is None:
                continue
            if isinstance(raw_track, dict):
                normalized_track = server._merge_track_metadata(
                    raw_track,
                    normalized_track,
                )
            embedding_key = server._recommendation_track_embedding_key(
                normalized_track
            )
            if not embedding_key or embedding_key in seen_keys:
                continue
            seen_keys.add(embedding_key)
            tracks.append(normalized_track)
    if not tracks:
        return {}
    return server._recommendation_track_embeddings(tracks)


def _build_rows_from_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
    candidate_snapshot: Dict[str, Any],
    precompute_hit: bool,
    allow_required_fallback: bool,
    force_rich_rows: bool = False,
    launch_tier_only: bool = False,
    trace: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    resolution = resolve_candidate_snapshot(
        server=server,
        profile=profile,
        precompute_snapshot={"candidate_snapshot": dict(candidate_snapshot or {})}
        if isinstance(candidate_snapshot, dict)
        else None,
        allow_live_snapshot_build=False,
        force_rich_rows=force_rich_rows,
        launch_tier_only=launch_tier_only,
        trace=trace,
    )
    resolution = type(resolution)(
        candidate_snapshot=dict(candidate_snapshot or {}),
        precompute_hit=precompute_hit,
        allow_required_fallback=allow_required_fallback,
        substrate_mode=resolution.substrate_mode,
        selected_row_kinds=resolution.selected_row_kinds,
        deferred_row_kinds=resolution.deferred_row_kinds,
        row_builder_mode=resolution.row_builder_mode,
        launch_tier_only=resolution.launch_tier_only,
    )
    return execute_candidate_pipeline(
        server=server,
        profile=profile,
        resolution=resolution,
        trace=trace,
        prepare_embeddings=_prepare_candidate_embeddings,
        finalize_row_seed=_finalize_row_seed,
    ).as_legacy_tuple()


def build_rows_v41(
    *,
    server: Any,
    profile: Dict[str, Any],
    precompute_snapshot: Dict[str, Any] | None = None,
    trace: Dict[str, Any] | None = None,
    allow_live_snapshot_build: bool = False,
    force_rich_rows: bool = False,
    launch_tier_only: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    resolution = resolve_candidate_snapshot(
        server=server,
        profile=profile,
        precompute_snapshot=precompute_snapshot,
        allow_live_snapshot_build=allow_live_snapshot_build,
        force_rich_rows=force_rich_rows,
        launch_tier_only=launch_tier_only,
        trace=trace,
    )
    try:
        return execute_candidate_pipeline(
            server=server,
            profile=profile,
            resolution=resolution,
            trace=trace,
            prepare_embeddings=_prepare_candidate_embeddings,
            finalize_row_seed=_finalize_row_seed,
        ).as_legacy_tuple()
    except Exception as exc:
        server._trace_put(
            trace,
            "errors",
            "recommend.candidate_snapshot_error",
            str(exc)[:240],
        )
        degraded_resolution = resolve_candidate_snapshot(
            server=server,
            profile=profile,
            precompute_snapshot=None,
            allow_live_snapshot_build=False,
            force_rich_rows=False,
            launch_tier_only=launch_tier_only,
            trace=trace,
        )
        degraded_snapshot = dict(degraded_resolution.candidate_snapshot or {})
        degraded_snapshot["resolved_from"] = "request_fallback_error"
        degraded_resolution = type(degraded_resolution)(
            candidate_snapshot=degraded_snapshot,
            precompute_hit=False,
            allow_required_fallback=True,
            substrate_mode=degraded_resolution.substrate_mode,
            selected_row_kinds=degraded_resolution.selected_row_kinds,
            deferred_row_kinds=degraded_resolution.deferred_row_kinds,
            row_builder_mode=degraded_resolution.row_builder_mode,
            launch_tier_only=degraded_resolution.launch_tier_only,
        )
        return execute_candidate_pipeline(
            server=server,
            profile=profile,
            resolution=degraded_resolution,
            trace=trace,
            prepare_embeddings=_prepare_candidate_embeddings,
            finalize_row_seed=_finalize_row_seed,
        ).as_legacy_tuple()
