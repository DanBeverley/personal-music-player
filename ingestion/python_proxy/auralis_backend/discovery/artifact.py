from __future__ import annotations

from typing import Any, Dict, List
from .adapters import row_to_storage_payload
from .config import (
    ARTIFACT_VERSION,
    POPULAR_RADIO_CARD_MIN_TRACKS,
    ROW_ORDER,
    ROW_RECIPES,
)
from .schema import DiscoveryArtifact, DiscoveryRow, TasteProfile


ROW_QUALITY_WARNING_REASONS = {
    "missing_artist_or_quiet_picks",
    "recommended_albums_listened_only",
}

ARTIFACT_FATAL_REASONS = {
    "below_min_accepted_rows",
    "weak_two_row_feed",
    "missing_personalized_core",
    "source_quality_violation",
    "missing_made_for_you",
    "missing_quiet_picks",
    "quiet_picks_below_launch_count",
    "missing_required_rows",
}

OPTIONAL_COMPLETE_ROWS = {
    "featured_new_albums",
    "recommended_albums",
    "popular_radio",
}

ROW_REPLENISHMENT_DOMAINS = {
    "todays_pick": "todays_pick",
    "made_for_you": "made_for_you_tracks",
    "because_you_played": "because_you_played",
    "recommended_artists": "recommended_artists",
    "quiet_picks": "quiet_picks",
}

def _hard_quality_reasons(reasons: List[str] | None) -> List[str]:
    return [
        reason
        for reason in list(reasons or [])
        if reason in ARTIFACT_FATAL_REASONS or str(reason or "").endswith(":empty")
    ]


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _listened_album_keys(taste: TasteProfile) -> set[str]:
    keys = {_normalized(value) for value in [*taste.album_hints, *taste.top_albums]}
    for track in (
        taste.recent_tracks
        + taste.top_tracks
        + taste.last_played_tracks
        + taste.anchor_tracks
    ):
        album = track.get("album")
        if isinstance(album, dict):
            album = album.get("title") or album.get("name")
        key = _normalized(album)
        if key:
            keys.add(key)
    keys.discard("")
    return keys


def _row_by_kind(rows: List[DiscoveryRow], kind: str) -> DiscoveryRow | None:
    return next((row for row in rows or [] if row.kind == kind), None)


def _applicable_row_kinds(taste: TasteProfile) -> List[str]:
    return [
        kind
        for kind in ROW_ORDER
        if kind not in OPTIONAL_COMPLETE_ROWS
        if not (
            kind == "last_played"
            and len(taste.last_played_tracks) < ROW_RECIPES[kind].min_items
        )
        and not (
            kind == "frequently_listened"
            and len(taste.frequent_tracks) < ROW_RECIPES[kind].min_items
        )
        and not (
            kind == "because_you_played"
            and not (
                taste.full_history_tracks
                or taste.recent_tracks
                or taste.anchor_tracks
            )
        )
    ]


def row_shortage_domains(
    *,
    rows: List[DiscoveryRow],
    taste: TasteProfile,
) -> List[str]:
    row_counts = {row.kind: len(row.items or []) for row in rows or []}
    shortages: List[str] = []
    for kind in _applicable_row_kinds(taste):
        if row_counts.get(kind, 0) >= ROW_RECIPES[kind].min_items:
            continue
        domain = ROW_REPLENISHMENT_DOMAINS.get(kind)
        if domain:
            shortages.append(domain)
    return shortages


def row_contract_report(
    *,
    rows: List[DiscoveryRow],
    taste: TasteProfile,
    home_tab_diagnostics: Dict[str, Any],
) -> Dict[str, Any]:
    row_ids = [row.kind for row in rows or []]
    row_counts = {row.kind: len(row.items or []) for row in rows or []}
    applicable_rows = _applicable_row_kinds(taste)
    contracts: Dict[str, bool] = {
        "min_rows": all(kind in row_ids for kind in applicable_rows),
        "made_for_you": row_counts.get("made_for_you", 0) >= ROW_RECIPES["made_for_you"].min_items,
        "album_feature": all(
            row_counts.get(kind, 0) == 0
            or row_counts.get(kind, 0) >= ROW_RECIPES[kind].min_items
            for kind in ("featured_new_albums", "recommended_albums")
        ),
        "popular_radio": row_counts.get("popular_radio", 0) == 0
        or row_counts.get("popular_radio", 0) >= ROW_RECIPES["popular_radio"].min_items,
        "popular_radio_card_depth": all(
            len(card.get("tracks") or card.get("items") or [])
            >= POPULAR_RADIO_CARD_MIN_TRACKS
            for row in rows or []
            if row.kind == "popular_radio"
            for card in row.items or []
            if isinstance(card, dict)
        ) if row_counts.get("popular_radio", 0) else True,
        "quiet_picks": row_counts.get("quiet_picks", 0) >= 20,
        "not_two_row_feed": not (row_ids == ["last_played", "frequently_listened"] or len(row_ids) <= 2),
        "complete_home_rows": all(
            row_counts.get(kind, 0) >= ROW_RECIPES[kind].min_items
            for kind in applicable_rows
        ),
    }
    recommended_albums = _row_by_kind(rows, "recommended_albums")
    listened_only = False
    if recommended_albums is not None and recommended_albums.items:
        listened_albums = _listened_album_keys(taste)
        album_titles = {
            _normalized(item.get("title") or item.get("album"))
            for item in recommended_albums.items
            if isinstance(item, dict)
        }
        album_titles.discard("")
        listened_only = bool(album_titles and album_titles.issubset(listened_albums))
    contracts["recommended_albums_not_listened_only"] = not listened_only

    source_violations: List[str] = []
    for row in rows or []:
        for item in row.items or []:
            authority = _normalized(item.get("source_authority"))
            if authority == "search_only":
                source_violations.append(row.kind)
                break
    contracts["source_safety"] = not source_violations

    failed = [name for name, passed in contracts.items() if not passed]
    passed_count = len(contracts) - len(failed)
    return {
        "contracts": contracts,
        "failed_contracts": failed,
        "passed_contract_count": passed_count,
        "total_contract_count": len(contracts),
        "row_counts": row_counts,
        "row_order": row_ids,
        "source_violation_rows": sorted(set(source_violations)),
    }


