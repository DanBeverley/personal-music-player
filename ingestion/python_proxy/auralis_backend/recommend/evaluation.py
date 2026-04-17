from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from ..domain.catalog import canonical_artist_identity, canonical_title_artist_identity


def _visible_items(
    rows: Sequence[Dict[str, Any]] | None,
    *,
    visible_items_per_row: int,
) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        for item in list(row.get("items") or [])[: max(int(visible_items_per_row or 0), 1)]:
            if isinstance(item, dict):
                visible.append(dict(item))
    return visible


def _supported_languages(profile: Dict[str, Any]) -> set[str]:
    values = profile.get("supported_languages") or []
    if isinstance(values, str):
        values = [values]
    normalized = {
        str(value or "").strip().lower()
        for value in values
        if str(value or "").strip()
    }
    dominant = str(profile.get("dominant_language") or "").strip().lower()
    if dominant:
        normalized.add(dominant)
    return normalized


def _recent_track_keys(profile: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for track in list(profile.get("recent_track_snapshots") or []) + list(
        profile.get("last_played_tracks") or []
    ):
        if not isinstance(track, dict):
            continue
        key = canonical_title_artist_identity(track)
        if key:
            keys.add(key)
    return keys


def evaluate_home_fixture(
    *,
    profile: Dict[str, Any],
    rows: Sequence[Dict[str, Any]] | None,
    row_diagnostics: Dict[str, Dict[str, Any]] | None = None,
    visible_items_per_row: int = 6,
    required_rows: Iterable[str] = (),
) -> Dict[str, Any]:
    visible = _visible_items(rows, visible_items_per_row=visible_items_per_row)
    track_counts: Dict[str, int] = {}
    artist_counts: Dict[str, int] = {}
    language_mismatch_count = 0
    recent_repeat_count = 0
    supported_languages = _supported_languages(profile)
    recent_track_keys = _recent_track_keys(profile)

    for item in visible:
        track_key = canonical_title_artist_identity(item)
        if track_key:
            track_counts[track_key] = int(track_counts.get(track_key) or 0) + 1
            if track_key in recent_track_keys:
                recent_repeat_count += 1
        artist_key = canonical_artist_identity(
            {
                "id": item.get("artist_id"),
                "name": item.get("channel") or item.get("artist") or item.get("author"),
            }
        )
        if artist_key:
            artist_counts[artist_key] = int(artist_counts.get(artist_key) or 0) + 1
        language = str(item.get("language") or "").strip().lower()
        if language and supported_languages and language not in supported_languages:
            language_mismatch_count += 1

    emitted_rows = {
        str(row.get("kind") or "")
        for row in list(rows or [])
        if isinstance(row, dict) and str(row.get("kind") or "").strip()
    }
    if row_diagnostics:
        for row_kind, diagnostics in dict(row_diagnostics or {}).items():
            if str((diagnostics or {}).get("status") or "").strip().lower() == "emitted":
                emitted_rows.add(str(row_kind or ""))

    missing_required_rows = [
        row_kind for row_kind in list(required_rows or []) if str(row_kind or "") not in emitted_rows
    ]
    duplicate_track_count = sum(max(count - 1, 0) for count in track_counts.values())
    max_artist_concentration = max(artist_counts.values(), default=0)
    emitted_row_count = len(emitted_rows)

    reasons: List[str] = []
    if duplicate_track_count > 0:
        reasons.append(f"visible_duplicate_tracks:{duplicate_track_count}")
    if max_artist_concentration >= 4:
        reasons.append(f"visible_artist_concentration:{max_artist_concentration}")
    if language_mismatch_count > 0:
        reasons.append(f"off_profile_language_items:{language_mismatch_count}")
    if recent_repeat_count > 0:
        reasons.append(f"recent_repeat_items:{recent_repeat_count}")
    if missing_required_rows:
        reasons.append(f"missing_required_rows:{','.join(missing_required_rows)}")

    return {
        "visible_item_count": len(visible),
        "emitted_row_count": emitted_row_count,
        "duplicate_track_count": duplicate_track_count,
        "unique_artist_count": len(artist_counts),
        "max_artist_concentration": max_artist_concentration,
        "off_profile_language_count": language_mismatch_count,
        "recent_repeat_count": recent_repeat_count,
        "missing_required_rows": missing_required_rows,
        "reasons": reasons,
    }
