from __future__ import annotations

from typing import Any, Dict, List

from .row_registry import default_required_row_kinds, ordered_row_kinds, row_title_template


def row_kinds() -> List[str]:
    return ordered_row_kinds()


def row_title(row_kind: str, profile: Dict[str, Any]) -> str:
    if row_kind != "because_you_played":
        return row_title_template(row_kind)
    anchor_track = (
        (profile.get("last_played_tracks") or [None])[0]
        or (profile.get("recent_track_snapshots") or [None])[0]
    )
    anchor_artist = ""
    anchor_title = ""
    if isinstance(anchor_track, dict):
        anchor_artist = (
            (anchor_track.get("channel") or "")
            or (anchor_track.get("artist") or "")
            or (anchor_track.get("author") or "")
        ).strip()
        anchor_title = ((anchor_track.get("title") or "")).strip()
    if anchor_artist:
        return f"Because you played {anchor_artist}"
    if anchor_title:
        return f"Because you played {anchor_title}"
    return row_title_template("because_you_played")


def required_row_kinds(server: Any) -> List[str]:
    configured = [
        str(item).strip()
        for item in (getattr(server, "RECOMMENDATION_REQUIRED_ROWS", ()) or ())
        if str(item).strip()
    ]
    if not configured:
        configured = default_required_row_kinds()
    valid = set(ordered_row_kinds())
    return [row_kind for row_kind in configured if row_kind in valid]


def is_required_row(server: Any, row_kind: str) -> bool:
    return row_kind in set(required_row_kinds(server))


def apply_required_row_fallback_policy(row_seed: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(row_seed or {})
    if updated.get("item_type") == "album":
        updated["row_strategy"] = "fallback"
        updated["fallback_reason"] = "required_row_missing"
        return updated
    updated["row_strategy"] = (
        "hybrid"
        if updated.get("row_strategy") == "personalized"
        else updated.get("row_strategy") or "fallback"
    )
    updated["fallback_reason"] = "required_row_missing"
    return updated


def should_extend_row(row: Dict[str, Any] | None, offset: int, page_size: int) -> bool:
    if not isinstance(row, dict):
        return False
    if not row.get("can_extend"):
        return False
    total_items = len(row.get("items") or [])
    if total_items <= 0:
        return False
    requested_end = max(int(offset or 0), 0) + max(1, min(page_size, 12))
    prefetch_window = max(page_size * 3, 18)
    return requested_end >= max(total_items - prefetch_window, 0)
