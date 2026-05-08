from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from typing import Any
from typing import Dict, List, Tuple

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ..domain.features import build_home_profile
from .row_extension_runtime import extend_row_from_snapshot
from .row_seed_builder import (
    build_row_seed,
    build_continue_listening_row,
    refresh_trending_by_genre_row,
)
from .snapshot_support_runtime import build_album_items, build_artist_artifacts
from .snapshot_builder import (
    build_home_candidate_snapshot,
    build_home_candidate_snapshot_fast_fallback,
    snapshot_substrate_mode,
    trim_home_candidate_snapshot,
)
from .policy import should_extend_row
from .precompute import (
    build_home_snapshot,
    get_home_heavy_artifact,
    get_home_heavy_artifact_for_profile,
    get_home_launch_artifact,
    get_home_launch_artifact_for_profile,
    get_home_snapshot,
    get_home_snapshot_for_profile,
    invalidate_home_snapshots,
    runtime_snapshot as precompute_runtime_snapshot,
    store_home_serving_artifacts,
)
from .feature_store import request_store_runtime
from .freshness_runtime import freshen_launch_rows, visible_impression_rows
from .quality import snapshot_quality_reasons as compute_snapshot_quality_reasons
from .admin_runtime import (
    evaluate_experiments as evaluate_experiments_runtime,
    experiments as experiments_runtime,
    interaction_event as interaction_event_runtime,
    model_status as model_status_runtime,
    model_versions as model_versions_runtime,
    recommended_artists as recommended_artists_runtime,
    search_interaction as search_interaction_runtime,
    train_model as train_model_runtime,
)
from .row_runtime import (
    QUALITY_CRITICAL_ROWS,
    build_rows_v41 as build_rows_for_snapshot,
    merge_home_rows,
)
from .row_registry import (
    deferred_row_kinds as registry_deferred_row_kinds,
    row_order_index,
    row_title_template,
)
from .session_runtime import load_feed_session, prune_feed_cache, store_feed_session
from .warmup_runtime import schedule_home_artifact_warmup, schedule_home_warmup

try:
    from ..storage.postgres import (
        activate_model_version,
        list_model_versions,
        list_rollout_events,
        rollback_model_version,
    )
except Exception:
    def list_model_versions(*, model_key: str, limit: int = 20):
        return []

    def activate_model_version(
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return False

    def rollback_model_version(
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ):
        return {"ok": False, "reason": "postgres_unavailable"}

    def list_rollout_events(*, model_key: str = "", limit: int = 50):
        return []


RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS = max(
    1.5,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS",
            "3.5",
        )
    ),
)
RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS = max(
    0.8,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS",
            "1.8",
        )
    ),
)
RECOMMEND_ROW_FINALIZE_BUDGET_SECONDS = max(
    1.5,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_FINALIZE_BUDGET_SECONDS",
            "3.2",
        )
    ),
)
RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS = max(
    RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS + 0.4,
    float(
        os.environ.get(
            "AURALIS_RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS",
            "5.8",
        )
    ),
)
RECOMMEND_RICH_BOOTSTRAP_ON_LAUNCH_MISS = (
    os.environ.get("AURALIS_RECOMMEND_RICH_BOOTSTRAP_ON_LAUNCH_MISS", "1")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)