def _artifact_from_dict(payload: Dict[str, Any] | None) -> DiscoveryArtifact | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("artifact_version") or "") != ARTIFACT_VERSION:
        return None
    rows: List[DiscoveryRow] = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            DiscoveryRow(
                id=str(row.get("id") or row.get("kind") or ""),
                title=str(row.get("title") or ""),
                kind=str(row.get("kind") or row.get("id") or ""),
                item_type=str(row.get("item_type") or "track"),
                row_style=str(row.get("row_style") or ""),
                items=[dict(item) for item in row.get("items") or [] if isinstance(item, dict)],
                meta=dict(row.get("meta") or {}),
                next_offset=int(row.get("next_offset") or 0),
                has_more=bool(row.get("has_more")),
            )
        )
    return DiscoveryArtifact(
        session_id=str(payload.get("session_id") or ""),
        user_scope_id=str(payload.get("user_scope_id") or "guest"),
        profile_key=str(payload.get("profile_key") or ""),
        generated_at=float(payload.get("generated_at") or 0.0),
        expires_at=float(payload.get("expires_at") or 0.0),
        rows=rows,
        diagnostics=dict(payload.get("diagnostics") or {}),
        candidate_pool_counts=dict(payload.get("candidate_pool_counts") or {}),
        provider_timings_ms=dict(payload.get("provider_timings_ms") or {}),
        home_tab_lanes=dict(payload.get("home_tab_lanes") or {}),
        accepted=bool(payload.get("accepted")),
        quality_reasons=list(payload.get("quality_reasons") or []),
        artifact_source=str(payload.get("artifact_source") or "cache"),
    )


def artifact_to_dict(artifact: DiscoveryArtifact) -> Dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "session_id": artifact.session_id,
        "user_scope_id": artifact.user_scope_id,
        "profile_key": artifact.profile_key,
        "generated_at": artifact.generated_at,
        "expires_at": artifact.expires_at,
        "rows": [row_to_storage_payload(row) for row in artifact.rows],
        "diagnostics": dict(artifact.diagnostics or {}),
        "candidate_pool_counts": dict(artifact.candidate_pool_counts or {}),
        "provider_timings_ms": dict(artifact.provider_timings_ms or {}),
        "home_tab_lanes": dict(artifact.home_tab_lanes or {}),
        "accepted": bool(artifact.accepted),
        "quality_reasons": list(artifact.quality_reasons or []),
        "artifact_source": artifact.artifact_source,
    }




