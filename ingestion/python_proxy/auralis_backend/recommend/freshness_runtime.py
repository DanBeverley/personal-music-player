from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Iterable, List, Sequence

from .store_runtime import open_recommendation_store_connection


_HOME_ROW_REPEAT_LOOKBACK_SECONDS = 604800
_HOME_ROW_REPEAT_LIMIT = 24

_ROW_FRESHNESS_CONFIG: Dict[str, Dict[str, float | int]] = {
    "because_you_played": {
        "impression_penalty": 3.1,
        "recent_track_penalty": 0.35,
        "rotation_bonus": 0.22,
        "cadence_seconds": 7200,
    },
    "trending_for_you": {
        "impression_penalty": 3.6,
        "recent_track_penalty": 0.3,
        "rotation_bonus": 0.26,
        "cadence_seconds": 5400,
    },
    "quiet_picks": {
        "impression_penalty": 3.8,
        "recent_track_penalty": 0.25,
        "rotation_bonus": 0.24,
        "cadence_seconds": 5400,
    },
    "deep_cuts": {
        "impression_penalty": 3.7,
        "recent_track_penalty": 0.2,
        "rotation_bonus": 0.28,
        "cadence_seconds": 3600,
    },
    "rediscover": {
        "impression_penalty": 3.9,
        "recent_track_penalty": 0.2,
        "rotation_bonus": 0.3,
        "cadence_seconds": 3600,
    },
}


def recent_row_impression_track_ids(
    server: Any,
    profile: Dict[str, Any],
    row_kind: str,
    *,
    limit: int = _HOME_ROW_REPEAT_LIMIT,
    lookback_seconds: int = _HOME_ROW_REPEAT_LOOKBACK_SECONDS,
) -> set[str]:
    cache = profile.setdefault("_recent_row_impression_cache", {})
    if isinstance(cache, dict) and row_kind in cache:
        return set(cache.get(row_kind) or [])
    user_scope_id = server._assistant_safe_scope_id(profile.get("user_scope_id") or "guest")
    cutoff = time.time() - max(int(lookback_seconds or 0), 0)
    track_ids: set[str] = set()
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        connection = None
    if connection is None:
        if isinstance(cache, dict):
            cache[row_kind] = []
        return track_ids
    try:
        rows = connection.execute(
            """
            SELECT track_id
            FROM recommendation_impressions
            WHERE user_scope_id = ? AND row_id = ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [
                user_scope_id,
                row_kind,
                float(cutoff),
                max(int(limit or 0), 1),
            ],
        ).fetchall()
        for row in rows:
            track_id = server._recommendation_trim_text(row["track_id"])
            if track_id:
                track_ids.add(track_id)
    except Exception:
        track_ids = set()
    finally:
        connection.close()
    if isinstance(cache, dict):
        cache[row_kind] = list(track_ids)
    return track_ids


def _track_artist_key(server: Any, track: Dict[str, Any]) -> str:
    return server._normalize_text(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    )


def _rotation_jitter(
    *,
    user_scope_id: str,
    row_kind: str,
    item_id: str,
    cadence_seconds: int,
    refresh_token: str = "",
) -> float:
    if cadence_seconds <= 0:
        return 0.0
    bucket = int(time.time() // cadence_seconds)
    digest = hashlib.sha1(
        f"{user_scope_id}|{row_kind}|{bucket}|{refresh_token}|{item_id}".encode("utf-8")
    ).hexdigest()
    return (int(digest[:8], 16) % 1000) / 1000.0


def freshen_launch_row(
    server: Any,
    profile: Dict[str, Any],
    row: Dict[str, Any],
    *,
    aggressive_refresh: bool = False,
    refresh_token: str = "",
) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return row
    row_kind = str(row.get("kind") or "")
    config = _ROW_FRESHNESS_CONFIG.get(row_kind)
    items = [dict(item) for item in list(row.get("items") or []) if isinstance(item, dict)]
    if not config or len(items) <= 1:
        return dict(row)

    recent_row_track_ids = recent_row_impression_track_ids(server, profile, row_kind)
    recent_track_ids = {
        server._recommendation_trim_text(track_id)
        for track_id in list(profile.get("recent_track_ids") or [])
        if server._recommendation_trim_text(track_id)
    }
    user_scope_id = server._assistant_safe_scope_id(profile.get("user_scope_id") or "guest")
    impression_penalty = float(config.get("impression_penalty") or 0.0)
    recent_track_penalty = float(config.get("recent_track_penalty") or 0.0)
    rotation_bonus = float(config.get("rotation_bonus") or 0.0)
    cadence_seconds = max(int(config.get("cadence_seconds") or 0), 0)
    refresh_token = server._recommendation_trim_text(refresh_token)
    if aggressive_refresh:
        impression_penalty *= 1.45
        recent_track_penalty *= 1.2
        rotation_bonus *= 1.8
        cadence_seconds = min(cadence_seconds or 900, 900)

    ranked: List[tuple[float, int, Dict[str, Any]]] = []
    for index, item in enumerate(items):
        track_id = server._recommendation_trim_text(item.get("id"))
        base_score = float(item.get("generator_score") or 0.0)
        if base_score <= 0.0:
            base_score = max(len(items) - index, 1) * 0.1
        score = base_score - (index * 0.025)
        if track_id and track_id in recent_row_track_ids:
            score -= impression_penalty
        if track_id and track_id in recent_track_ids:
            score -= recent_track_penalty
        if track_id:
            score += _rotation_jitter(
                user_scope_id=user_scope_id,
                row_kind=row_kind,
                item_id=track_id,
                cadence_seconds=cadence_seconds,
                refresh_token=refresh_token,
            ) * rotation_bonus
        if aggressive_refresh:
            score -= index * 0.06
        ranked.append((score, index, item))
    ranked.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)

    ordered: List[Dict[str, Any]] = []
    deferred: List[tuple[float, int, Dict[str, Any]]] = []
    artist_counts: Dict[str, int] = {}
    for score, index, item in ranked:
        artist_key = _track_artist_key(server, item)
        if artist_key and artist_counts.get(artist_key, 0) >= 1 and len(ordered) < min(6, len(items)):
            deferred.append((score, index, item))
            continue
        ordered.append(item)
        if artist_key:
            artist_counts[artist_key] = int(artist_counts.get(artist_key) or 0) + 1
    ordered.extend(item for _score, _index, item in deferred)

    updated = dict(row)
    updated["items"] = ordered
    return updated


def freshen_launch_rows(
    server: Any,
    profile: Dict[str, Any],
    rows: Sequence[Dict[str, Any]] | None,
    *,
    aggressive_refresh: bool = False,
    refresh_token: str = "",
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for row in list(rows or []):
        output.append(
            freshen_launch_row(
                server,
                profile,
                row,
                aggressive_refresh=aggressive_refresh,
                refresh_token=refresh_token,
            )
        )
    return output


def visible_impression_rows(
    rows: Iterable[Dict[str, Any]] | None,
    *,
    page_size: int,
) -> List[Dict[str, Any]]:
    visible_rows: List[Dict[str, Any]] = []
    limit = max(int(page_size or 0), 1)
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        item_type = str(row.get("item_type") or "track")
        if item_type != "track":
            visible_rows.append(dict(row))
            continue
        row_copy = dict(row)
        row_copy["items"] = list(row.get("items") or [])[:limit]
        visible_rows.append(row_copy)
    return visible_rows
