from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..domain.catalog import canonical_artist_identity, canonical_title_artist_identity


def row_status_name(diagnostics: Dict[str, Any] | None) -> str:
    if not isinstance(diagnostics, dict):
        return ""
    return str(diagnostics.get("status") or "").strip().lower()


def summarize_row_status(
    row_diagnostics: Dict[str, Dict[str, Any]] | None,
) -> Dict[str, str]:
    summary: Dict[str, str] = {}
    for row_kind, diagnostics in dict(row_diagnostics or {}).items():
        status = row_status_name(diagnostics)
        if status:
            summary[row_kind] = status
    return summary


def snapshot_quality_reasons(
    row_diagnostics: Dict[str, Dict[str, Any]] | None,
    *,
    critical_rows: Iterable[str] = (),
    min_emitted: int = 5,
    filtered_threshold: int = 4,
) -> List[str]:
    critical = {str(row_kind or "") for row_kind in critical_rows}
    diagnostics = row_diagnostics or {}
    reasons: List[str] = []
    emitted_count = 0
    filtered_count = 0
    for row_kind, diag in diagnostics.items():
        status = row_status_name(diag)
        if status == "emitted":
            emitted_count += 1
        if status in {"filtered_out", "finalize_filtered_out"}:
            filtered_count += 1
        if status.startswith("fallback_"):
            reasons.append(f"{row_kind}:{status}")
        elif row_kind in critical and status in {
            "filtered_out",
            "finalize_filtered_out",
            "empty",
            "seed_pool_empty",
            "post_filter_empty",
            "missing_no_fallback",
            "fallback_unavailable",
        }:
            reasons.append(f"{row_kind}:{status}")
    if emitted_count < int(min_emitted):
        reasons.append(f"emitted_count:{emitted_count}")
    if filtered_count >= int(filtered_threshold):
        reasons.append(f"filtered_count:{filtered_count}")
    return reasons


def artifact_quality_score(
    row_status: Dict[str, str],
    quality_reasons: List[str],
) -> float:
    emitted = sum(1 for status in row_status.values() if status == "emitted")
    fallback = sum(1 for status in row_status.values() if status.startswith("fallback_"))
    filtered = sum(
        1
        for status in row_status.values()
        if status in {"filtered_out", "finalize_filtered_out"}
    )
    missing = sum(
        1
        for status in row_status.values()
        if status in {
            "missing_no_fallback",
            "fallback_unavailable",
            "seed_pool_empty",
            "post_filter_empty",
        }
    )
    score = 1.0
    score += emitted * 0.08
    score -= fallback * 0.18
    score -= filtered * 0.1
    score -= missing * 0.2
    score -= len(quality_reasons) * 0.12
    return round(max(0.0, min(score, 1.0)), 4)


def artifact_repetition_reasons(
    rows: Sequence[Dict[str, Any]] | None,
    *,
    visible_items_per_row: int = 6,
    max_visible_same_artist: int = 4,
) -> List[str]:
    reasons: List[str] = []
    visible_track_keys: set[str] = set()
    duplicate_tracks = 0
    artist_counts: Dict[str, int] = {}
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        for item in list(row.get("items") or [])[:max(int(visible_items_per_row or 0), 1)]:
            if not isinstance(item, dict):
                continue
            track_key = canonical_title_artist_identity(item)
            if not track_key:
                track_key = str(item.get("id") or "").strip()
            if track_key:
                if track_key in visible_track_keys:
                    duplicate_tracks += 1
                else:
                    visible_track_keys.add(track_key)
            artist_key = canonical_artist_identity(
                {
                    "id": item.get("artist_id"),
                    "name": item.get("channel") or item.get("artist") or item.get("author"),
                }
            )
            if artist_key:
                artist_counts[artist_key] = int(artist_counts.get(artist_key) or 0) + 1
    if duplicate_tracks > 0:
        reasons.append(f"visible_duplicate_tracks:{duplicate_tracks}")
    if artist_counts:
        max_artist_count = max(artist_counts.values())
        if max_artist_count >= int(max_visible_same_artist):
            reasons.append(f"visible_artist_concentration:{max_artist_count}")
    return reasons


def split_rows_by_kind(
    rows: Sequence[Dict[str, Any]] | None,
    *,
    heavy_row_kinds: Iterable[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    heavy_kinds = {str(row_kind or "") for row_kind in heavy_row_kinds}
    launch_rows: List[Dict[str, Any]] = []
    heavy_rows: List[Dict[str, Any]] = []
    for row in list(rows or []):
        row_copy = dict(row or {})
        if str(row_copy.get("kind") or "") in heavy_kinds:
            heavy_rows.append(row_copy)
        else:
            launch_rows.append(row_copy)
    return launch_rows, heavy_rows


def promote_artifact_status(
    row_status: Dict[str, str],
    launch_rows: Sequence[Dict[str, Any]] | None,
    quality_reasons: List[str],
    *,
    primary_row_kinds: Iterable[str],
    builder_mode: str = "",
) -> str:
    if not list(launch_rows or []):
        return "rejected"
    if "thin_core" in str(builder_mode or ""):
        # Thin-core launch results are allowed as emergency first-paint feeds,
        # but they must not become reusable launch artifacts across sessions.
        return "rejected"
    primary_ok = all(
        row_status.get(str(row_kind or "")) == "emitted"
        for row_kind in primary_row_kinds
    )
    if primary_ok and not quality_reasons:
        return "promoted"
    minimal_ok = (
        row_status.get("continue_listening") == "emitted"
        and row_status.get("because_you_played") == "emitted"
        and len(list(launch_rows or [])) >= 4
    )
    if minimal_ok:
        return "usable"
    return "rejected"


def acceptable_launch_artifact(
    row_status: Dict[str, str],
    launch_rows: Sequence[Dict[str, Any]] | None,
    quality_reasons: List[str],
    *,
    builder_mode: str = "",
) -> bool:
    rows = list(launch_rows or [])
    if "thin_core" in str(builder_mode or ""):
        return False
    if len(rows) < 5:
        return False
    if row_status.get("continue_listening") != "emitted":
        return False
    if row_status.get("because_you_played") not in {"emitted", "fallback_emitted"}:
        return False

    supportive_rows = (
        "trending_for_you",
        "quiet_picks",
        "rediscover",
        "deep_cuts",
    )
    supportive_ready = sum(
        1
        for row_kind in supportive_rows
        if row_status.get(row_kind) in {"emitted", "fallback_emitted"}
    )
    if supportive_ready < 2:
        return False

    hard_missing = {
        "missing_no_fallback",
        "fallback_unavailable",
        "empty",
        "seed_pool_empty",
        "post_filter_empty",
        "finalize_filtered_out",
    }
    if (
        row_status.get("trending_for_you") in hard_missing
        and row_status.get("quiet_picks") in hard_missing
    ):
        return False

    return artifact_quality_score(row_status, quality_reasons) >= 0.24