def evaluate_quality(
    *,
    rows: List[DiscoveryRow],
    taste: TasteProfile,
    home_tab_diagnostics: Dict[str, Any],
) -> tuple[bool, List[str], str]:
    contract_report = row_contract_report(
        rows=rows,
        taste=taste,
        home_tab_diagnostics=home_tab_diagnostics,
    )
    row_ids = [row.kind for row in rows or []]
    row_counts = {row.kind: len(row.items or []) for row in rows or []}
    reasons: List[str] = []
    contracts = dict(contract_report.get("contracts") or {})
    if not contracts.get("min_rows"):
        reasons.append("below_min_accepted_rows")
    if not contracts.get("complete_home_rows"):
        reasons.append("missing_required_rows")
    if not contracts.get("made_for_you"):
        reasons.append("missing_made_for_you")
    if "popular_radio" in row_ids:
        radio_row = next((row for row in rows if row.kind == "popular_radio"), None)
        if radio_row is None or len(radio_row.items or []) < 8:
            reasons.append("popular_radio_below_min_cards")
        elif not contracts.get("popular_radio_card_depth"):
            reasons.append("popular_radio_card_depth")
    quiet_picks = next((row for row in rows if row.kind == "quiet_picks"), None)
    if quiet_picks is None:
        reasons.append("missing_quiet_picks")
    elif len(quiet_picks.items or []) < 20:
        reasons.append("quiet_picks_below_launch_count")
    if "recommended_artists" not in row_ids and "quiet_picks" not in row_ids:
        reasons.append("missing_artist_or_quiet_picks")
    if not contracts.get("source_safety"):
        reasons.append("source_quality_violation")
    recommended_albums = next(
        (row for row in rows if row.kind == "recommended_albums"),
        None,
    )
    if recommended_albums is not None and recommended_albums.items:
        listened_albums = _listened_album_keys(taste)
        album_titles = {
            _normalized(item.get("title") or item.get("album"))
            for item in recommended_albums.items
            if isinstance(item, dict)
        }
        album_titles.discard("")
        if album_titles and album_titles.issubset(listened_albums):
            reasons.append("recommended_albums_listened_only")
    if row_ids == ["last_played", "frequently_listened"] or len(row_ids) <= 2:
        reasons.append("weak_two_row_feed")
    personalized_core_present = any(
        row_counts.get(kind, 0) > 0
        for kind in ("made_for_you", "because_you_played", "popular_radio", "quiet_picks")
    )
    if not personalized_core_present:
        reasons.append("missing_personalized_core")
    for row_kind in ROW_ORDER:
        if row_kind in row_ids and row_counts.get(row_kind, 0) <= 0:
            reasons.append(f"{row_kind}:empty")
    if not reasons:
        return True, [], "servable"
    if not _hard_quality_reasons(reasons):
        return True, reasons, "servable"
    return False, reasons, "build_failed"


def build_diagnostics(
    *,
    artifact_source: str,
    artifact_quality: str,
    row_status: Dict[str, Any],
    rows: List[DiscoveryRow],
    candidate_pool_counts: Dict[str, int],
    provider_timings_ms: Dict[str, int],
    home_tab_lanes: Dict[str, Dict[str, Any]],
    home_tab_diagnostics: Dict[str, Any],
    quality_reasons: List[str],
    elapsed_ms: int,
    taste: TasteProfile | None = None,
) -> Dict[str, Any]:
    breadth = {
        key.removeprefix("breadth_"): value
        for key, value in candidate_pool_counts.items()
        if key.startswith("breadth_")
    }
    radio_status = row_status.get("popular_radio") if isinstance(row_status, dict) else {}
    radio_diagnostics = (
        dict((radio_status or {}).get("diagnostics") or {})
        if isinstance(radio_status, dict)
        else {}
    )
    quiet_row = next((row for row in rows or [] if row.kind == "quiet_picks"), None)
    return {
        "engine": "discovery_engine",
        "artifact_source": artifact_source,
        "artifact_quality": artifact_quality,
        "artifact_status": artifact_quality,
        "artifact_failed_contracts": _hard_quality_reasons(quality_reasons),
        "row_status": row_status,
        "row_item_counts": {row.kind: len(row.items or []) for row in rows or []},
        "row_order": [row.kind for row in rows or []],
        "quality_warnings": [
            reason for reason in quality_reasons if reason in ROW_QUALITY_WARNING_REASONS
        ],
        "home_tab_lanes": home_tab_lanes,
        "home_tab_diagnostics": home_tab_diagnostics,
        "candidate_pool_counts": candidate_pool_counts,
        "candidate_breadth": breadth,
        "provider_timings_ms": provider_timings_ms,
        "row_reserve_counts": {
            row.kind: int((row.meta or {}).get("reserve_count") or 0)
            for row in rows or []
        },
        "popular_radio_summary": {
            "status": (radio_status or {}).get("status") if isinstance(radio_status, dict) else "",
            "reason": (radio_status or {}).get("reason") if isinstance(radio_status, dict) else "",
            "card_count": int((radio_status or {}).get("count") or 0) if isinstance(radio_status, dict) else 0,
            "candidate_artist_count": int(radio_diagnostics.get("candidate_artist_count") or 0),
            "radio_track_rejected": int(radio_diagnostics.get("radio_track_rejected") or 0),
        },
        "quiet_picks_summary": {
            "prepared_count": len(quiet_row.items or []) if quiet_row is not None else 0,
            "present": quiet_row is not None,
        },
        "pageable_rows": [row.kind for row in rows or [] if row.has_more],
        "quality_reasons": quality_reasons,
        "row_shortage_domains": (
            row_shortage_domains(rows=rows, taste=taste)
            if taste is not None
            else []
        ),
        "deferred_row_kinds": [],
        "deferred_rows_pending": False,
        "time_to_first_home_payload_ms": elapsed_ms,
        "total_build_ms": elapsed_ms,
    }
