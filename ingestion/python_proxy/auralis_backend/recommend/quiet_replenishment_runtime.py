from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .pool_runtime import (
    _post_filter_row_candidates,
    _track_list_to_candidates,
    _trim_candidate_pool,
)
from .source_runtime import _recommendation_home_fallback_tracks


def quiet_replenishment_query_plan(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    limit: int | None = None,
) -> List[str]:
    normalized_used_queries = {
        server._recommendation_trim_text(query).lower()
        for query in (row.get("used_queries") or [])
        if server._recommendation_trim_text(query)
    }
    candidate_queries: List[str] = []

    def _add_query(value: str) -> None:
        normalized = server._recommendation_trim_text(value)
        if not normalized:
            return
        lowered = normalized.lower()
        if lowered in normalized_used_queries:
            return
        if normalized in candidate_queries:
            return
        candidate_queries.append(normalized)

    def _track_artist(track: Dict[str, Any] | None) -> str:
        if not isinstance(track, dict):
            return ""
        return server._recommendation_trim_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )

    def _track_album(track: Dict[str, Any] | None) -> str:
        if not isinstance(track, dict):
            return ""
        return server._recommendation_trim_text(
            track.get("album_title") or track.get("album") or ""
        )

    def _track_title(track: Dict[str, Any] | None) -> str:
        if not isinstance(track, dict):
            return ""
        return server._recommendation_trim_text(track.get("title") or track.get("name") or "")

    seed_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(row.get("items") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        24,
    )

    _add_query(server._recommendation_trim_text(row.get("base_query") or ""))
    for artist_hint in server._recommendation_unique_strings(
        [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
            *[
                _track_artist(track)
                for track in seed_tracks
                if isinstance(track, dict)
            ],
        ],
        16,
    ):
        _add_query(artist_hint)
    for album_hint in server._recommendation_unique_strings(
        [
            *(profile.get("top_albums") or []),
            *(profile.get("album_hints") or []),
            *[
                _track_album(track)
                for track in seed_tracks
                if isinstance(track, dict)
            ],
        ],
        12,
    ):
        _add_query(album_hint)
    for track in seed_tracks[:18]:
        artist_name = _track_artist(track)
        title = _track_title(track)
        album = _track_album(track)
        if title and artist_name:
            _add_query(f"{title} {artist_name}")
        elif title:
            _add_query(title)
        if album and artist_name:
            _add_query(f"{album} {artist_name}")
    for query_hint in server._recommendation_unique_strings(
        list(profile.get("recent_queries") or []),
        8,
    ):
        _add_query(query_hint)

    base_variants = list(candidate_queries)
    for query in base_variants[:12]:
        lowered = query.lower()
        if " acoustic" not in lowered:
            _add_query(f"{query} acoustic")
        if " unplugged" not in lowered:
            _add_query(f"{query} unplugged")
        if " live acoustic" not in lowered and " live" not in lowered:
            _add_query(f"{query} live acoustic")
        if " stripped" not in lowered:
            _add_query(f"{query} stripped")
        if " session" not in lowered:
            _add_query(f"{query} session")

    if isinstance(limit, int) and limit > 0:
        return candidate_queries[:limit]
    return candidate_queries


def quiet_replenishment_candidates(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    page_size: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    candidate_queries = quiet_replenishment_query_plan(
        server=server,
        row=row,
        profile=profile,
        limit=28,
    )

    replenishment_tracks: List[Dict[str, Any]] = []
    used_queries: List[str] = []
    per_query_limit = max(page_size * 5, 36)
    for query in candidate_queries:
        try:
            result_tracks = server._assistant_tool_search_tracks(query, per_query_limit)
        except Exception:
            result_tracks = []
        if result_tracks:
            replenishment_tracks.extend(
                track
                for track in result_tracks
                if isinstance(track, dict)
            )
            used_queries.append(query)
        if len(replenishment_tracks) >= max(page_size * 24, 144):
            break

    if len(replenishment_tracks) < max(page_size * 3, 18):
        replenishment_tracks.extend(
            _recommendation_home_fallback_tracks(
                profile,
                limit=max(page_size * 6, 30),
                allow_catalog_fallback=False,
                server=server,
            )
        )

    replenishment_candidates = _track_list_to_candidates(
        server,
        replenishment_tracks,
        generator_name="quiet_extension_replenishment",
        base_score=2.55,
        reason="Additional quiet picks loaded while you keep scrolling.",
    )
    replenishment_candidates = _post_filter_row_candidates(
        server,
        "quiet_picks",
        profile,
        replenishment_candidates,
    )
    return (
        _trim_candidate_pool(
            server,
            replenishment_candidates,
            limit=max(page_size * 16, 96),
        ),
        used_queries,
    )
