from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from .home_pipeline import (
    apply_track_row_runtime_fields,
    apply_quiet_row_runtime_fields,
    build_home_candidate_snapshot,
    build_home_candidate_snapshot_fallback,
    build_required_fallback_seed,
    build_row_seed,
    finalize_row_items,
)
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
    "quiet_picks",
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
    row_kind = row_seed["kind"]
    row_strategy = row_seed.get("row_strategy") or "personalized"
    fallback_reason = row_seed.get("fallback_reason") or ""
    source_pool_counts = dict(row_seed.get("source_pool_counts") or {})
    allocator_ms = int(row_seed.get("allocator_ms") or 0)

    if row_seed.get("item_type") in {"album", "artist"}:
        row = {
            "id": row_kind,
            "kind": row_kind,
            "title": row_seed["title"],
            "item_type": row_seed.get("item_type") or "track",
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
        RECOMMEND_INITIAL_QUIET_ITEMS
        if row_kind == "quiet_picks"
        else RECOMMEND_INITIAL_TRACK_BANK_ITEMS
    )
    finalized = finalize_row_items(
        server=server,
        row_kind=row_kind,
        title=row_seed["title"],
        candidates=row_seed.get("candidates") or [],
        profile=profile,
        used_track_ids=used_track_ids,
        used_artist_counts=used_artist_counts,
        enforce_feed_artist_cap=enforce_feed_artist_cap,
        max_items=max_items,
        embedding_lookup=embedding_lookup,
        metadata_enrich_limit=metadata_enrich_limit,
    )
    if finalized is None:
        diagnostics = {
            "builder": f"candidate_snapshot:{row_kind}",
            "builder_ms": allocator_ms,
            "status": "filtered_out",
            "required": is_required_row(server, row_kind),
            "candidate_count": len(row_seed.get("candidates") or []),
            "row_strategy": row_strategy,
            "fallback_reason": fallback_reason,
            "source_pool_counts": source_pool_counts,
        }
        return None, diagnostics

    finalized = _expand_row_bank(
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
    finalized = apply_track_row_runtime_fields(
        server=server,
        finalized=finalized,
        row_seed=row_seed,
    )
    if row_kind == "quiet_picks":
        finalized = apply_quiet_row_runtime_fields(
            server=server,
            finalized=finalized,
            row_seed=row_seed,
        )
    finalized["row_strategy"] = row_strategy
    finalized["fallback_reason"] = fallback_reason
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
    trace: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    used_track_ids: set[str] = set()
    used_artist_counts: Dict[str, int] = {}
    generator_timings: Dict[str, int] = {}
    row_diagnostics: Dict[str, Dict[str, Any]] = {}
    row_seeds: Dict[str, Dict[str, Any]] = {}

    for row_kind in policy_row_kinds():
        row_seed = build_row_seed(
            server=server,
            row_kind=row_kind,
            profile=profile,
            snapshot=candidate_snapshot,
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
    if not precompute_hit and "fallback" in resolved_from:
        metadata_enrich_limit = 0
    elif not precompute_hit:
        metadata_enrich_limit = 2
    embedding_lookup = _prepare_candidate_embeddings(
        server=server,
        row_seeds=list(row_seeds.values()),
    )

    for row_kind in policy_row_kinds():
        row_seed = row_seeds.get(row_kind)
        if not isinstance(row_seed, dict):
            continue
        row, diagnostics = _finalize_row_seed(
            server=server,
            profile=profile,
            row_seed=row_seed,
            used_track_ids=used_track_ids,
            used_artist_counts=used_artist_counts,
            embedding_lookup=embedding_lookup,
            metadata_enrich_limit=metadata_enrich_limit,
        )
        row_diagnostics[row_kind] = diagnostics
        if row is not None:
            rows.append(row)

    existing_row_ids = {
        row.get("id")
        for row in rows
        if isinstance(row, dict)
    }
    for required_row_kind in required_row_kinds(server):
        if required_row_kind in existing_row_ids:
            continue
        if not allow_required_fallback:
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
        fallback_row, fallback_diag = _finalize_row_seed(
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
        existing_row_ids.add(required_row_kind)

    rows.sort(key=lambda row: ROW_KIND_ORDER.get(row.get("kind"), 100))
    row_builder_mode = candidate_snapshot.get("row_mode") or "candidate_snapshot_v42"
    server._trace_put(
        trace,
        "ranking_meta",
        "recommend.row_builder_mode",
        row_builder_mode,
    )
    builder_meta = {
        "row_builder_mode": row_builder_mode,
        "candidate_snapshot_source": candidate_snapshot.get("resolved_from") or "",
        "candidate_pool_counts": dict(candidate_snapshot.get("pool_counts") or {}),
        "candidate_stage_timings_ms": dict(candidate_snapshot.get("stage_timings_ms") or {}),
        "candidate_snapshot_generated_at": float(candidate_snapshot.get("generated_at") or 0.0),
        "candidate_snapshot_build_ms": int(candidate_snapshot.get("build_ms") or 0),
        "candidate_snapshot_albums_count": int(candidate_snapshot.get("albums_count") or 0),
        "candidate_snapshot_hit": bool(precompute_hit),
        "metadata_enrich_limit": (
            metadata_enrich_limit if metadata_enrich_limit is not None else -1
        ),
        "candidate_snapshot": dict(candidate_snapshot),
    }
    return rows, generator_timings, row_diagnostics, builder_meta


def build_rows_v41(
    *,
    server: Any,
    profile: Dict[str, Any],
    precompute_snapshot: Dict[str, Any] | None = None,
    trace: Dict[str, Any] | None = None,
    allow_live_snapshot_build: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
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
            candidate_snapshot = build_home_candidate_snapshot_fallback(
                server=server,
                profile=profile,
            )
            candidate_snapshot["resolved_from"] = "request_fallback_deferred"
    resolved_from = str(candidate_snapshot.get("resolved_from") or "")
    allow_required_fallback = "fallback" in resolved_from or "error" in resolved_from
    try:
        return _build_rows_from_candidate_snapshot(
            server=server,
            profile=profile,
            candidate_snapshot=candidate_snapshot,
            precompute_hit=precompute_hit,
            allow_required_fallback=allow_required_fallback,
            trace=trace,
        )
    except Exception as exc:
        server._trace_put(
            trace,
            "errors",
            "recommend.candidate_snapshot_error",
            str(exc)[:240],
        )
        degraded_snapshot = build_home_candidate_snapshot_fallback(
            server=server,
            profile=profile,
        )
        degraded_snapshot["resolved_from"] = "request_fallback_error"
        return _build_rows_from_candidate_snapshot(
            server=server,
            profile=profile,
            candidate_snapshot=degraded_snapshot,
            precompute_hit=False,
            allow_required_fallback=True,
            trace=trace,
        )
