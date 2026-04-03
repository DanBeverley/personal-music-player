from __future__ import annotations

from typing import Any, Dict, List, Optional


def get_server():
    import server

    return server


def build_search_request(**kwargs):
    server = get_server()
    return server.SearchRequest(**kwargs)


def build_track_snapshot(raw_track: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_track, dict):
        return None
    server = get_server()
    normalized = server.normalize_recommendation_track(dict(raw_track))
    if normalized is None:
        return None
    return dict(normalized)


def unique_snapshot_tracks(values, limit: int = 16) -> List[Dict[str, Any]]:
    server = get_server()
    return [
        dict(track)
        for track in server._recommendation_unique_snapshot_tracks(values or [], limit)
    ]


def trim_text(value: Optional[str]) -> str:
    return get_server()._recommendation_trim_text(value)


def unique_strings(values, limit: Optional[int] = None) -> List[str]:
    return list(get_server()._recommendation_unique_strings(values or [], limit))