RECOMMEND_DISABLE_TIMEOUTS = (
    (os.environ.get("AURALIS_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
    or (os.environ.get("AURALIS_RECOMMEND_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
)
RECOMMEND_LIVE_SNAPSHOT_ON_MISS = (
    os.environ.get("AURALIS_RECOMMEND_LIVE_SNAPSHOT_ON_MISS", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)

_FLAGSHIP_DEFERRED_ROW_KINDS: tuple[str, ...] = (
    "quiet_picks",
    "mixed_for_you",
    "trending_by_genre",
    "recommended_albums",
    "recommended_artists",
)
_HEAVY_FLAGSHIP_ROW_KINDS = {"recommended_albums", "recommended_artists"}
_DEFERRED_ROW_KIND_SET = set(registry_deferred_row_kinds())
_FLAGSHIP_REFINE_ROW_CONTEXT = "flagship_refine"
_FLAGSHIP_ROW_STATE_PENDING = "pending"
_FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT = "partial_inflight"
_FLAGSHIP_ROW_STATE_READY = "ready"
_FLAGSHIP_ROW_STATE_UNAVAILABLE = "unavailable"


def _snapshot_quality_is_weak(
    row_diagnostics: Dict[str, Dict[str, Any]] | None,
    *,
    critical_rows: tuple[str, ...] = ("continue_listening", "because_you_played"),
) -> bool:
    return bool(
        compute_snapshot_quality_reasons(
            row_diagnostics,
            critical_rows=critical_rows,
        )
    )


def _should_bootstrap_rich_snapshot(profile: Dict[str, Any]) -> bool:
    recent_depth = max(
        len(list(profile.get("recent_track_ids") or [])),
        len(list(profile.get("recent_track_snapshots") or [])),
        len(list(profile.get("last_played_tracks") or [])),
    )
    long_term_depth = max(
        len(list(profile.get("top_track_ids") or [])),
        len(list(profile.get("top_track_snapshots") or [])),
    )
    artist_depth = len(
        {
            str(value or "").strip().lower()
            for value in [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
                *(profile.get("listened_artists") or []),
            ]
            if str(value or "").strip()
        }
    )
    collaborative_ready = bool(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )
    anchor_depth = len(list(profile.get("anchor_track_snapshots") or []))
    library_depth = max(
        len(list(profile.get("library_track_ids") or [])),
        len(list(profile.get("offline_track_ids") or [])),
    )
    query_depth = max(
        len(list(profile.get("recent_queries") or [])),
        len(list(profile.get("taste_queries") or [])),
    )
    signal_score = 0
    if recent_depth >= 2:
        signal_score += 2
    elif recent_depth >= 1:
        signal_score += 1
    if long_term_depth >= 4:
        signal_score += 2
    elif long_term_depth >= 2:
        signal_score += 1
    if artist_depth >= 3:
        signal_score += 2
    elif artist_depth >= 2:
        signal_score += 1
    if collaborative_ready:
        signal_score += 2
    if anchor_depth >= 1:
        signal_score += 1
    if library_depth >= 8:
        signal_score += 1
    if query_depth >= 2:
        signal_score += 1
    return signal_score >= 3


def _candidate_snapshot_from_precompute(
    precompute_snapshot: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    if not isinstance(precompute_snapshot, dict):
        return None
    candidate_snapshot = precompute_snapshot.get("candidate_snapshot")
    if isinstance(candidate_snapshot, dict) and candidate_snapshot:
        return dict(candidate_snapshot)
    return None


def _precompute_snapshot_supports_rich_rows(
    precompute_snapshot: Dict[str, Any] | None,
) -> bool:
    candidate_snapshot = _candidate_snapshot_from_precompute(precompute_snapshot)
    if not isinstance(candidate_snapshot, dict) or not candidate_snapshot:
        return False
    return snapshot_substrate_mode(candidate_snapshot) == "rich_personalized"


def _launch_artifact_supports_rich_rows(
    launch_artifact: Dict[str, Any] | None,
) -> bool:
    if not isinstance(launch_artifact, dict):
        return False
    diagnostics = dict(launch_artifact.get("diagnostics") or {})
    row_builder_mode = str(diagnostics.get("row_builder_mode") or "").strip().lower()
    if "rich" in row_builder_mode:
        return True
    candidate_snapshot = launch_artifact.get("candidate_snapshot")
    if isinstance(candidate_snapshot, dict) and candidate_snapshot:
        return snapshot_substrate_mode(candidate_snapshot) == "rich_personalized"
    row_status = dict(launch_artifact.get("row_status") or {})
    deferred_rich_rows = (
        "todays_pick",
        "trending_by_genre",
        "trending_for_you",
        "quiet_picks",
        "deep_cuts",
    )
    return not any(
        str(row_status.get(row_kind) or "").strip().lower() == "deferred_substrate"
        for row_kind in deferred_rich_rows
    )


def _launch_artifact_hard_missing_statuses() -> set[str]:
    return {
        "empty",
        "missing_no_fallback",
        "fallback_unavailable",
        "seed_pool_empty",
        "post_filter_empty",
        "finalize_filtered_out",
    }


def _launch_artifact_blocking_gap_kinds(
    launch_artifact: Dict[str, Any] | None,
) -> List[str]:
    if not isinstance(launch_artifact, dict):
        return ["artifact_missing"]
    row_status = {
        str(row_kind or ""): str(status or "").strip().lower()
        for row_kind, status in dict(launch_artifact.get("row_status") or {}).items()
    }
    hard_missing = _launch_artifact_hard_missing_statuses()
    blocking_rows = ("todays_pick",)
    return [
        row_kind
        for row_kind in blocking_rows
        if row_status.get(row_kind) in hard_missing
    ]


def _launch_artifact_refresh_gap_kinds(
    launch_artifact: Dict[str, Any] | None,
) -> List[str]:
    if not isinstance(launch_artifact, dict):
        return []
    row_status = {
        str(row_kind or ""): str(status or "").strip().lower()
        for row_kind, status in dict(launch_artifact.get("row_status") or {}).items()
    }
    hard_missing = _launch_artifact_hard_missing_statuses()
    row_kinds = dict.fromkeys(
        (
            "trending_for_you",
            *_FLAGSHIP_DEFERRED_ROW_KINDS,
        )
    )
    return [
        row_kind
        for row_kind in row_kinds
        if row_status.get(row_kind) in hard_missing
    ]


def _launch_artifact_is_thin_core(
    launch_artifact: Dict[str, Any] | None,
) -> bool:
    if not isinstance(launch_artifact, dict):
        return False
    diagnostics = dict(launch_artifact.get("diagnostics") or {})
    row_builder_mode = str(diagnostics.get("row_builder_mode") or "").strip().lower()
    return "thin_core" in row_builder_mode or bool(diagnostics.get("launch_tier_only"))


def _flagship_row_shell(row_kind: str) -> Dict[str, Any]:
    item_type = "track"
    row_style = ""
    loading_label = "Preparing"
    loading_message = "Finding more picks for this lane."
    if row_kind == "mixed_for_you":
        item_type = "mix"
        row_style = "mix_cards"
        loading_label = "Blending"
        loading_message = "Building a mix from the corners of your recent taste."
    elif row_kind == "trending_by_genre":
        row_style = "genre_tabs"
        loading_label = "Scanning"
        loading_message = "Looking for a genre pocket that fits what you have been playing."
    elif row_kind == "recommended_albums":
        item_type = "album"
        loading_label = "Curating"
        loading_message = "Matching full albums worth settling into."
    elif row_kind == "recommended_artists":
        item_type = "artist"
        loading_label = "Connecting"
        loading_message = "Finding artists that line up with your current taste."
    elif row_kind == "quiet_picks":
        loading_label = "Settling"
        loading_message = "Lining up a calmer pocket with room to stretch out."
    return {
        "id": row_kind,
        "kind": row_kind,
        "title": row_title_template(row_kind),
        "item_type": item_type,
        "row_style": row_style,
        "items": [],
        "next_offset": 0,
        "has_more": False,
        "meta": {
            "loading_state": "pending",
            "deferred_flagship": True,
            "loading_label": loading_label,
            "loading_message": loading_message,
            "row_state": _FLAGSHIP_ROW_STATE_PENDING,
            "row_version": 0,
            "refinement_active": True,
        },
    }


def _flagship_row_item_key(item_type: str, item: Dict[str, Any]) -> str:
    if item_type == "album":
        album_id = str(item.get("id") or "").strip()
        if album_id:
            return f"album:{album_id}"
        title = str(item.get("title") or "").strip().lower()
        artist = str(item.get("artist") or "").strip().lower()
        return f"album:{title}|{artist}"
    if item_type == "artist":
        artist_id = str(item.get("id") or "").strip()
        if artist_id:
            return f"artist:{artist_id}"
        name = str(item.get("name") or "").strip().lower()
        return f"artist:{name}"
    if item_type == "mix":
        mix_id = str(item.get("id") or "").strip()
        if mix_id:
            return f"mix:{mix_id}"
        title = str(item.get("title") or "").strip().lower()
        return f"mix:{title}"
    track_id = str(item.get("id") or item.get("videoId") or item.get("video_id") or "").strip()
    if track_id:
        return f"track:{track_id}"
    title = str(item.get("title") or "").strip().lower()
    artist = str(item.get("artist") or item.get("author") or item.get("channel") or "").strip().lower()
    return f"track:{title}|{artist}"


def _flagship_row_signature(row: Dict[str, Any] | None) -> tuple[str, tuple[str, ...]]:
    if not isinstance(row, dict):
        return ("", ())
    item_type = str(row.get("item_type") or "").strip().lower()
    item_keys = tuple(
        _flagship_row_item_key(item_type, dict(item))
        for item in list(row.get("items") or [])
        if isinstance(item, dict)
    )
    return (item_type, item_keys)


def _flagship_row_progress_signature(
    row: Dict[str, Any] | None,
) -> tuple[Any, ...]:
    if not isinstance(row, dict):
        return tuple()
    row_kind = str(row.get("kind") or row.get("id") or "").strip()
    meta = dict(row.get("meta") or {})
    items = list(row.get("items") or [])
    if row_kind == "trending_by_genre":
        return (
            int(meta.get("tab_count") or 0),
            int(meta.get("pending_tab_count") or 0),
            int(meta.get("total_page_count") or 0),
            str(meta.get("active_tab_id") or "").strip(),
            bool(meta.get("refinement_exhausted")),
        )
    if row_kind == "mixed_for_you":
        return (
            len(items),
            int(meta.get("pending_blueprint_count") or 0),
            int(meta.get("partial_mix_count") or 0),
            bool(meta.get("refinement_exhausted")),
        )
    if row_kind == "recommended_albums":
        return (
            len(items),
            int(meta.get("ready_count") or 0),
            int(meta.get("cached_candidate_count") or 0),
        )
    if row_kind == "recommended_artists":
        return (
            len(items),
            int(meta.get("neighbor_seed_count") or 0),
            int(meta.get("peer_seed_count") or 0),
        )
    return (len(items),)


def _flagship_row_refine_after_ms(
    row_kind: str,
    row: Dict[str, Any] | None,
) -> int:
    if not isinstance(row, dict):
        return 1200
    meta = dict(row.get("meta") or {})
    if row_kind == "mixed_for_you":
        pending_blueprints = int(meta.get("pending_blueprint_count") or 0)
        return 650 if pending_blueprints > 0 else 900
    if row_kind == "trending_by_genre":
        if meta.get("refinement_exhausted") == True:
            return 1100
        pending_tabs = int(meta.get("pending_tab_count") or 0)
        total_pages = int(meta.get("total_page_count") or 0)
        return 700 if pending_tabs > 0 or total_pages < 2 else 950
    if row_kind == "quiet_picks":
        return 700
    if row_kind in _HEAVY_FLAGSHIP_ROW_KINDS:
        return 950
    return 850


def _flagship_row_state(
    row: Dict[str, Any] | None,
) -> str:
    if not isinstance(row, dict):
        return _FLAGSHIP_ROW_STATE_UNAVAILABLE
    meta = dict(row.get("meta") or {})
    row_state = str(meta.get("row_state") or "").strip().lower()
    if row_state:
        return row_state
    loading_state = str(meta.get("loading_state") or "").strip().lower()
    partial_ready = meta.get("partial_ready") == True
    refinement_active = meta.get("refinement_active") == True
    items = list(row.get("items") or [])
    if loading_state == "pending" and not items:
        return _FLAGSHIP_ROW_STATE_PENDING
    if partial_ready and refinement_active:
        return _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT
    if items:
        return _FLAGSHIP_ROW_STATE_READY
    return _FLAGSHIP_ROW_STATE_UNAVAILABLE


def _stamp_flagship_row_meta(
    row: Dict[str, Any],
    *,
    previous_row: Dict[str, Any] | None = None,
    refinement_active: bool | None = None,
) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return row
    row_kind = str(row.get("kind") or row.get("id") or "").strip()
    if row_kind not in _FLAGSHIP_DEFERRED_ROW_KINDS:
        return dict(row)
    updated = dict(row)
    meta = dict(updated.get("meta") or {})
    previous_meta = dict((previous_row or {}).get("meta") or {})
    items = list(updated.get("items") or [])
    loading_state = str(meta.get("loading_state") or "").strip().lower()
    pending = loading_state == "pending" and not items
    partial_ready = meta.get("partial_ready") == True
    existing_refinement_active = meta.get("refinement_active")
    if refinement_active is None:
        if pending:
            refinement_active_value = (
                True
                if existing_refinement_active is None
                else bool(existing_refinement_active)
            )
        elif partial_ready:
            if existing_refinement_active is not None:
                refinement_active_value = bool(existing_refinement_active)
            elif previous_row is None:
                refinement_active_value = True
            else:
                refinement_active_value = bool(
                    previous_meta.get("refinement_active") == True
                )
        else:
            refinement_active_value = False
    else:
        refinement_active_value = bool(refinement_active)
    if pending:
        row_state = _FLAGSHIP_ROW_STATE_PENDING
    elif partial_ready and refinement_active_value:
        row_state = _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT
    elif items:
        row_state = _FLAGSHIP_ROW_STATE_READY
    else:
        row_state = _FLAGSHIP_ROW_STATE_UNAVAILABLE
    previous_version = int(
        previous_meta.get("row_version")
        or meta.get("row_version")
        or (0 if pending else 1)
    )
    changed = False
    if isinstance(previous_row, dict):
        changed = (
            _flagship_row_signature(previous_row) != _flagship_row_signature(updated)
            or _flagship_row_progress_signature(previous_row)
            != _flagship_row_progress_signature(updated)
            or bool(previous_meta.get("partial_ready") == True) != partial_ready
            or bool(previous_meta.get("refinement_active") == True) != refinement_active_value
            or str(previous_meta.get("row_state") or "").strip().lower() != row_state
        )
    if isinstance(previous_row, dict):
        row_version = previous_version + 1 if changed else previous_version
    else:
        row_version = max(previous_version, 0 if pending else 1)
    if row_version <= 0 and items:
        row_version = 1
    meta["row_state"] = row_state
    meta["row_version"] = row_version
    meta["refinement_active"] = refinement_active_value
    meta["refine_after_ms"] = _flagship_row_refine_after_ms(row_kind, updated)
    updated["meta"] = meta
    return updated


def _flagship_row_materially_improves(
    current_row: Dict[str, Any] | None,
    candidate_row: Dict[str, Any] | None,
) -> bool:
    if not isinstance(candidate_row, dict):
        return False
    if not isinstance(current_row, dict):
        return bool(candidate_row.get("items"))
    current_rank = _flagship_row_quality_rank(current_row)
    candidate_rank = _flagship_row_quality_rank(candidate_row)
    if candidate_rank > current_rank:
        return True
    if candidate_rank < current_rank:
        return False
    return (
        _flagship_row_signature(current_row) != _flagship_row_signature(candidate_row)
        or _flagship_row_progress_signature(current_row)
        != _flagship_row_progress_signature(candidate_row)
    )


def _flagship_row_should_continue_refinement(
    row_kind: str,
    current_row: Dict[str, Any] | None,
    candidate_row: Dict[str, Any] | None,
) -> bool:
    if not isinstance(candidate_row, dict):
        return False
    if not _flagship_row_needs_live_refinement(row_kind, candidate_row):
        return False
    return _flagship_row_materially_improves(current_row, candidate_row)


def _pending_flagship_row_kinds(
    *,
    profile: Dict[str, Any],
    rows: List[Dict[str, Any]],
    diagnostics: Dict[str, Any],
) -> List[str]:
    if not _should_bootstrap_rich_snapshot(profile):
        return []
    existing_kinds = {
        str((row or {}).get("kind") or (row or {}).get("id") or "").strip()
        for row in list(rows or [])
        if isinstance(row, dict)
    }
    row_status = {
        str(row_kind or "").strip(): str(
            ((status or {}).get("status") if isinstance(status, dict) else status) or ""
        )
        .strip()
        .lower()
        for row_kind, status in dict(diagnostics.get("row_status") or {}).items()
    }
    pending: List[str] = []
    for row_kind in _FLAGSHIP_DEFERRED_ROW_KINDS:
        if row_kind in existing_kinds:
            continue
        if (
            row_kind in _HEAVY_FLAGSHIP_ROW_KINDS
            and bool(diagnostics.get("heavy_rows_pending"))
        ):
            pending.append(row_kind)
            continue
        if row_status.get(row_kind) == "emitted":
            continue
        pending.append(row_kind)
    return pending


def _partial_flagship_row_kinds(
    rows: List[Dict[str, Any]],
) -> List[str]:
    partial: List[str] = []
    seen: set[str] = set()
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        row_kind = str((row.get("kind") or row.get("id") or "")).strip()
        if row_kind not in _FLAGSHIP_DEFERRED_ROW_KINDS or row_kind in seen:
            continue
        meta = dict(row.get("meta") or {})
        if (
            meta.get("refinement_active") == True
            or str(meta.get("row_state") or "").strip().lower()
            == _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT
        ):
            partial.append(row_kind)
            seen.add(row_kind)
    return partial


def _row_by_kind(
    rows: List[Dict[str, Any]] | None,
    row_kind: str,
) -> Dict[str, Any] | None:
    normalized_kind = str(row_kind or "").strip()
    if not normalized_kind:
        return None
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        candidate_kind = str(row.get("kind") or row.get("id") or "").strip()
        if candidate_kind == normalized_kind:
            return dict(row)
    return None


def _flagship_row_quality_rank(row: Dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(row, dict):
        return (-1, -1)
    items = list(row.get("items") or [])
    row_state = _flagship_row_state(row)
    if row_state == _FLAGSHIP_ROW_STATE_PENDING or not items:
        readiness = 0
    elif row_state == _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT:
        readiness = 1
    else:
        readiness = 2
    return (readiness, len(items))


def _flagship_row_needs_live_refinement(
    row_kind: str,
    row: Dict[str, Any] | None,
) -> bool:
    if not isinstance(row, dict):
        return True
    row_state = _flagship_row_state(row)
    items = list(row.get("items") or [])
    meta = dict(row.get("meta") or {})
    if row_state in {_FLAGSHIP_ROW_STATE_PENDING, _FLAGSHIP_ROW_STATE_UNAVAILABLE}:
        return True
    if row_kind == "quiet_picks":
        return row_state == _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT or len(items) < 8
    if row_kind == "mixed_for_you":
        if bool(meta.get("refinement_exhausted")) and items:
            return False
        return (
            row_state == _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT
            or len(items) < 3
            or int(meta.get("pending_blueprint_count") or 0) > 0
        )
    if row_kind == "trending_by_genre":
        tabs = [
            dict(tab)
            for tab in list(meta.get("tabs") or [])
            if isinstance(tab, dict)
        ]
        refinement_exhausted = bool(meta.get("refinement_exhausted"))
        if refinement_exhausted:
            return row_state in {
                _FLAGSHIP_ROW_STATE_PENDING,
                _FLAGSHIP_ROW_STATE_UNAVAILABLE,
            } and not items
        if row_state == _FLAGSHIP_ROW_STATE_PARTIAL_INFLIGHT:
            return True
        if int(meta.get("pending_tab_count") or 0) > 0:
            return True
        total_pages = sum(max(int(tab.get("page_count") or 0), 1) for tab in tabs)
        return total_pages < 2
    if row_kind in _HEAVY_FLAGSHIP_ROW_KINDS:
        return row_state != _FLAGSHIP_ROW_STATE_READY or not items
    return row_state != _FLAGSHIP_ROW_STATE_READY


def _clone_flagship_row_identity(
    target_row: Dict[str, Any],
    candidate_row: Dict[str, Any],
    *,
    row_kind: str,
) -> Dict[str, Any]:
    updated = dict(candidate_row or {})
    updated["id"] = (
        str(target_row.get("id") or "").strip()
        or str(updated.get("id") or "").strip()
        or row_kind
    )
    updated["kind"] = (
        str(target_row.get("kind") or "").strip()
        or str(updated.get("kind") or "").strip()
        or row_kind
    )
    updated["title"] = (
        str(target_row.get("title") or "").strip()
        or str(updated.get("title") or "").strip()
        or row_title_template(row_kind)
    )
    if target_row.get("row_style") and not updated.get("row_style"):
        updated["row_style"] = target_row.get("row_style")
    return updated


def _rows_with_flagship_shells(
    *,
    profile: Dict[str, Any],
    rows: List[Dict[str, Any]],
    diagnostics: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows_with_shells = [
        _stamp_flagship_row_meta(dict(row))
        if isinstance(row, dict)
        else row
        for row in list(rows or [])
    ]
    next_diagnostics = dict(diagnostics or {})
    pending_flagship_kinds = _pending_flagship_row_kinds(
        profile=profile,
        rows=rows_with_shells,
        diagnostics=next_diagnostics,
    )
    if pending_flagship_kinds:
        rows_with_shells.extend(
            _flagship_row_shell(row_kind) for row_kind in pending_flagship_kinds
        )
        rows_with_shells.sort(
            key=lambda row: row_order_index(
                str((row or {}).get("kind") or (row or {}).get("id") or "")
            )
        )
    partial_flagship_kinds = _partial_flagship_row_kinds(rows_with_shells)
    next_diagnostics["flagship_deferred_row_kinds"] = list(pending_flagship_kinds)
    next_diagnostics["flagship_rows_pending"] = bool(pending_flagship_kinds)
    next_diagnostics["flagship_partial_row_kinds"] = list(partial_flagship_kinds)
    next_diagnostics["flagship_rows_partial"] = bool(partial_flagship_kinds)
    next_diagnostics["heavy_rows_pending"] = bool(
        next_diagnostics.get("heavy_rows_pending")
    ) or any(row_kind in _HEAVY_FLAGSHIP_ROW_KINDS for row_kind in pending_flagship_kinds)
    next_diagnostics["heavy_rows_partial"] = any(
        row_kind in _HEAVY_FLAGSHIP_ROW_KINDS for row_kind in partial_flagship_kinds
    )
    deferred_row_kinds = {
        str(value or "").strip()
        for value in list(next_diagnostics.get("deferred_row_kinds") or [])
        if str(value or "").strip()
    }
    deferred_row_kinds.update(
        row_kind for row_kind in pending_flagship_kinds if row_kind in _DEFERRED_ROW_KIND_SET
    )
    next_diagnostics["deferred_row_kinds"] = [
        row_kind for row_kind in registry_deferred_row_kinds() if row_kind in deferred_row_kinds
    ]
    next_diagnostics["deferred_rows_pending"] = bool(
        next_diagnostics["deferred_row_kinds"]
    )
    next_diagnostics["row_order"] = [
        str((row or {}).get("id") or (row or {}).get("kind") or "")
        for row in rows_with_shells
        if isinstance(row, dict)
    ]
    next_diagnostics["row_item_counts"] = {
        str((row or {}).get("id") or (row or {}).get("kind") or ""): len(
            (row or {}).get("items") or []
        )
        for row in rows_with_shells
        if isinstance(row, dict)
    }
    return rows_with_shells, next_diagnostics


def _build_rich_bootstrap_snapshot(
    *,
    server: Any,
    user_scope_id: str,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    candidate_snapshot = trim_home_candidate_snapshot(
        server,
        build_home_candidate_snapshot(
            server=server,
            profile=profile,
        ),
    )
    now = time.time()
    return {
        "user_scope_id": user_scope_id,
        "generated_at": now,
        "expires_at": now + 900,
        "candidate_snapshot": candidate_snapshot,
        "resolved_from": "request_bootstrap_live",
        "profile_summary": {
            "profile_key": profile.get("profile_key") or "",
        },
    }


def _recommendation_runtime_health(server: Any) -> Dict[str, Any]:
    runtime_snapshot_fn = getattr(server, "_recommendation_runtime_snapshot", None)
    runtime = {}
    if callable(runtime_snapshot_fn):
        try:
            runtime = dict(runtime_snapshot_fn() or {})
        except Exception:
            runtime = {}
    external_worker_expected = bool(
        runtime.get("external_worker_expected")
        if runtime
        else getattr(server, "RECOMMENDATION_EXTERNAL_WORKER", False)
    )
    worker_status = str(runtime.get("worker_status") or "").strip().lower()
    worker_mode = str(runtime.get("worker_mode") or "").strip().lower()
    worker_last_heartbeat_at = float(runtime.get("worker_last_heartbeat_at") or 0.0)
    heartbeat_age_seconds = (
        max(0.0, time.time() - worker_last_heartbeat_at)
        if worker_last_heartbeat_at > 0
        else 0.0
    )
    max_heartbeat_age_seconds = max(
        60.0,
        float(getattr(server, "RECOMMENDATION_SYNC_INTERVAL_SECONDS", 300)) * 2.0,
    )
    worker_healthy = (
        not external_worker_expected
        or (
            worker_status == "running"
            and worker_last_heartbeat_at > 0
            and heartbeat_age_seconds <= max_heartbeat_age_seconds
        )
    )
    return {
        "external_worker_expected": external_worker_expected,
        "worker_mode": worker_mode,
        "worker_status": worker_status,
        "worker_last_heartbeat_at": worker_last_heartbeat_at,
        "worker_heartbeat_age_seconds": round(heartbeat_age_seconds, 3),
        "worker_max_heartbeat_age_seconds": round(max_heartbeat_age_seconds, 3),
        "worker_healthy": worker_healthy,
        "external_worker_unhealthy": external_worker_expected and not worker_healthy,
        "scheduler_enabled": bool(runtime.get("scheduler_enabled") or getattr(server, "RECOMMENDATION_ENABLE_SCHEDULER", False)),
        "scheduler_last_error": str(runtime.get("last_scheduler_error") or "").strip(),
        "nearline_last_cycle_status": str(runtime.get("nearline_last_cycle_status") or "").strip(),
        "nearline_last_cycle_at": float(runtime.get("nearline_last_cycle_at") or 0.0),
    }


def _schedule_runtime_bootstrap(server: Any) -> bool:
    bootstrap_fn = getattr(server, "_start_recommendation_bootstrap_thread", None)
    if not callable(bootstrap_fn):
        return False
    try:
        return bool(bootstrap_fn())
    except Exception:
        return False


class RecommendationService:
    def __init__(self, server: Any) -> None:
        self._server = server

    def _resolve_launch_artifact_policy(
        self,
        req,
        *,
        launch_artifact: Dict[str, Any],
        rich_launch_required: bool = False,
    ) -> Dict[str, Any]:
        resolved_from = str(launch_artifact.get("resolved_from") or "")
        stale_artifact = bool(launch_artifact.get("stale"))
        acceptable_artifact = resolved_from.startswith("acceptable")
        prefer_fresh_rows = bool(getattr(req, "prefer_fresh_rows", False))
        thin_launch_artifact = bool(
            rich_launch_required and not _launch_artifact_supports_rich_rows(launch_artifact)
        )
        should_schedule_refresh = bool(
            prefer_fresh_rows or stale_artifact or acceptable_artifact or thin_launch_artifact
        )
        # Manual refresh should not replace a solid visible artifact with a weaker
        # request-time thin rebuild. We keep serving the current artifact, but
        # schedule a fresher artifact in the background and apply row freshness.
        should_bypass_artifact = False
        artifact_bypass_reason = ""
        return {
            "resolved_from": resolved_from,
            "stale_artifact": stale_artifact,
            "acceptable_artifact": acceptable_artifact,
            "thin_launch_artifact": thin_launch_artifact,
            "prefer_fresh_rows": prefer_fresh_rows,
            "should_schedule_refresh": should_schedule_refresh,
            "should_bypass_artifact": should_bypass_artifact,
            "artifact_bypass_reason": artifact_bypass_reason,
        }

    def _summarize_row_diagnostics(
        self,
        row_diagnostics: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        server = self._server
        row_source_counts = {}
        row_selected_source_counts = {}
        row_model_versions = {}
        for row_id, diagnostics in row_diagnostics.items():
            if not isinstance(diagnostics, dict):
                continue
            source_counts = diagnostics.get("source_counts")
            if isinstance(source_counts, dict) and source_counts:
                row_source_counts[row_id] = dict(source_counts)
            selected_source_counts = diagnostics.get("selected_source_counts")
            if isinstance(selected_source_counts, dict) and selected_source_counts:
                row_selected_source_counts[row_id] = dict(selected_source_counts)
            ranking_model_version = server._recommendation_trim_text(
                diagnostics.get("ranking_model_version")
            )
            if ranking_model_version:
                row_model_versions[row_id] = ranking_model_version
        return {
            "row_source_counts": row_source_counts,
            "row_selected_source_counts": row_selected_source_counts,
            "row_model_versions": row_model_versions,
        }

    def _load_precompute_snapshot(
        self,
        req,
        *,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        server = self._server
        profile_key = profile.get("profile_key") or ""
        precompute_snapshot = None
        precompute_hit = False
        precompute_cache_hit = False
        if bool(req.force_refresh):
            try:
                precompute_snapshot = build_home_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    force=True,
                    profile=profile,
                )
                if isinstance(precompute_snapshot, dict) and precompute_snapshot:
                    precompute_snapshot = dict(precompute_snapshot)
                    precompute_snapshot.setdefault(
                        "resolved_from",
                        "request_force_snapshot_build",
                    )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
            except Exception:
                precompute_snapshot = None
        else:
            precompute_snapshot = get_home_snapshot(
                user_scope_id=profile.get("user_scope_id") or "guest",
                server=server,
            )
            if isinstance(precompute_snapshot, dict):
                precompute_hit = bool(
                    precompute_snapshot.get("candidate_snapshot")
                    or precompute_snapshot.get("rows")
                )
                precompute_cache_hit = precompute_hit
            if not precompute_hit:
                precompute_snapshot = get_home_snapshot_for_profile(
                    profile_key=profile_key,
                    server=server,
                )
                if isinstance(precompute_snapshot, dict):
                    precompute_hit = bool(
                        (precompute_snapshot or {}).get("candidate_snapshot")
                    )
                    precompute_cache_hit = precompute_hit
        return {
            "precompute_snapshot": precompute_snapshot,
            "precompute_hit": precompute_hit,
            "precompute_cache_hit": precompute_cache_hit,
        }

    def _apply_precompute_snapshot_policy(
        self,
        *,
        req,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        worker_runtime: Dict[str, Any] | None = None,
        precompute_snapshot: Dict[str, Any] | None = None,
        precompute_hit: bool = False,
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = dict(worker_runtime or {})
        rich_launch_required = _should_bootstrap_rich_snapshot(profile)
        force_rich_rows = False
        runtime_bootstrap_scheduled = False
        rich_bootstrap_deferred = False
        rich_bootstrap_attempted = False
        rich_bootstrap_ms = 0
        allow_request_bootstrap = not bool(
            getattr(req, "prepare_next_session", False)
        ) and bool(getattr(req, "force_refresh", False))

        if (
            rich_launch_required
            and precompute_hit
            and not _precompute_snapshot_supports_rich_rows(precompute_snapshot)
        ):
            force_rich_rows = True
            if bool(worker_runtime.get("external_worker_unhealthy")):
                rich_bootstrap_deferred = True
                runtime_bootstrap_scheduled = _schedule_runtime_bootstrap(server)
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.precompute_thin_deferred_external_worker_unhealthy",
                    True,
                )
            elif bool(getattr(req, "force_refresh", False)):
                precompute_snapshot = None
                precompute_hit = False
                force_rich_rows = True
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.precompute_thin_rejected",
                    True,
                )
            else:
                rich_bootstrap_deferred = True
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.precompute_thin_deferred_launch_policy",
                    True,
                )

        if (
            not precompute_hit
            and rich_launch_required
            and bool(worker_runtime.get("external_worker_unhealthy"))
        ):
            rich_bootstrap_deferred = True
            runtime_bootstrap_scheduled = (
                runtime_bootstrap_scheduled or _schedule_runtime_bootstrap(server)
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.rich_bootstrap_deferred_external_worker_unhealthy",
                True,
            )
        elif (
            not precompute_hit
            and rich_launch_required
            and allow_request_bootstrap
            and not rich_bootstrap_deferred
        ):
            rich_bootstrap_attempted = True
            force_rich_rows = True
            try:
                rich_bootstrap_started_at = time.perf_counter()
                precompute_snapshot = _build_rich_bootstrap_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    profile=profile,
                )
                rich_bootstrap_ms = int(
                    (time.perf_counter() - rich_bootstrap_started_at) * 1000
                )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
                server._trace_stage(
                    trace,
                    "recommend.rich_bootstrap",
                    rich_bootstrap_started_at,
                )
                server._trace_put(
                    trace,
                    "timings_ms",
                    "recommend.rich_bootstrap_ms",
                    rich_bootstrap_ms,
                )
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.rich_bootstrap_snapshot",
                    bool(precompute_hit),
                )
            except Exception as exc:
                server._trace_put(
                    trace,
                    "errors",
                    "recommend.rich_bootstrap_error",
                    str(exc)[:240],
                )
                precompute_snapshot = None
        elif not precompute_hit and rich_launch_required:
            rich_bootstrap_deferred = True
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.rich_bootstrap_deferred_launch_policy",
                True,
            )

        return {
            "precompute_snapshot": precompute_snapshot,
            "precompute_hit": precompute_hit,
            "rich_launch_required": rich_launch_required,
            "force_rich_rows": force_rich_rows,
            "runtime_bootstrap_scheduled": runtime_bootstrap_scheduled,
            "rich_bootstrap_deferred": rich_bootstrap_deferred,
            "rich_bootstrap_attempted": rich_bootstrap_attempted,
            "rich_bootstrap_ms": rich_bootstrap_ms,
        }

    def _build_nearline_precompute_diagnostics(
        self,
        *,
        precompute_cache_hit: bool,
        precompute_hit: bool,
        precompute_stale: bool,
        precompute_snapshot: Dict[str, Any] | None,
        row_builder_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "hit": precompute_cache_hit,
            "snapshot_ready": precompute_hit,
            "stale": precompute_stale,
            "resolved_from": (precompute_snapshot or {}).get("resolved_from") or "",
            "snapshot_hit": bool(row_builder_meta.get("candidate_snapshot_hit")),
            "rows_seeded": len((precompute_snapshot or {}).get("rows") or {}),
            "candidate_pools_seeded": len(
                (row_builder_meta.get("candidate_pool_counts") or {})
            ),
            "snapshot_generated_at": float((precompute_snapshot or {}).get("generated_at") or 0.0),
            "snapshot_expires_at": float((precompute_snapshot or {}).get("expires_at") or 0.0),
            "candidate_snapshot_generated_at": float(
                row_builder_meta.get("candidate_snapshot_generated_at") or 0.0
            ),
            "candidate_snapshot_build_ms": int(
                row_builder_meta.get("candidate_snapshot_build_ms") or 0
            ),
            "candidate_snapshot_albums_count": int(
                row_builder_meta.get("candidate_snapshot_albums_count") or 0
            ),
            "runtime": precompute_runtime_snapshot(),
        }

    def _build_request_session_diagnostics(
        self,
        *,
        profile: Dict[str, Any],
        profile_ms: int,
        profile_key: str,
        worker_runtime: Dict[str, Any],
        precompute_state: Dict[str, Any],
        row_ms: int,
        generator_timings: Dict[str, int],
        row_diagnostics: Dict[str, Dict[str, Any]],
        row_builder_meta: Dict[str, Any],
        rows: List[Dict[str, Any]],
        started_at: float,
    ) -> Dict[str, Any]:
        server = self._server
        summary = self._summarize_row_diagnostics(row_diagnostics)
        row_item_counts = {
            row["id"]: len(row.get("items") or [])
            for row in rows
        }
        snapshot_quality_reasons = compute_snapshot_quality_reasons(
            row_diagnostics,
            critical_rows=QUALITY_CRITICAL_ROWS,
        )
        snapshot_cacheable = not bool(snapshot_quality_reasons)
        deferred_row_kinds = list(row_builder_meta.get("deferred_row_kinds") or [])
        home_ranking_model_key = "home_global_ranker_v4"
        home_ranking_model_version = server._ranking_model_version(home_ranking_model_key)
        precompute_snapshot = precompute_state["precompute_snapshot"]

        total_build_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "cache_hit": False,
            "ranking_backend": "embedding_profile",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "home_ranking_model_key": home_ranking_model_key,
            "home_ranking_model_version": home_ranking_model_version,
            "collaborative_model_ready": bool((profile.get("collaborative") or {}).get("model_ready")),
            "collaborative_model_type": (profile.get("collaborative") or {}).get("model_type") or "",
            "collaborative_model_id": (profile.get("collaborative") or {}).get("model_id") or "",
            "experiment_key": server.RECOMMENDATION_EXPERIMENT_KEY,
            "experiment_variant": profile.get("experiment_variant") or "control",
            "active_promotion_variant": (server._recommendation_active_promotion() or {}).get("promoted_variant") or "",
            "external_worker_expected": server.RECOMMENDATION_EXTERNAL_WORKER,
            "sync_dsn_configured": bool(server.RECOMMENDATION_SYNC_DATABASE_DSN),
            "scheduler_enabled": server.RECOMMENDATION_ENABLE_SCHEDULER,
            "profile_build_ms": profile_ms,
            "artifact_load_ms": 0,
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": profile.get("catalog_feature_version") or "",
            "taste_profile_version": profile.get("taste_profile_version") or "",
            "scene_graph_version": profile.get("scene_graph_version") or "",
            "feature_source": profile.get("feature_source") or "",
            "negative_feedback_applied": bool(profile.get("negative_feedback_applied")),
            "rich_launch_required": bool(precompute_state["rich_launch_required"]),
            "rich_bootstrap_attempted": bool(precompute_state["rich_bootstrap_attempted"]),
            "rich_bootstrap_deferred": bool(precompute_state["rich_bootstrap_deferred"]),
            "rich_bootstrap_snapshot_ms": int(precompute_state["rich_bootstrap_ms"] or 0),
            "precompute_cache_hit": bool(precompute_state["precompute_cache_hit"]),
            "precompute_resolution_ms": int(precompute_state["precompute_resolution_ms"] or 0),
            "force_rich_rows": bool(precompute_state["force_rich_rows"]),
            "launch_tier_only": bool(row_builder_meta.get("launch_tier_only")),
            "runtime_bootstrap_scheduled": bool(precompute_state["runtime_bootstrap_scheduled"]),
            "worker_runtime": worker_runtime,
            "precompute_substrate_mode": snapshot_substrate_mode(
                _candidate_snapshot_from_precompute(precompute_snapshot) or {}
            ),
            "snapshot_cacheable": snapshot_cacheable,
            "snapshot_quality_reasons": list(snapshot_quality_reasons),
            "row_assembly_ms": row_ms,
            "generator_timings_ms": generator_timings,
            "row_status": row_diagnostics,
            "row_order": [row["id"] for row in rows],
            "row_item_counts": row_item_counts,
            "row_source_counts": summary["row_source_counts"],
            "row_selected_source_counts": summary["row_selected_source_counts"],
            "row_model_versions": summary["row_model_versions"],
            "profile_key": profile_key,
            "row_builder_mode": row_builder_meta.get("row_builder_mode") or "candidate_snapshot_v42",
            "candidate_snapshot_source": row_builder_meta.get("candidate_snapshot_source") or "",
            "deferred_row_kinds": deferred_row_kinds,
            "deferred_rows_pending": bool(deferred_row_kinds),
            "candidate_pool_counts": dict(
                row_builder_meta.get("candidate_pool_counts") or {}
            ),
            "candidate_stage_timings_ms": dict(
                row_builder_meta.get("candidate_stage_timings_ms") or {}
            ),
            "artifact_source": "request_build",
            "promotion_status": "pending",
            "background_refresh_scheduled": False,
            "heavy_rows_pending": False,
            "heavy_rows_hydrated": True,
            "nearline_precompute": self._build_nearline_precompute_diagnostics(
                precompute_cache_hit=bool(precompute_state["precompute_cache_hit"]),
                precompute_hit=bool(precompute_state["precompute_hit"]),
                precompute_stale=bool(precompute_state["precompute_stale"]),
                precompute_snapshot=precompute_snapshot,
                row_builder_meta=row_builder_meta,
            ),
            "row_builder_budget_ms": {
                "required": int(RECOMMEND_REQUIRED_ROW_TIMEOUT_SECONDS * 1000),
                "optional": int(RECOMMEND_OPTIONAL_ROW_TIMEOUT_SECONDS * 1000),
                "finalize": int(RECOMMEND_ROW_FINALIZE_BUDGET_SECONDS * 1000),
                "total": int(RECOMMEND_TOTAL_ROW_BUILD_BUDGET_SECONDS * 1000),
                "disabled": bool(RECOMMEND_DISABLE_TIMEOUTS),
            },
            "request_mode": "request_build",
            "artifact_bypass_reason": "",
            "live_row_refresh": False,
            "live_row_refresh_ms": 0,
            "time_to_first_home_payload_ms": total_build_ms,
            "total_build_ms": total_build_ms,
        }

    def _build_rows_v41(
        self,
        profile: Dict[str, Any],
        *,
        precompute_snapshot: Dict[str, Any] | None = None,
        trace: Dict[str, Any] | None = None,
        allow_live_snapshot_build: bool = False,
        force_rich_rows: bool = False,
        launch_tier_only: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        return build_rows_for_snapshot(
            server=self._server,
            profile=profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=allow_live_snapshot_build,
            force_rich_rows=force_rich_rows,
            launch_tier_only=launch_tier_only,
        )

    def _prepare_next_session_response(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        started_at = time.perf_counter()
        profile_started_at = time.perf_counter()
        _legacy_req, profile = build_home_profile(req)
        profile_ms = int((time.perf_counter() - profile_started_at) * 1000)
        server._trace_stage(trace, "recommend.profile_build", profile_started_at)
        prepare_started_at = time.perf_counter()
        prepared = schedule_home_artifact_warmup(
            server=server,
            user_scope_id=profile.get("user_scope_id") or "guest",
            profile=profile,
            force=False,
        )
        prepare_ms = int((time.perf_counter() - prepare_started_at) * 1000)
        request_ms = int((time.perf_counter() - started_at) * 1000)
        diagnostics = {
            "cache_hit": False,
            "ranking_backend": "background_prepare",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "profile_build_ms": profile_ms,
            "row_assembly_ms": 0,
            "generator_timings_ms": {},
            "request_mode": "background_prepare",
            "background_refresh_scheduled": bool(prepared),
            "prepare_next_session": True,
            "prepare_only": True,
            "prepare_force_refresh": False,
            "promotion_status": "pending",
            "artifact_source": "background_prepare",
            "artifact_bypass_reason": "",
            "row_status": {},
            "time_to_first_home_payload_ms": request_ms,
            "total_build_ms": request_ms,
            "prepare_schedule_ms": prepare_ms,
        }
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=background_prepare scheduled={bool(prepared)} "
            f"profile_ms={profile_ms} schedule_ms={prepare_ms}",
            flush=True,
        )
        return {
            "status": "accepted",
            "prepared": bool(prepared),
            "diagnostics": diagnostics,
        }

    def _build_session_from_rows(
        self,
        *,
        profile: Dict[str, Any],
        rows: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
        candidate_snapshot: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        session_id = str(uuid.uuid4())
        now = time.time()
        prepared_rows, prepared_diagnostics = _rows_with_flagship_shells(
            profile=profile,
            rows=rows,
            diagnostics=diagnostics,
        )
        session = {
            "session_id": session_id,
            "user_scope_id": profile["user_scope_id"],
            "profile_key": profile["profile_key"],
            "profile": profile,
            "candidate_snapshot": dict(candidate_snapshot or {}),
            "generated_at": now,
            "expires_at": now + server.RECOMMENDATION_FEED_SESSION_TTL_SECONDS,
            "rows": list(prepared_rows or []),
            "diagnostics": dict(prepared_diagnostics or {}),
        }
        store_feed_session(server, session)
        server._recommendation_record_impressions(
            session,
            visible_impression_rows(
                prepared_rows,
                page_size=server.RECOMMENDATION_ROW_PAGE_SIZE,
            ),
        )
        return session

    def _session_from_launch_artifact(
        self,
        *,
        req,
        profile: Dict[str, Any],
        launch_artifact: Dict[str, Any],
        heavy_artifact: Dict[str, Any] | None = None,
        background_refresh_scheduled: bool = False,
        aggressive_refresh: bool = False,
        refresh_token: str = "",
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = _recommendation_runtime_health(server)
        launch_rows = freshen_launch_rows(
            server,
            profile,
            list(launch_artifact.get("rows") or []),
            aggressive_refresh=aggressive_refresh,
            refresh_token=refresh_token,
        )
        launch_artifact_diagnostics = dict(launch_artifact.get("diagnostics") or {})
        artifact_row_builder_mode = str(
            launch_artifact_diagnostics.get("row_builder_mode") or ""
        )
        launch_tier_only = bool(launch_artifact_diagnostics.get("launch_tier_only")) or (
            artifact_row_builder_mode == "candidate_snapshot_thin_core_v1"
        )
        deferred_row_kinds = list(
            launch_artifact_diagnostics.get("deferred_row_kinds") or []
        )
        if launch_tier_only and not deferred_row_kinds:
            deferred_row_kinds = list(registry_deferred_row_kinds())
        heavy_rows = list((heavy_artifact or {}).get("rows") or [])
        rows = merge_home_rows(launch_rows, heavy_rows)
        launch_age_ms = max(
            0,
            int((time.time() - float(launch_artifact.get("generated_at") or 0.0)) * 1000),
        )
        diagnostics = {
            "cache_hit": True,
            "ranking_backend": "artifact_launch",
            "embedding_backend": server.ASSISTANT_EMBED_BACKEND,
            "profile_build_ms": 0,
            "row_assembly_ms": 0,
            "generator_timings_ms": {},
            "profile_cache_hit": bool((profile.get("profile_runtime") or {}).get("cache_hit")),
            "profile_cache_source": (profile.get("profile_runtime") or {}).get("source") or "",
            "catalog_feature_version": profile.get("catalog_feature_version") or "",
            "taste_profile_version": profile.get("taste_profile_version") or "",
            "scene_graph_version": profile.get("scene_graph_version") or "",
            "feature_source": profile.get("feature_source") or "",
            "negative_feedback_applied": bool(profile.get("negative_feedback_applied")),
            "row_status": {
                row_kind: {"status": status}
                for row_kind, status in dict(launch_artifact.get("row_status") or {}).items()
            },
            "row_order": [row.get("id") for row in rows if isinstance(row, dict)],
            "row_item_counts": {
                row.get("id"): len(row.get("items") or [])
                for row in rows
                if isinstance(row, dict)
            },
            "artifact_source": launch_artifact.get("resolved_from") or "promoted",
            "artifact_resolved_from": launch_artifact.get("resolved_from") or "promoted",
            "artifact_bypass_reason": "",
            "artifact_age_ms": launch_age_ms,
            "promotion_status": launch_artifact.get("promotion_status") or "promoted",
            "artifact_quality_score": float(launch_artifact.get("quality_score") or 0.0),
            "artifact_quality_reasons": list(launch_artifact.get("quality_reasons") or []),
            "artifact_row_builder_mode": artifact_row_builder_mode,
            "worker_runtime": worker_runtime,
            "background_refresh_scheduled": bool(background_refresh_scheduled),
            "heavy_rows_pending": not bool(heavy_rows),
            "heavy_rows_hydrated": bool(heavy_rows),
            "heavy_rows_source": (heavy_artifact or {}).get("resolved_from") or "",
            "heavy_rows_promotion_status": (heavy_artifact or {}).get("promotion_status") or "",
            "launch_freshness_applied": True,
            "launch_freshness_mode": "manual_refresh" if aggressive_refresh else "launch",
            "launch_tier_only": launch_tier_only,
            "candidate_snapshot_source": "launch_artifact",
            "candidate_pool_counts": {},
            "candidate_stage_timings_ms": {},
            "deferred_row_kinds": deferred_row_kinds,
            "deferred_rows_pending": bool(
                launch_artifact_diagnostics.get("deferred_rows_pending")
            )
            or bool(deferred_row_kinds),
            "nearline_precompute": {
                "hit": True,
                "stale": bool(launch_artifact.get("stale")),
                "resolved_from": launch_artifact.get("resolved_from") or "promoted",
                "launch_artifact": True,
                "snapshot_hit": False,
                "rows_seeded": len(rows),
                "candidate_pools_seeded": 0,
                "snapshot_generated_at": float(launch_artifact.get("generated_at") or 0.0),
                "snapshot_expires_at": float(launch_artifact.get("expires_at") or 0.0),
                "runtime": precompute_runtime_snapshot(),
            },
            "request_mode": "launch_artifact",
            "live_row_refresh": False,
            "live_row_refresh_ms": 0,
            "time_to_first_home_payload_ms": 0,
            "total_build_ms": 0,
        }
        return self._build_session_from_rows(
            profile=profile,
            rows=rows,
            diagnostics=diagnostics,
            candidate_snapshot=dict(launch_artifact.get("candidate_snapshot") or {}),
        )

    def _try_launch_artifact_session(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        server = self._server
        user_scope_id = profile.get("user_scope_id") or "guest"
        profile_key = profile.get("profile_key") or ""
        rich_launch_required = _should_bootstrap_rich_snapshot(profile)
        launch_artifact = get_home_launch_artifact(
            user_scope_id=user_scope_id,
            include_usable=True,
            server=server,
        )
        if not isinstance(launch_artifact, dict):
            launch_artifact = get_home_launch_artifact_for_profile(
                profile_key=profile_key,
                include_usable=True,
                server=server,
            )
        if not isinstance(launch_artifact, dict):
            return None
        if _launch_artifact_is_thin_core(launch_artifact):
            invalidate_home_snapshots(
                user_scope_id=user_scope_id,
                profile_key=profile_key,
                include_artifacts=True,
                server=server,
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.thin_launch_artifact_rejected",
                True,
            )
            return None
        refresh_gap_row_kinds: List[str] = []
        if rich_launch_required:
            artifact_rejected = False
            rejection_reason = ""
            if _launch_artifact_supports_rich_rows(launch_artifact):
                blocking_gap_row_kinds = _launch_artifact_blocking_gap_kinds(
                    launch_artifact
                )
                refresh_gap_row_kinds = _launch_artifact_refresh_gap_kinds(
                    launch_artifact
                )
                if blocking_gap_row_kinds:
                    artifact_rejected = True
                    rejection_reason = "blocking_launch_contract"
                    server._trace_put(
                        trace,
                        "ranking_meta",
                        "recommend.blocking_launch_gap_row_kinds",
                        list(blocking_gap_row_kinds),
                    )
                elif refresh_gap_row_kinds:
                    server._trace_put(
                        trace,
                        "ranking_meta",
                        "recommend.launch_refresh_gap_row_kinds",
                        list(refresh_gap_row_kinds),
                    )
            if artifact_rejected:
                invalidate_home_snapshots(
                    user_scope_id=user_scope_id,
                    profile_key=profile_key,
                    include_artifacts=True,
                    server=server,
                )
                server._trace_put(
                    trace,
                    "ranking_meta",
                    f"recommend.{rejection_reason}_artifact_rejected",
                    True,
                )
                return None
        heavy_artifact = None
        if bool(req.hydrate_heavy_rows):
            heavy_artifact = get_home_heavy_artifact(
                user_scope_id=user_scope_id,
                include_usable=True,
                server=server,
            )
            if not isinstance(heavy_artifact, dict):
                heavy_artifact = get_home_heavy_artifact_for_profile(
                    profile_key=profile_key,
                    include_usable=True,
                    server=server,
                )
        artifact_policy = self._resolve_launch_artifact_policy(
            req,
            launch_artifact=launch_artifact,
            rich_launch_required=rich_launch_required,
        )
        if refresh_gap_row_kinds:
            artifact_policy["should_schedule_refresh"] = True
        refresh_scheduled = False
        if artifact_policy["should_schedule_refresh"]:
            refresh_scheduled = schedule_home_artifact_warmup(
                server=server,
                user_scope_id=user_scope_id,
                profile=profile,
                force=bool(artifact_policy["prefer_fresh_rows"]),
            )
        if artifact_policy["should_bypass_artifact"]:
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.launch_artifact_bypassed_for_refresh",
                artifact_policy["artifact_bypass_reason"] or artifact_policy["resolved_from"],
            )
            return None
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.launch_artifact_source",
            artifact_policy["resolved_from"],
        )
        return self._session_from_launch_artifact(
            req=req,
            profile=profile,
            launch_artifact=launch_artifact,
            heavy_artifact=heavy_artifact if isinstance(heavy_artifact, dict) else None,
            background_refresh_scheduled=refresh_scheduled,
            aggressive_refresh=bool(artifact_policy["prefer_fresh_rows"]),
            refresh_token=str(getattr(req, "refresh_token", "") or ""),
        )

    def _load_cached_launch_session(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        profile_ms: int = 0,
        worker_runtime: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        if bool(req.force_refresh):
            return None
        server = self._server
        artifact_started_at = time.perf_counter()
        artifact_session = self._try_launch_artifact_session(
            req,
            profile=profile,
            trace=trace,
        )
        artifact_load_ms = int((time.perf_counter() - artifact_started_at) * 1000)
        server._trace_put(
            trace,
            "timings_ms",
            "recommend.artifact_load_ms",
            artifact_load_ms,
        )
        if artifact_session is None:
            return None
        diagnostics = dict(artifact_session.get("diagnostics") or {})
        diagnostics["profile_build_ms"] = profile_ms
        diagnostics["artifact_load_ms"] = artifact_load_ms
        diagnostics["worker_runtime"] = worker_runtime or {}
        diagnostics["request_mode"] = (
            "refresh_artifact"
            if bool(getattr(req, "prefer_fresh_rows", False))
            else "launch_artifact"
        )
        diagnostics["time_to_first_home_payload_ms"] = profile_ms + artifact_load_ms
        diagnostics["total_build_ms"] = profile_ms + artifact_load_ms
        artifact_session["diagnostics"] = diagnostics
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=artifact_load done artifact_ms={artifact_load_ms} "
            f"rows={len(artifact_session.get('rows') or [])}",
            flush=True,
        )
        return artifact_session

    def _resolve_precompute_session_state(
        self,
        req,
        *,
        profile: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
        worker_runtime: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        worker_runtime = dict(worker_runtime or {})
        resolution_started_at = time.perf_counter()
        snapshot_state = self._load_precompute_snapshot(
            req,
            profile=profile,
        )
        precompute_state = self._apply_precompute_snapshot_policy(
            req=req,
            profile=profile,
            trace=trace,
            worker_runtime=worker_runtime,
            precompute_snapshot=snapshot_state["precompute_snapshot"],
            precompute_hit=bool(snapshot_state["precompute_hit"]),
        )
        precompute_cache_hit = bool(snapshot_state.get("precompute_cache_hit"))
        precompute_stale = bool(
            (precompute_state["precompute_snapshot"] or {}).get("stale")
        )
        if (not precompute_state["precompute_hit"]) or precompute_stale:
            schedule_home_artifact_warmup(
                server=server,
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile=profile,
                force=False,
            )
        precompute_resolution_ms = int(
            (time.perf_counter() - resolution_started_at) * 1000
        )
        server._trace_put(
            trace,
            "timings_ms",
            "recommend.precompute_resolution_ms",
            precompute_resolution_ms,
        )
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=precompute resolved_hit={precompute_cache_hit} "
            f"snapshot_ready={bool(precompute_state['precompute_hit'])} "
            f"stale={precompute_stale} "
            f"source={((precompute_state['precompute_snapshot'] or {}).get('resolved_from') or '')} "
            f"lookup_ms={precompute_resolution_ms} "
            f"bootstrap_ms={int(precompute_state['rich_bootstrap_ms'] or 0)} "
            f"worker_unhealthy={bool(worker_runtime.get('external_worker_unhealthy'))}",
            flush=True,
        )
        precompute_state["precompute_stale"] = precompute_stale
        precompute_state["precompute_cache_hit"] = precompute_cache_hit
        precompute_state["precompute_resolution_ms"] = precompute_resolution_ms
        return precompute_state

    def _build_session_v41(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        prune_feed_cache(server)
        started_at = time.perf_counter()

        profile_started_at = time.perf_counter()
        _legacy_req, profile = build_home_profile(req)
        profile_ms = int((time.perf_counter() - profile_started_at) * 1000)
        server._trace_stage(trace, "recommend.profile_build", profile_started_at)
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=profile_build done profile_ms={profile_ms}",
            flush=True,
        )
        profile_key = profile["profile_key"]
        worker_runtime = _recommendation_runtime_health(server)
        artifact_session = self._load_cached_launch_session(
            req,
            profile=profile,
            trace=trace,
            profile_ms=profile_ms,
            worker_runtime=worker_runtime,
        )
        if artifact_session is not None:
            return artifact_session
        precompute_state = self._resolve_precompute_session_state(
            req,
            profile=profile,
            trace=trace,
            worker_runtime=worker_runtime,
        )
        precompute_snapshot = precompute_state["precompute_snapshot"]
        precompute_hit = bool(precompute_state["precompute_hit"])
        precompute_stale = bool(precompute_state["precompute_stale"])
        rich_launch_required = bool(precompute_state["rich_launch_required"])
        force_rich_rows = bool(precompute_state["force_rich_rows"])
        runtime_bootstrap_scheduled = bool(precompute_state["runtime_bootstrap_scheduled"])
        rich_bootstrap_deferred = bool(precompute_state["rich_bootstrap_deferred"])
        rich_bootstrap_attempted = bool(precompute_state["rich_bootstrap_attempted"])
        rich_bootstrap_ms = int(precompute_state["rich_bootstrap_ms"] or 0)
        precompute_resolution_ms = int(precompute_state["precompute_resolution_ms"] or 0)
        if (
            not precompute_hit
            and not rich_bootstrap_attempted
            and rich_launch_required
            and RECOMMEND_RICH_BOOTSTRAP_ON_LAUNCH_MISS
            and not bool(req.force_refresh)
            and not bool(getattr(req, "prefer_fresh_rows", False))
            and not bool(req.hydrate_heavy_rows)
            and not bool(worker_runtime.get("external_worker_unhealthy"))
        ):
            rich_bootstrap_attempted = True
            force_rich_rows = True
            try:
                rich_bootstrap_started_at = time.perf_counter()
                precompute_snapshot = _build_rich_bootstrap_snapshot(
                    server=server,
                    user_scope_id=profile.get("user_scope_id") or "guest",
                    profile=profile,
                )
                rich_bootstrap_ms = int(
                    (time.perf_counter() - rich_bootstrap_started_at) * 1000
                )
                precompute_hit = bool(
                    (precompute_snapshot or {}).get("candidate_snapshot")
                )
                server._trace_stage(
                    trace,
                    "recommend.rich_bootstrap",
                    rich_bootstrap_started_at,
                )
                server._trace_put(
                    trace,
                    "timings_ms",
                    "recommend.rich_bootstrap_ms",
                    rich_bootstrap_ms,
                )
                server._trace_put(
                    trace,
                    "ranking_meta",
                    "recommend.rich_bootstrap_snapshot",
                    bool(precompute_hit),
                )
            except Exception as exc:
                server._trace_put(
                    trace,
                    "errors",
                    "recommend.rich_bootstrap_error",
                    str(exc)[:240],
                )
                precompute_snapshot = None
                precompute_hit = False
        launch_tier_only = (
            not bool(req.force_refresh)
            and not bool(getattr(req, "prefer_fresh_rows", False))
            and not bool(req.hydrate_heavy_rows)
            and not rich_bootstrap_attempted
        )

        row_started_at = time.perf_counter()
        rows, generator_timings, row_diagnostics, row_builder_meta = self._build_rows_v41(
            profile,
            precompute_snapshot=precompute_snapshot,
            trace=trace,
            allow_live_snapshot_build=bool(req.force_refresh)
            or RECOMMEND_LIVE_SNAPSHOT_ON_MISS
            or rich_bootstrap_attempted
            or force_rich_rows,
            force_rich_rows=force_rich_rows,
            launch_tier_only=launch_tier_only,
        )
        row_ms = int((time.perf_counter() - row_started_at) * 1000)
        server._trace_stage(trace, "recommend.row_assembly", row_started_at)
        print(
            "[EBB:recommend][progress] "
            f"request_id={trace.get('request_id') if isinstance(trace, dict) else ''} "
            f"stage=row_assembly done row_ms={row_ms} rows={len(rows or [])}",
            flush=True,
        )

        diagnostics = self._build_request_session_diagnostics(
            profile=profile,
            profile_ms=profile_ms,
            profile_key=profile_key,
            worker_runtime=worker_runtime,
            precompute_state=precompute_state,
            row_ms=row_ms,
            generator_timings=generator_timings,
            row_diagnostics=row_diagnostics,
            row_builder_meta=row_builder_meta,
            rows=rows,
            started_at=started_at,
        )
        artifact_result = store_home_serving_artifacts(
            user_scope_id=profile.get("user_scope_id") or "guest",
            profile_key=profile.get("profile_key") or "",
            rows=rows,
            candidate_snapshot=dict(row_builder_meta.get("candidate_snapshot") or {}),
            diagnostics=diagnostics,
            row_diagnostics=row_diagnostics,
            source_signature=profile.get("profile_key") or "",
        )
        diagnostics["promotion_status"] = artifact_result.get("promotion_status") or "rejected"
        diagnostics["artifact_source"] = "request_build"
        diagnostics["heavy_rows_pending"] = not bool(
            (artifact_result.get("heavy_artifact") or {}).get("rows")
        )
        diagnostics["heavy_rows_hydrated"] = True
        diagnostics["artifact_quality_reasons"] = list(
            artifact_result.get("quality_reasons") or []
        )
        session = self._build_session_from_rows(
            profile=profile,
            rows=rows,
            diagnostics=diagnostics,
            candidate_snapshot=dict(row_builder_meta.get("candidate_snapshot") or {}),
        )
        server._trace_put(trace, "candidate_counts", "recommend.rows_emitted", len(rows))
        server._trace_put(
            trace,
            "candidate_counts",
            "recommend.total_items",
            sum((diagnostics.get("row_item_counts") or {}).values()),
        )
        server._trace_put(
            trace,
            "source_counts",
            "recommend.row_item_counts",
            diagnostics.get("row_item_counts") or {},
        )
        server._trace_put(
            trace,
            "source_counts",
            "recommend.row_source_counts",
            diagnostics.get("row_source_counts") or {},
        )
        server._trace_put(trace, "ranking_meta", "recommend.profile_key", profile_key)
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.home_model_key",
            diagnostics.get("home_ranking_model_key") or "",
        )
        server._trace_put(
            trace,
            "ranking_meta",
            "recommend.home_model_version",
            diagnostics.get("home_ranking_model_version") or "",
        )
        if not bool(diagnostics.get("snapshot_cacheable")):
            invalidate_home_snapshots(
                user_scope_id=profile.get("user_scope_id") or "guest",
                profile_key=profile_key,
                include_artifacts=False,
            )
            server._trace_put(
                trace,
                "ranking_meta",
                "recommend.snapshot_invalidated",
                list(diagnostics.get("snapshot_quality_reasons") or []),
            )
        return session

    def _row_slice(self, row: Dict[str, Any], offset: int, page_size: int) -> Dict[str, Any]:
        items = list(row.get("items") or [])
        bounded_offset = max(int(offset or 0), 0)
        page_limit = max(1, min(page_size, 12))
        visible = items[bounded_offset: bounded_offset + page_limit]
        next_offset = bounded_offset + len(visible)
        can_extend = bool(row.get("can_extend"))
        return {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "item_type": row.get("item_type") or "track",
            "row_style": row.get("row_style") or "",
            "meta": dict(row.get("meta") or {}),
            "items": visible,
            "next_offset": next_offset,
            "has_more": next_offset < len(items) or can_extend,
        }

    def _feed_response_v41(self, session: Dict[str, Any]) -> Dict[str, Any]:
        initial_rows = [
            self._row_slice(row, 0, self._server.RECOMMENDATION_ROW_PAGE_SIZE)
            for row in session.get("rows") or []
        ]
        flatten_priority = {
            "todays_pick": -2,
            "mixed_for_you": -1,
            "continue_listening": 0,
            "because_you_played": 1,
            "listeners_like_you": 2,
            "rediscover": 3,
            "deep_cuts": 4,
            "trending_by_genre": 5,
            "offline_ready": 6,
            "trending_for_you": 7,
            "quiet_picks": 8,
            "frequently_listened": 9,
        }
        flattened = []
        for row in sorted(
            initial_rows,
            key=lambda item: flatten_priority.get(item.get("kind"), 50),
        ):
            if str(row.get("item_type") or "track") != "track":
                continue
            flattened.extend(row.get("items") or [])
            if len(flattened) >= 18:
                break
        return {
            "status": "success",
            "session_id": session["session_id"],
            "generated_at": session["generated_at"],
            "expires_at": session["expires_at"],
            "rows": initial_rows,
            "recommendations": flattened[:18],
            "has_more": any(row["has_more"] for row in initial_rows),
            "next_offset": sum(len(row.get("items") or []) for row in initial_rows),
            "diagnostics": session.get("diagnostics") or {},
        }

    def _active_flagship_rows_for_session(
        self,
        session: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        rows = [
            dict(row)
            for row in list(session.get("rows") or [])
            if isinstance(row, dict)
        ]
        active_rows: List[Dict[str, Any]] = []
        for row in rows:
            row_kind = self._server._recommendation_trim_text(
                row.get("kind") or row.get("id") or ""
            )
            if row_kind not in _FLAGSHIP_DEFERRED_ROW_KINDS:
                continue
            if _flagship_row_needs_live_refinement(row_kind, row):
                active_rows.append(row)
        return active_rows

    def _apply_flagship_row_refresh(
        self,
        req,
        *,
        session: Dict[str, Any],
        target_row: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
    ) -> tuple[Dict[str, Any] | None, Dict[str, Any], bool, str]:
        server = self._server
        stored_rows = list(session.get("rows") or [])
        target_row_id = server._recommendation_trim_text(
            target_row.get("id") or target_row.get("kind") or ""
        )
        target_index = next(
            (
                index
                for index, row in enumerate(stored_rows)
                if server._recommendation_trim_text(
                    row.get("id") or row.get("kind") or ""
                )
                == target_row_id
            ),
            -1,
        )
        if target_index < 0:
            return None, session, False, ""

        refine_started_at = time.perf_counter()
        refreshed_row, refreshed_profile, refreshed_snapshot, refresh_source = (
            self._resolve_flagship_row_for_session(
                req,
                session=session,
                target_row=target_row,
                trace=trace,
            )
        )
        refine_ms = int((time.perf_counter() - refine_started_at) * 1000)

        if not isinstance(refreshed_row, dict):
            if bool(getattr(req, "prepare_next_session", False)):
                schedule_home_artifact_warmup(
                    server=server,
                    user_scope_id=session.get("user_scope_id") or "guest",
                    profile=session.get("profile") or {},
                    force=bool(getattr(req, "prefer_fresh_rows", False)),
                )
            return None, session, False, refresh_source

        stored_rows[target_index] = refreshed_row
        session["rows"], refreshed_diagnostics = _rows_with_flagship_shells(
            profile=refreshed_profile,
            rows=stored_rows,
            diagnostics={
                **dict(session.get("diagnostics") or {}),
                "request_mode": "flagship_row_refresh",
                "flagship_row_refresh": True,
                "flagship_row_refresh_ms": refine_ms,
                "flagship_row_refresh_target": str(
                    target_row.get("kind") or target_row.get("id") or ""
                ),
                "flagship_row_refresh_source": refresh_source,
            },
        )
        session["profile"] = refreshed_profile
        if refreshed_profile.get("profile_key"):
            session["profile_key"] = refreshed_profile.get("profile_key") or ""
        if refreshed_snapshot:
            session["candidate_snapshot"] = dict(refreshed_snapshot)
        if bool(getattr(req, "prepare_next_session", False)):
            refreshed_diagnostics["background_refresh_scheduled"] = bool(
                schedule_home_artifact_warmup(
                    server=server,
                    user_scope_id=refreshed_profile.get("user_scope_id") or "guest",
                    profile=refreshed_profile,
                    force=bool(getattr(req, "prefer_fresh_rows", False)),
                )
            )
        session["diagnostics"] = refreshed_diagnostics
        store_feed_session(server, session)
        final_row = (
            _row_by_kind(
                list(session.get("rows") or []),
                refreshed_row.get("kind") or "",
            )
            or refreshed_row
        )
        advanced = _flagship_row_materially_improves(target_row, final_row)
        return final_row, session, advanced, refresh_source

    def _flagship_row_response_payload(
        self,
        *,
        session: Dict[str, Any],
        row: Dict[str, Any],
        limit: int,
    ) -> Dict[str, Any]:
        sliced = self._row_slice(
            row,
            0,
            max(
                len(list(row.get("items") or [])),
                limit or self._server.RECOMMENDATION_ROW_PAGE_SIZE,
            ),
        )
        sliced["has_more"] = bool(row.get("has_more"))
        return {
            "status": "success",
            "session_id": session["session_id"],
            "generated_at": session["generated_at"],
            "expires_at": session["expires_at"],
            "row": sliced,
            "diagnostics": session.get("diagnostics") or {},
        }

    def _flagship_stream_response_v41(
        self,
        req,
    ) -> StreamingResponse:
        server = self._server
        prune_feed_cache(server)
        session_id = server._recommendation_trim_text(req.session_id)
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = load_feed_session(server, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Recommendation session expired")
        if session.get("user_scope_id") != server._recommendation_trim_text(
            req.user_scope_id or "guest"
        ):
            raise HTTPException(
                status_code=403,
                detail="Recommendation session scope mismatch",
            )

        def _sse(event_name: str, payload: Dict[str, Any]) -> str:
            return (
                f"event: {event_name}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )

        def _stream():
            current_session = dict(session)
            max_attempts = 8
            attempt = 0
            yield _sse(
                "open",
                {
                    "session_id": current_session.get("session_id") or "",
                    "pending_row_ids": [
                        str(row.get("id") or row.get("kind") or "").strip()
                        for row in self._active_flagship_rows_for_session(
                            current_session
                        )
                    ],
                },
            )
            while attempt < max_attempts:
                active_rows = self._active_flagship_rows_for_session(
                    current_session
                )
                if not active_rows:
                    yield _sse(
                        "complete",
                        {
                            "session_id": current_session.get("session_id") or "",
                            "pending_row_ids": [],
                        },
                    )
                    return
                progressed = False
                for target_row in active_rows:
                    refreshed_row, current_session, advanced, refresh_source = (
                        self._apply_flagship_row_refresh(
                            req,
                            session=current_session,
                            target_row=target_row,
                            trace=None,
                        )
                    )
                    if not isinstance(refreshed_row, dict):
                        continue
                    payload = self._flagship_row_response_payload(
                        session=current_session,
                        row=refreshed_row,
                        limit=req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                    )
                    payload["refresh_source"] = refresh_source
                    yield _sse("row", payload)
                    progressed = progressed or advanced
                remaining_rows = self._active_flagship_rows_for_session(
                    current_session
                )
                if not remaining_rows:
                    yield _sse(
                        "complete",
                        {
                            "session_id": current_session.get("session_id") or "",
                            "pending_row_ids": [],
                        },
                    )
                    return
                retry_after_ms = min(
                    max(
                        int(dict(row.get("meta") or {}).get("refine_after_ms") or 900),
                        250,
                    )
                    for row in remaining_rows
                )
                yield _sse(
                    "idle",
                    {
                        "session_id": current_session.get("session_id") or "",
                        "pending_row_ids": [
                            str(row.get("id") or row.get("kind") or "").strip()
                            for row in remaining_rows
                        ],
                        "retry_after_ms": retry_after_ms,
                        "progressed": progressed,
                    },
                )
                if not progressed and attempt >= max_attempts - 1:
                    yield _sse(
                        "complete",
                        {
                            "session_id": current_session.get("session_id") or "",
                            "pending_row_ids": [
                                str(row.get("id") or row.get("kind") or "").strip()
                                for row in remaining_rows
                            ],
                        },
                    )
                    return
                attempt += 1
                time.sleep(retry_after_ms / 1000.0)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def _row_page_response_v41(
        self,
        req,
        *,
        trace: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        server = self._server
        prune_feed_cache(server)
        session_id = server._recommendation_trim_text(req.session_id)
        row_id = server._recommendation_trim_text(req.row_id)
        if not session_id or not row_id:
            raise HTTPException(status_code=400, detail="session_id and row_id are required")
        load_started_at = time.perf_counter()
        session = load_feed_session(server, session_id)
        server._trace_stage(trace, "recommend.session_lookup", load_started_at)
        if session is None:
            raise HTTPException(status_code=404, detail="Recommendation session expired")
        if session.get("user_scope_id") != server._recommendation_trim_text(req.user_scope_id or "guest"):
            raise HTTPException(status_code=403, detail="Recommendation session scope mismatch")
        stored_rows = list(session.get("rows") or [])
        for index, row in enumerate(stored_rows):
            if row.get("id") != row_id:
                continue
            target_row = row
            row_context = server._recommendation_trim_text(getattr(req, "row_context", ""))
            original_item_count = len(list(target_row.get("items") or []))
            if (
                row_context == _FLAGSHIP_REFINE_ROW_CONTEXT
                and str(target_row.get("kind") or "") in _FLAGSHIP_DEFERRED_ROW_KINDS
            ):
                refreshed_row, session, _advanced, _refresh_source = (
                    self._apply_flagship_row_refresh(
                        req,
                        session=session,
                        target_row=target_row,
                        trace=trace,
                    )
                )
                if isinstance(refreshed_row, dict):
                    target_row = refreshed_row
                sliced = self._flagship_row_response_payload(
                    session=session,
                    row=target_row,
                    limit=req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                )["row"]
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.row_page_items",
                    len(sliced.get("items") or []),
                )
                return {
                    "status": "success",
                    "session_id": session["session_id"],
                    "generated_at": session["generated_at"],
                    "expires_at": session["expires_at"],
                    "row": sliced,
                    "diagnostics": session.get("diagnostics") or {},
                }
            if row_context.startswith("genre_tab:") and str(target_row.get("kind") or "") == "trending_by_genre":
                candidate_snapshot = self._resolve_candidate_snapshot_for_session(session)
                if candidate_snapshot:
                    requested_tab_id = row_context.split(":", 1)[1].strip()
                    try:
                        refreshed_row = refresh_trending_by_genre_row(
                            server=server,
                            row=target_row,
                            profile=session.get("profile") or {},
                            snapshot=candidate_snapshot,
                            tab_id=requested_tab_id,
                        )
                    except Exception as exc:
                        server._trace_put(
                            trace,
                            "errors",
                            "recommend.row_context_refresh_error",
                            str(exc)[:240],
                        )
                        refreshed_row = None
                    if isinstance(refreshed_row, dict):
                        stored_rows[index] = refreshed_row
                        session["rows"] = stored_rows
                        store_feed_session(server, session)
                        target_row = refreshed_row
                sliced = self._row_slice(
                    target_row,
                    0,
                    max(
                        len(list(target_row.get("items") or [])),
                        req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                    ),
                )
                sliced["has_more"] = False
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.row_page_items",
                    len(sliced.get("items") or []),
                )
                return {
                    "status": "success",
                    "session_id": session["session_id"],
                    "generated_at": session["generated_at"],
                    "expires_at": session["expires_at"],
                    "row": sliced,
                    "diagnostics": session.get("diagnostics") or {},
                }
            if row_context == "live_refresh" and str(target_row.get("kind") or "") == "continue_listening":
                live_refresh_started_at = time.perf_counter()
                refreshed_row = None
                refreshed_profile = session.get("profile") or {}
                try:
                    _legacy_req, refreshed_profile = build_home_profile(req)
                    refreshed_snapshot = trim_home_candidate_snapshot(
                        build_home_candidate_snapshot_fast_fallback(
                            server=server,
                            profile=refreshed_profile,
                        )
                    )
                    refreshed_row = build_continue_listening_row(
                        server=server,
                        profile=refreshed_profile,
                        snapshot=refreshed_snapshot,
                    )
                except Exception:
                    refreshed_row = None
                live_refresh_ms = int(
                    (time.perf_counter() - live_refresh_started_at) * 1000
                )
                if isinstance(refreshed_row, dict):
                    refreshed_row["id"] = target_row.get("id") or refreshed_row.get("id") or "continue_listening"
                    refreshed_row["title"] = target_row.get("title") or refreshed_row.get("title") or "Continue listening"
                    refreshed_row["kind"] = target_row.get("kind") or refreshed_row.get("kind") or "continue_listening"
                    stored_rows[index] = refreshed_row
                    session["rows"] = stored_rows
                    session["profile"] = refreshed_profile
                    diagnostics = dict(session.get("diagnostics") or {})
                    diagnostics["request_mode"] = "live_row_refresh"
                    diagnostics["live_row_refresh"] = True
                    diagnostics["live_row_refresh_ms"] = live_refresh_ms
                    session["diagnostics"] = diagnostics
                    store_feed_session(server, session)
                    target_row = refreshed_row
                sliced = self._row_slice(
                    target_row,
                    0,
                    max(
                        len(list(target_row.get("items") or [])),
                        req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
                    ),
                )
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.row_page_items",
                    len(sliced.get("items") or []),
                )
                return {
                    "status": "success",
                    "session_id": session["session_id"],
                    "generated_at": session["generated_at"],
                    "expires_at": session["expires_at"],
                    "row": sliced,
                    "diagnostics": session.get("diagnostics") or {},
                }
            if should_extend_row(
                target_row,
                req.offset,
                req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
            ):
                candidate_snapshot = self._resolve_candidate_snapshot_for_session(session)
                if candidate_snapshot:
                    try:
                        extended_row = extend_row_from_snapshot(
                            server=server,
                            row=target_row,
                            profile=session.get("profile") or {},
                            snapshot=candidate_snapshot,
                            page_size=max(req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE, 10),
                        )
                    except Exception as exc:
                        server._trace_put(
                            trace,
                            "errors",
                            "recommend.row_extension_error",
                            str(exc)[:240],
                        )
                        extended_row = dict(target_row)
                        extended_row["can_extend"] = False
                else:
                    schedule_home_warmup(
                        user_scope_id=session.get("user_scope_id") or "guest",
                        profile=session.get("profile") or {},
                    )
                    extended_row = dict(target_row)
                    extended_row["can_extend"] = False
                if (
                    len(list(extended_row.get("items") or [])) <= original_item_count
                    and int(req.offset or 0) >= original_item_count
                ):
                    extended_row["can_extend"] = False
                stored_rows[index] = extended_row
                session["rows"] = stored_rows
                store_feed_session(server, session)
                target_row = extended_row
            sliced = self._row_slice(
                target_row,
                req.offset,
                req.limit or server.RECOMMENDATION_ROW_PAGE_SIZE,
            )
            if (
                not list(sliced.get("items") or [])
                and int(req.offset or 0) >= len(list(target_row.get("items") or []))
            ):
                sliced["has_more"] = False
            server._trace_put(
                trace,
                "candidate_counts",
                "recommend.row_page_items",
                len(sliced.get("items") or []),
            )
            return {
                "status": "success",
                "session_id": session["session_id"],
                "generated_at": session["generated_at"],
                "expires_at": session["expires_at"],
                "row": sliced,
                "diagnostics": session.get("diagnostics") or {},
            }
        raise HTTPException(status_code=404, detail="Recommendation row not found")

    def _resolve_candidate_snapshot_for_session(
        self,
        session: Dict[str, Any],
    ) -> Dict[str, Any]:
        server = self._server
        candidate_snapshot = dict(session.get("candidate_snapshot") or {})
        if candidate_snapshot:
            return candidate_snapshot
        warm_snapshot = get_home_snapshot(
            user_scope_id=session.get("user_scope_id") or "guest",
            server=server,
        )
        if isinstance(warm_snapshot, dict):
            candidate_snapshot = dict(warm_snapshot.get("candidate_snapshot") or {})
            if candidate_snapshot:
                candidate_snapshot["resolved_from"] = (
                    warm_snapshot.get("resolved_from") or "user_scope"
                )
                return candidate_snapshot
        profile_snapshot = get_home_snapshot_for_profile(
            profile_key=session.get("profile_key") or "",
            server=server,
        )
        if isinstance(profile_snapshot, dict):
            candidate_snapshot = dict(profile_snapshot.get("candidate_snapshot") or {})
            if candidate_snapshot:
                candidate_snapshot["resolved_from"] = (
                    profile_snapshot.get("resolved_from") or "profile_key"
                )
                return candidate_snapshot
        return {}

    def flagship_stream(self, req):
        return self._flagship_stream_response_v41(req)

    def _resolve_flagship_row_for_session(
        self,
        req,
        *,
        session: Dict[str, Any],
        target_row: Dict[str, Any],
        trace: Dict[str, Any] | None = None,
    ) -> tuple[Dict[str, Any] | None, Dict[str, Any], Dict[str, Any], str]:
        server = self._server
        row_kind = server._recommendation_trim_text(
            target_row.get("kind") or target_row.get("id") or ""
        )
        if row_kind not in _FLAGSHIP_DEFERRED_ROW_KINDS:
            return None, dict(session.get("profile") or {}), {}, ""

        refreshed_profile = dict(session.get("profile") or {})
        try:
            _legacy_req, built_profile = build_home_profile(req)
            if isinstance(built_profile, dict) and built_profile:
                refreshed_profile = built_profile
        except Exception:
            refreshed_profile = dict(session.get("profile") or {})

        normalized_scope = (
            refreshed_profile.get("user_scope_id")
            or session.get("user_scope_id")
            or "guest"
        )
        profile_key = (
            refreshed_profile.get("profile_key")
            or session.get("profile_key")
            or ""
        )
        resolution_session = {
            **dict(session or {}),
            "user_scope_id": normalized_scope,
            "profile_key": profile_key,
            "profile": refreshed_profile,
        }

        launch_artifact = get_home_launch_artifact(
            user_scope_id=normalized_scope,
            include_usable=True,
            server=server,
        )
        if not isinstance(launch_artifact, dict):
            launch_artifact = get_home_launch_artifact_for_profile(
                profile_key=profile_key,
                include_usable=True,
                server=server,
            )
        heavy_artifact = None
        if row_kind in _HEAVY_FLAGSHIP_ROW_KINDS or bool(
            getattr(req, "hydrate_heavy_rows", False)
        ):
            heavy_artifact = get_home_heavy_artifact(
                user_scope_id=normalized_scope,
                include_usable=True,
                server=server,
            )
            if not isinstance(heavy_artifact, dict):
                heavy_artifact = get_home_heavy_artifact_for_profile(
                    profile_key=profile_key,
                    include_usable=True,
                    server=server,
                )

        candidates: List[tuple[Dict[str, Any], str]] = []
        launch_row = _row_by_kind(
            list((launch_artifact or {}).get("rows") or []),
            row_kind,
        )
        if isinstance(launch_row, dict):
            candidates.append((launch_row, "launch_artifact"))
        heavy_row = _row_by_kind(
            list((heavy_artifact or {}).get("rows") or []),
            row_kind,
        )
        if isinstance(heavy_row, dict):
            candidates.append((heavy_row, "heavy_artifact"))

        candidate_snapshot = self._resolve_candidate_snapshot_for_session(
            resolution_session
        )
        if not candidate_snapshot and isinstance(launch_artifact, dict):
            candidate_snapshot = dict(launch_artifact.get("candidate_snapshot") or {})
        if not candidate_snapshot and isinstance(heavy_artifact, dict):
            candidate_snapshot = dict(heavy_artifact.get("candidate_snapshot") or {})
        if candidate_snapshot:
            rebuilt_row = build_row_seed(
                server=server,
                row_kind=row_kind,
                profile=refreshed_profile,
                snapshot=candidate_snapshot,
                relaxed_filter=False,
                allow_empty_diagnostics=False,
                launch_tier_only=False,
                existing_row=target_row,
            )
            if isinstance(rebuilt_row, dict):
                candidates.append(
                    (
                        rebuilt_row,
                        f"candidate_snapshot:{candidate_snapshot.get('resolved_from') or 'session'}",
                    )
                )
                server._trace_put(
                    trace,
                    "candidate_counts",
                    "recommend.flagship_refresh_snapshot_items",
                    len(list(rebuilt_row.get("items") or [])),
                )
        best_existing_row = None
        best_existing_source = ""
        if candidates:
            best_existing_row, best_existing_source = max(
                candidates,
                key=lambda entry: _flagship_row_quality_rank(entry[0]),
            )
        live_refined_snapshot = dict(candidate_snapshot or {})
        if (
            _flagship_row_needs_live_refinement(row_kind, best_existing_row)
            and not live_refined_snapshot
        ):
            try:
                live_refined_snapshot = trim_home_candidate_snapshot(
                    server,
                    build_home_candidate_snapshot_fast_fallback(
                        server=server,
                        profile=refreshed_profile,
                    ),
                )
            except Exception:
                live_refined_snapshot = {}
        if (
            _flagship_row_needs_live_refinement(row_kind, best_existing_row)
            and row_kind == "recommended_artists"
        ):
            try:
                artist_artifacts = build_artist_artifacts(
                    server,
                    refreshed_profile,
                    full_refinement=True,
                )
            except Exception:
                artist_artifacts = {}
            if isinstance(artist_artifacts, dict):
                if list(artist_artifacts.get("artists") or []):
                    live_refined_snapshot["artists"] = list(
                        artist_artifacts.get("artists") or []
                    )
                if isinstance(artist_artifacts.get("meta"), dict):
                    live_refined_snapshot["artist_artifact_meta"] = dict(
                        artist_artifacts.get("meta") or {}
                    )
        elif (
            _flagship_row_needs_live_refinement(row_kind, best_existing_row)
            and row_kind == "recommended_albums"
        ):
            try:
                refined_album_row = build_album_items(
                    server,
                    refreshed_profile,
                    existing_candidate_cache=list(
                        (live_refined_snapshot.get("recommended_album_candidate_cache") or [])
                    ),
                    return_row=True,
                )
            except Exception:
                refined_album_row = {}
            if isinstance(refined_album_row, dict):
                refinement_cache = dict(
                    refined_album_row.pop("_server_refinement_cache", {}) or {}
                )
                refined_albums = [
                    dict(album)
                    for album in list(refined_album_row.get("items") or [])
                    if isinstance(album, dict)
                ]
                if refined_albums:
                    live_refined_snapshot["albums"] = list(refined_albums)
                if list(refinement_cache.get("recommended_album_candidate_cache") or []):
                    live_refined_snapshot["recommended_album_candidate_cache"] = [
                        dict(album)
                        for album in list(
                            refinement_cache.get("recommended_album_candidate_cache") or []
                        )
                        if isinstance(album, dict)
                    ]
                candidates.append((refined_album_row, "live_refine"))
                candidate_snapshot = live_refined_snapshot
        if (
            _flagship_row_needs_live_refinement(row_kind, best_existing_row)
            and live_refined_snapshot
            and row_kind != "recommended_albums"
        ):
            refined_row = build_row_seed(
                server=server,
                row_kind=row_kind,
                profile=refreshed_profile,
                snapshot=live_refined_snapshot,
                relaxed_filter=False,
                allow_empty_diagnostics=False,
                launch_tier_only=False,
                full_refinement=True,
                existing_row=best_existing_row or target_row,
            )
            if isinstance(refined_row, dict):
                candidates.append((refined_row, "live_refine"))
                candidate_snapshot = live_refined_snapshot
        if not candidates:
            return None, refreshed_profile, candidate_snapshot, ""

        source_priority = {
            "live_refine": 3,
            "candidate_snapshot:session": 2,
            "candidate_snapshot:launch_artifact": 2,
            "candidate_snapshot:heavy_artifact": 2,
            "heavy_artifact": 1,
            "launch_artifact": 0,
        }
        best_row, best_source = max(
            candidates,
            key=lambda entry: (
                _flagship_row_quality_rank(entry[0]),
                source_priority.get(str(entry[1] or ""), 0),
            ),
        )
        candidate_row = _clone_flagship_row_identity(
            target_row,
            best_row,
            row_kind=row_kind,
        )
        improved = _flagship_row_materially_improves(target_row, candidate_row)
        candidate_meta = dict(candidate_row.get("meta") or {})
        if list(candidate_row.get("items") or []):
            should_continue_refinement = _flagship_row_should_continue_refinement(
                row_kind,
                target_row,
                candidate_row,
            )
            if should_continue_refinement:
                candidate_meta["partial_ready"] = True
                candidate_meta["refinement_active"] = True
            else:
                candidate_meta.pop("loading_label", None)
                candidate_meta.pop("loading_message", None)
                candidate_meta["partial_ready"] = False
                candidate_meta["refinement_active"] = False
            candidate_row["meta"] = candidate_meta
        normalized_row = _stamp_flagship_row_meta(
            candidate_row,
            previous_row=target_row,
            refinement_active=bool(candidate_meta.get("refinement_active") == True),
        )
        return (
            normalized_row,
            refreshed_profile,
            candidate_snapshot,
            best_source,
        )

    def recommend(self, req):
        server = self._server
        trace = server._trace_start(
            "recommend",
            user_scope_id=req.user_scope_id or "guest",
            surface=req.surface or "home_feed",
            query=req.query or "",
        )
        request_mode = "unknown"
        try:
            request_started_at = time.perf_counter()
            with request_store_runtime(allow_persistent_reads=False):
                parse_started_at = time.perf_counter()
                is_row_page_request = bool(
                    server._recommendation_trim_text(req.session_id)
                    and server._recommendation_trim_text(req.row_id)
                )
                is_prepare_only_request = bool(
                    getattr(req, "prepare_next_session", False)
                ) and not is_row_page_request
                if is_row_page_request:
                    request_mode = "row_page"
                elif is_prepare_only_request:
                    request_mode = "background_prepare"
                else:
                    request_mode = "full_feed"
                recommender_impl = "v1"
                server._trace_put(trace, "ranking_meta", "recommend.request_mode", request_mode)
                server._trace_put(trace, "ranking_meta", "recommend.impl", recommender_impl)
                print(
                    "[EBB:recommend][progress] "
                    f"request_id={trace.get('request_id') or ''} "
                    f"stage=request_parse mode={request_mode} impl={recommender_impl}",
                    flush=True,
                )
                server._trace_stage(trace, "recommend.request_parse", parse_started_at)

                if is_row_page_request:
                    response = self._row_page_response_v41(req, trace=trace)
                elif is_prepare_only_request:
                    response = self._prepare_next_session_response(req, trace=trace)
                else:
                    print(
                        "[EBB:recommend][progress] "
                        f"request_id={trace.get('request_id') or ''} "
                        "stage=build_session start",
                        flush=True,
                    )
                    session = self._build_session_v41(req, trace=trace)
                    print(
                        "[EBB:recommend][progress] "
                        f"request_id={trace.get('request_id') or ''} "
                        f"stage=build_session done rows={len(session.get('rows') or [])}",
                        flush=True,
                    )
                    response = self._feed_response_v41(session)

            serialize_started_at = time.perf_counter()
            server._trace_stage(trace, "recommend.serialize", serialize_started_at)

            diagnostics = response.get("diagnostics")
            if isinstance(diagnostics, dict):
                request_ms = int((time.perf_counter() - request_started_at) * 1000)
                diagnostics.setdefault("request_ms", request_ms)
                diagnostics.update(
                    server._trace_diagnostics(server._trace_finalize(trace, status="success"))
                )
                response["request_id"] = diagnostics.get("request_id") or trace.get("request_id")
                if request_ms >= 6000:
                    slow_row_status = diagnostics.get("row_status") or {}
                    if isinstance(slow_row_status, dict):
                        slow_row_status = {
                            key: (value or {}).get("status")
                            for key, value in slow_row_status.items()
                        }
                    print(
                        "[EBB:recommend][slow] "
                        f"request_id={diagnostics.get('request_id') or ''} "
                        f"request_ms={request_ms} "
                        f"profile_build_ms={diagnostics.get('profile_build_ms') or 0} "
                        f"row_assembly_ms={diagnostics.get('row_assembly_ms') or 0} "
                        f"stage_timings={diagnostics.get('stage_timings_ms') or {}} "
                        f"row_status={slow_row_status}"
                    )
            else:
                server._trace_finalize(trace, status="success")

            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
                session_id=server._recommendation_trim_text(response.get("session_id")),
                model_version=server._recommendation_trim_text(
                    (response.get("diagnostics") or {}).get("home_ranking_model_version")
                    or (response.get("diagnostics") or {}).get("collaborative_model_id")
    ),
)
            return response
        except HTTPException:
            server._trace_finalize(trace, status="failed")
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise
        except Exception as exc:
            print(
                "[EBB:recommend][error] "
                f"mode={request_mode} "
                f"scope={server._recommendation_trim_text(req.user_scope_id or 'guest')} "
                f"session_id={server._recommendation_trim_text(getattr(req, 'session_id', ''))} "
                f"row_id={server._recommendation_trim_text(getattr(req, 'row_id', ''))} "
                f"row_context={server._recommendation_trim_text(getattr(req, 'row_context', ''))} "
                f"offset={int(getattr(req, 'offset', 0) or 0)} "
                f"limit={int(getattr(req, 'limit', 0) or 0)} "
                f"error={str(exc)[:240]}",
                flush=True,
            )
            print(traceback.format_exc(), flush=True)
            server._trace_finalize(trace, status="failed", error=str(exc))
            server._trace_log_request(
                trace,
                request_type="recommend",
                user_scope_id=req.user_scope_id or "guest",
            )
            raise HTTPException(status_code=500, detail=str(exc))

    def recommended_artists(self, req):
        return recommended_artists_runtime(self._server, req)

    def interaction_event(self, req):
        return interaction_event_runtime(self._server, req)

    def search_interaction(self, req):
        return search_interaction_runtime(self._server, req)

    def model_status(self):
        return model_status_runtime(self._server)

    def model_versions(self):
        return model_versions_runtime(self._server)

    def experiments(self, *, window_hours: int):
        return experiments_runtime(self._server, window_hours=window_hours)

    def evaluate_experiments(self, *, force_promote: bool, window_hours: int):
        return evaluate_experiments_runtime(
            self._server,
            force_promote=force_promote,
            window_hours=window_hours,
        )

    def train_model(self, req):
        return train_model_runtime(self._server, req)

    def model_registry_versions(self, *, model_key: str, limit: int = 20):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        return {
            "status": "success",
            "model_key": normalized_key,
            "versions": list_model_versions(
                model_key=normalized_key,
                limit=limit,
            ),
        }

    def model_registry_activate(
        self,
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        normalized_version = self._server._recommendation_trim_text(version)
        if not normalized_key or not normalized_version:
            raise HTTPException(status_code=400, detail="model_key and version are required")
        ok = activate_model_version(
            model_key=normalized_key,
            version=normalized_version,
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not ok:
            raise HTTPException(status_code=404, detail="model version not found")
        return {
            "status": "success",
            "model_key": normalized_key,
            "active_version": normalized_version,
        }

    def model_registry_rollback(
        self,
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
    ):
        normalized_key = self._server._recommendation_trim_text(model_key)
        if not normalized_key:
            raise HTTPException(status_code=400, detail="model_key is required")
        result = rollback_model_version(
            model_key=normalized_key,
            target_version=self._server._recommendation_trim_text(target_version),
            actor=self._server._recommendation_trim_text(actor) or "system",
            reason=self._server._recommendation_trim_text(reason),
            metadata={"source": "api"},
        )
        if not bool((result or {}).get("ok")):
            raise HTTPException(
                status_code=400,
                detail=f"rollback failed: {(result or {}).get('reason') or 'unknown'}",
            )
        return {
            "status": "success",
            "model_key": normalized_key,
            "rollback": result,
        }

    def model_rollout_events(self, *, model_key: str = "", limit: int = 50):
        normalized_key = self._server._recommendation_trim_text(model_key)
        return {
            "status": "success",
            "model_key": normalized_key,
            "events": list_rollout_events(
                model_key=normalized_key,
                limit=limit,
            ),
        }
