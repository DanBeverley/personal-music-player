from __future__ import annotations

from typing import Any, Dict


def track_signal_count(profile: Dict[str, Any]) -> int:
    ids = set()
    for key in ("recent_track_ids", "top_track_ids"):
        for value in list(profile.get(key) or []):
            normalized = str(value or "").strip()
            if normalized:
                ids.add(normalized)
    for key in ("last_played_tracks", "recent_track_snapshots", "top_track_snapshots"):
        for track in list(profile.get(key) or []):
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("id") or track.get("videoId") or "").strip()
            title = str(track.get("title") or "").strip().lower()
            artist = str(
                track.get("channel") or track.get("artist") or track.get("author") or ""
            ).strip().lower()
            signal_key = track_id or (f"{title}|{artist}" if title or artist else "")
            if signal_key:
                ids.add(signal_key)
    return len(ids)


def artist_signal_count(profile: Dict[str, Any]) -> int:
    artists = set()
    for key in ("top_artists", "artist_hints", "listened_artists"):
        for value in list(profile.get(key) or []):
            normalized = str(value or "").strip().lower()
            if normalized:
                artists.add(normalized)
    return len(artists)


def query_signal_count(profile: Dict[str, Any]) -> int:
    return len(
        {
            str(value or "").strip().lower()
            for value in list(profile.get("recent_queries") or [])
            if str(value or "").strip()
        }
    )


def profile_signal_tier(profile: Dict[str, Any]) -> str:
    track_count = track_signal_count(profile)
    artist_count = artist_signal_count(profile)
    query_count = query_signal_count(profile)
    if track_count <= 0 and artist_count <= 0 and query_count <= 0:
        return "cold_start"
    if track_count < 3 and artist_count < 2:
        return "sparse"
    return "personalized"
