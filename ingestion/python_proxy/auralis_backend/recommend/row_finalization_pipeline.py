from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from .policy import is_required_row


def finalize_row_seed_execution(
    *,
    server: Any,
    profile: Dict[str, Any],
    row_seed: Dict[str, Any],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int],
    embedding_lookup: Dict[str, List[float]] | None,
    metadata_enrich_limit: int | None,
    enforce_feed_artist_cap: bool,
    initial_quiet_items: int,
    initial_track_bank_items: int,
    filter_prebuilt_track_items_fn: Callable[..., List[Dict[str, Any]]],
    finalize_row_items_fn: Callable[..., Dict[str, Any] | None],
    expand_row_bank_fn: Callable[..., Dict[str, Any]],
    apply_track_row_runtime_fields_fn: Callable[..., Dict[str, Any]],
    apply_quiet_row_runtime_fields_fn: Callable[..., Dict[str, Any]],
) -> Tuple[Dict[str, Any] | None, Dict[str, Any]]:
    row_kind = row_seed["kind"]
    row_strategy = row_seed.get("row_strategy") or "personalized"
    fallback_reason = row_seed.get("fallback_reason") or ""
    source_pool_counts = dict(row_seed.get("source_pool_counts") or {})
    allocator_ms = int(row_seed.get("allocator_ms") or 0)
    candidate_count_input = int(
        row_seed.get("candidate_count_input") or len(row_seed.get("candidates") or [])
    )
    candidate_count_filtered = int(
        row_seed.get("candidate_count_filtered") or len(row_seed.get("candidates") or [])
    )
    prebuilt_items = isinstance(row_seed.get("items"), list) and not row_seed.get("candidates")
    if not prebuilt_items and not list(row_seed.get("candidates") or []):
        failure_status = str(row_seed.get("seed_failure_stage") or "seed_pool_empty").strip() or "seed_pool_empty"
        diagnostics = {
            "builder": f"candidate_snapshot:{row_kind}",
            "builder_ms": allocator_ms,
            "status": failure_status,
            "required": is_required_row(server, row_kind),
            "candidate_count_input": candidate_count_input,
            "candidate_count_filtered": candidate_count_filtered,
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "source_pool_counts": source_pool_counts,
        }
        return None, diagnostics
    if prebuilt_items:
        row = {
            "id": row_kind,
            "kind": row_kind,
            "title": row_seed["title"],
            "item_type": row_seed.get("item_type") or "track",
            "row_style": row_seed.get("row_style") or "",
            "meta": dict(row_seed.get("meta") or {}),
            "items": list(row_seed.get("items") or [])[:18],
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "can_extend": False,
        }
        if row.get("item_type") == "track":
            row_style = str(row.get("row_style") or "").strip()
            if row_style == "genre_tabs":
                meta = dict(row.get("meta") or {})
                filtered_tabs: List[Dict[str, Any]] = []
                for raw_tab in list(meta.get("tabs") or []):
                    if not isinstance(raw_tab, dict):
                        continue
                    filtered_tracks = filter_prebuilt_track_items_fn(
                        server=server,
                        items=list(raw_tab.get("tracks") or []),
                        used_track_ids=set(),
                        used_artist_counts={},
                        limit=6,
                    )
                    if not filtered_tracks:
                        continue
                    tab = dict(raw_tab)
                    tab["tracks"] = filtered_tracks
                    filtered_tabs.append(tab)
                if len(filtered_tabs) < 2:
                    diagnostics = {
                        "builder": f"candidate_snapshot:{row_kind}",
                        "builder_ms": allocator_ms,
                        "status": "filtered_out",
                        "required": is_required_row(server, row_kind),
                        "candidate_count": len(row_seed.get("items") or []),
                        "row_strategy": row_strategy,
                        "fallback_reason": fallback_reason,
                        "source_pool_counts": source_pool_counts,
                    }
                    return None, diagnostics
                active_tab_id = str(meta.get("active_tab_id") or "").strip()
                active_tab = next(
                    (
                        tab
                        for tab in filtered_tabs
                        if str(tab.get("id") or "").strip() == active_tab_id
                    ),
                    filtered_tabs[0],
                )
                row["items"] = list(active_tab.get("tracks") or [])[:6]
                meta["tabs"] = filtered_tabs
                meta["active_tab_id"] = str(active_tab.get("id") or "").strip()
                row["meta"] = meta
                active_tab_tracks = filter_prebuilt_track_items_fn(
                    server=server,
                    items=list(active_tab.get("tracks") or []),
                    used_track_ids=set(used_track_ids),
                    used_artist_counts=dict(used_artist_counts),
                    limit=6,
                )
                if len(active_tab_tracks) >= min(2, len(list(active_tab.get("tracks") or []))):
                    row["items"] = active_tab_tracks
                for track in list(row.get("items") or []):
                    signature = server._recommendation_track_signature(track)
                    if signature:
                        used_track_ids.add(signature)
                    artist_key = server._normalize_text(
                        track.get("channel") or track.get("artist") or track.get("author") or ""
                    )
                    if artist_key:
                        used_artist_counts[artist_key] = int(used_artist_counts.get(artist_key) or 0) + 1
            else:
                row["items"] = filter_prebuilt_track_items_fn(
                    server=server,
                    items=list(row.get("items") or []),
                    used_track_ids=used_track_ids,
                    used_artist_counts=used_artist_counts,
                    limit=18,
                )
            if not list(row.get("items") or []):
                diagnostics = {
                    "builder": f"candidate_snapshot:{row_kind}",
                    "builder_ms": allocator_ms,
                    "status": "filtered_out",
                    "required": is_required_row(server, row_kind),
                    "candidate_count": len(row_seed.get("items") or []),
                    "row_strategy": row_strategy,
                    "fallback_reason": fallback_reason,
                    "source_pool_counts": source_pool_counts,
                }
                return None, diagnostics
            row = apply_track_row_runtime_fields_fn(
                server=server,
                finalized=row,
                row_seed=row_seed,
            )
        diagnostics = {
            "builder": f"candidate_snapshot:{row_kind}",
            "builder_ms": allocator_ms,
            "status": "emitted" if row.get("items") else "empty",
            "required": is_required_row(server, row_kind),
            "item_count": len(row.get("items") or []),
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "source_pool_counts": source_pool_counts,
        }
        return row, diagnostics

    if row_seed.get("item_type") in {"album", "artist"}:
        row = {
            "id": row_kind,
            "kind": row_kind,
            "title": row_seed["title"],
            "item_type": row_seed.get("item_type") or "track",
            "row_style": row_seed.get("row_style") or "",
            "meta": dict(row_seed.get("meta") or {}),
            "items": list(row_seed.get("items") or [])[:18],
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
        }
        diagnostics = {
            "builder": f"candidate_snapshot:{row_kind}",
            "builder_ms": allocator_ms,
            "status": "emitted" if row.get("items") else "empty",
            "required": is_required_row(server, row_kind),
            "item_count": len(row.get("items") or []),
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "source_pool_counts": source_pool_counts,
        }
        return row, diagnostics

    max_items = (
        initial_quiet_items
        if row_kind == "quiet_picks"
        else initial_track_bank_items
    )
    local_used_track_ids = set(used_track_ids)
    local_used_artist_counts = dict(used_artist_counts)
    finalized = finalize_row_items_fn(
        server=server,
        row_kind=row_kind,
        title=row_seed["title"],
        candidates=row_seed.get("candidates") or [],
        profile=profile,
        used_track_ids=local_used_track_ids,
        used_artist_counts=local_used_artist_counts,
        enforce_feed_artist_cap=enforce_feed_artist_cap,
        max_items=max_items,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
    )
    if finalized is None and row_kind == "quiet_picks" and enforce_feed_artist_cap:
        local_used_track_ids = set(used_track_ids)
        local_used_artist_counts = dict(used_artist_counts)
        finalized = finalize_row_items_fn(
            server=server,
            row_kind=row_kind,
            title=row_seed["title"],
            candidates=row_seed.get("candidates") or [],
            profile=profile,
            used_track_ids=local_used_track_ids,
            used_artist_counts=local_used_artist_counts,
            enforce_feed_artist_cap=False,
            max_items=max_items,
            embedding_lookup=embedding_lookup,
            metadata_enrich_limit=metadata_enrich_limit,
        )
        if isinstance(finalized, dict):
            diagnostics = dict(finalized.get("_diagnostics") or {})
            diagnostics["feed_artist_cap_relaxed"] = True
            finalized["_diagnostics"] = diagnostics
    if finalized is None:
        diagnostics = {
            "builder": f"candidate_snapshot:{row_kind}",
            "builder_ms": allocator_ms,
            "status": "finalize_filtered_out",
            "required": is_required_row(server, row_kind),
            "candidate_count": len(row_seed.get("candidates") or []),
            "candidate_count_input": candidate_count_input,
            "candidate_count_filtered": candidate_count_filtered,
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "source_pool_counts": source_pool_counts,
        }
        return None, diagnostics

    finalized = expand_row_bank_fn(
        server=server,
        row_kind=row_kind,
        finalized=finalized,
        row_seed=row_seed,
        profile=profile,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
    )
    finalize_diagnostics = {}
    if isinstance(finalized.get("_diagnostics"), dict):
        finalize_diagnostics = dict(finalized.pop("_diagnostics"))
        finalized["diagnostics"] = dict(finalize_diagnostics)
    finalized = apply_track_row_runtime_fields_fn(
        server=server,
        finalized=finalized,
        row_seed=row_seed,
    )
    if row_kind == "quiet_picks":
        finalized = apply_quiet_row_runtime_fields_fn(
            server=server,
            finalized=finalized,
            row_seed=row_seed,
        )
    finalized["row_strategy"] = row_strategy
    finalized["fallback_reason"] = fallback_reason
    used_track_ids.update(local_used_track_ids)
    used_artist_counts.update(local_used_artist_counts)
    diagnostics = {
        "builder": f"candidate_snapshot:{row_kind}",
        "builder_ms": allocator_ms,
        "status": "emitted",
        "required": is_required_row(server, row_kind),
        "candidate_count": len(row_seed.get("candidates") or []),
        "item_count": len(finalized.get("items") or []),
        "row_strategy": row_strategy,
        "fallback_reason": fallback_reason,
        "source_pool_counts": source_pool_counts,
    }
    if finalize_diagnostics:
        diagnostics["candidate_count_merged"] = int(
            finalize_diagnostics.get("candidate_count_merged") or 0
        )
        diagnostics["source_counts"] = dict(
            finalize_diagnostics.get("source_counts") or {}
        )
        diagnostics["selected_source_counts"] = dict(
            finalize_diagnostics.get("selected_source_counts") or {}
        )
        diagnostics["ranking_model_key"] = (
            finalize_diagnostics.get("model_key") or ""
        )
        diagnostics["ranking_model_version"] = (
            finalize_diagnostics.get("model_version") or ""
        )
        diagnostics["row_feature_mix"] = dict(
            finalize_diagnostics.get("row_feature_mix") or {}
        )
    return finalized, diagnostics
