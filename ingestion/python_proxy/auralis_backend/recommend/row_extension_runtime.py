from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from .policy import row_title
from .pool_runtime import (
    _candidate_signature,
    _combine_pools,
    _post_filter_row_candidates,
    _row_extension_pool_names,
)
from .quiet_replenishment_runtime import (
    quiet_replenishment_candidates,
    quiet_replenishment_query_plan,
)
from .row_item_finalizer import finalize_row_items
from .row_seed_builder import build_row_seed


def extend_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
    build_row_seed_fn=None,
    finalize_row_items_fn=None,
    combine_pools_fn=None,
    post_filter_row_candidates_fn=None,
    row_extension_pool_names_fn=None,
    candidate_signature_fn=None,
    quiet_replenishment_candidates_fn=None,
    quiet_replenishment_query_plan_fn=None,
) -> Dict[str, Any]:
    build_row_seed_fn = build_row_seed_fn or build_row_seed
    finalize_row_items_fn = finalize_row_items_fn or finalize_row_items
    combine_pools_fn = combine_pools_fn or _combine_pools
    post_filter_row_candidates_fn = (
        post_filter_row_candidates_fn or _post_filter_row_candidates
    )
    row_extension_pool_names_fn = row_extension_pool_names_fn or _row_extension_pool_names
    candidate_signature_fn = candidate_signature_fn or _candidate_signature
    quiet_replenishment_candidates_fn = (
        quiet_replenishment_candidates_fn or quiet_replenishment_candidates
    )
    quiet_replenishment_query_plan_fn = (
        quiet_replenishment_query_plan_fn or quiet_replenishment_query_plan
    )
    if not isinstance(row, dict):
        return row
    extended_row = dict(row)
    row_kind = server._recommendation_trim_text(extended_row.get("kind"))
    item_type = server._recommendation_trim_text(
        extended_row.get("item_type") or "track"
    )
    if item_type and item_type != "track":
        return extended_row
    row_seed = build_row_seed_fn(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        candidate_limit_override=max(
            len(extended_row.get("items") or []) + (page_size * 10),
            96,
        ),
    )
    if not isinstance(row_seed, dict):
        extended_row["can_extend"] = False
        return extended_row

    existing_items = list(extended_row.get("items") or [])
    existing_signatures = {
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in existing_items
        )
        if signature
    }
    existing_signatures.update(
        signature
        for signature in (extended_row.get("used_signatures") or [])
        if server._recommendation_trim_text(signature)
    )
    extension_candidates, _source_pool_counts = combine_pools_fn(
        server,
        snapshot,
        row_extension_pool_names_fn(row_kind, row_seed),
        limit=max(len(existing_items) + (page_size * 8), 96),
    )
    extension_candidates = post_filter_row_candidates_fn(
        server,
        row_kind,
        profile,
        extension_candidates,
    )
    target_bank_size = max(
        len(existing_items) + (page_size * 4),
        64 if row_kind == "quiet_picks" else 40,
    )
    target_new_items = max(target_bank_size - len(existing_items), page_size * 3, 18)
    existing_artist_counts: Dict[str, int] = defaultdict(int)
    for track in existing_items:
        if not isinstance(track, dict):
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        if artist_key:
            existing_artist_counts[artist_key] += 1
    finalized = finalize_row_items_fn(
        server=server,
        row_kind=row_kind,
        title=extended_row.get("title") or row_seed.get("title") or row_title(row_kind, profile),
        candidates=extension_candidates,
        profile=profile,
        used_track_ids=set(existing_signatures),
        used_artist_counts=dict(existing_artist_counts),
        max_items=target_new_items,
    )
    new_items = []
    for track in (finalized or {}).get("items") or []:
        signature = server._recommendation_track_signature(track)
        if not signature or signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        new_items.append(track)
    replenishment_queries: List[str] = []
    replenishment_candidates: List[Dict[str, Any]] = []
    if row_kind == "quiet_picks" and not new_items:
        replenishment_candidates, replenishment_queries = quiet_replenishment_candidates_fn(
            server=server,
            row=extended_row,
            profile=profile,
            page_size=page_size,
        )
        if replenishment_candidates:
            replenished = finalize_row_items_fn(
                server=server,
                row_kind=row_kind,
                title=extended_row.get("title")
                or row_seed.get("title")
                or row_title(row_kind, profile),
                candidates=replenishment_candidates,
                profile=profile,
                used_track_ids=set(existing_signatures),
                used_artist_counts=dict(existing_artist_counts),
                max_items=target_new_items,
            )
            if replenished is None:
                replenished = finalize_row_items_fn(
                    server=server,
                    row_kind=row_kind,
                    title=extended_row.get("title")
                    or row_seed.get("title")
                    or row_title(row_kind, profile),
                    candidates=replenishment_candidates,
                    profile=profile,
                    used_track_ids=set(existing_signatures),
                    used_artist_counts=dict(existing_artist_counts),
                    enforce_feed_artist_cap=False,
                    max_items=target_new_items,
                )
            for track in (replenished or {}).get("items") or []:
                signature = server._recommendation_track_signature(track)
                if not signature or signature in existing_signatures:
                    continue
                existing_signatures.add(signature)
                new_items.append(track)
    extended_row["used_signatures"] = list(existing_signatures)
    existing_used_queries = [
        query
        for query in (extended_row.get("used_queries") or [])
        if server._recommendation_trim_text(query)
    ]
    extended_row["used_queries"] = list(
        dict.fromkeys([*existing_used_queries, *replenishment_queries])
    )
    extended_row["extension_cycle"] = max(int(extended_row.get("extension_cycle") or 0), 0) + 1
    if new_items:
        extended_row["items"] = existing_items + new_items
    else:
        extended_row["can_extend"] = False
        return extended_row
    remaining_available = 0
    for candidate in extension_candidates:
        signature = candidate_signature_fn(server, candidate)
        if signature and signature not in existing_signatures:
            remaining_available += 1
    if row_kind == "quiet_picks" and replenishment_candidates:
        for candidate in replenishment_candidates:
            signature = candidate_signature_fn(server, candidate)
            if signature and signature not in existing_signatures:
                remaining_available += 1
    quiet_queries_remaining = False
    if row_kind == "quiet_picks":
        quiet_queries_remaining = bool(
            quiet_replenishment_query_plan_fn(
                server=server,
                row=extended_row,
                profile=profile,
                limit=8,
            )
        )
    extended_row["can_extend"] = remaining_available > 0 or quiet_queries_remaining
    return extended_row


def extend_quiet_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
    build_row_seed_fn=None,
    finalize_row_items_fn=None,
    combine_pools_fn=None,
    post_filter_row_candidates_fn=None,
    row_extension_pool_names_fn=None,
    candidate_signature_fn=None,
    quiet_replenishment_candidates_fn=None,
    quiet_replenishment_query_plan_fn=None,
) -> Dict[str, Any]:
    return extend_row_from_snapshot(
        server=server,
        row=row,
        profile=profile,
        snapshot=snapshot,
        page_size=page_size,
        build_row_seed_fn=build_row_seed_fn,
        finalize_row_items_fn=finalize_row_items_fn,
        combine_pools_fn=combine_pools_fn,
        post_filter_row_candidates_fn=post_filter_row_candidates_fn,
        row_extension_pool_names_fn=row_extension_pool_names_fn,
        candidate_signature_fn=candidate_signature_fn,
        quiet_replenishment_candidates_fn=quiet_replenishment_candidates_fn,
        quiet_replenishment_query_plan_fn=quiet_replenishment_query_plan_fn,
    )
