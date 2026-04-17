from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .server_adapter import adapt_domain_server
from ..search.runtime import search_artist_seed_tracks


def trim_text(value: Optional[str]) -> str:
    return adapt_domain_server().trim_text(value)


def track_signature(track: Optional[Dict[str, Any]], *, server: Any | None = None) -> str:
    server = adapt_domain_server(server)
    return server.recommendation_track_signature(track)


def anchor_query(track: Optional[Dict[str, Any]], *, include_album: bool = False) -> str:
    if not isinstance(track, dict):
        return ""
    parts = [
        trim_text(track.get("title")),
        trim_text(track.get("channel") or track.get("author") or track.get("artist")),
    ]
    if include_album:
        parts.append(trim_text(track.get("album")))
    return " ".join([part for part in parts if part]).strip()


def album_candidates_for_track(
    track: Optional[Dict[str, Any]],
    *,
    limit: int = 2,
    include_search: bool = True,
    server: Any | None = None,
) -> List[Dict[str, Any]]:
    server = adapt_domain_server(server)
    if not isinstance(track, dict):
        return []
    albums: List[Dict[str, Any]] = []
    seen = set()

    def add_album(raw_album: Optional[Dict[str, Any]]) -> None:
        if not isinstance(raw_album, dict):
            return
        album_id = trim_text(raw_album.get("id"))
        title = trim_text(raw_album.get("title"))
        artist = trim_text(raw_album.get("artist"))
        key = album_id or f"{server.normalize_text(title)}|{server.normalize_text(artist)}"
        if not key or key in seen:
            return
        seen.add(key)
        albums.append(raw_album)

    album_id = trim_text(track.get("album_id"))
    album_title = trim_text(track.get("album"))
    if album_id:
        add_album(
            {
                "id": album_id,
                "title": album_title or "Unknown Album",
                "artist": track.get("channel") or track.get("artist"),
                "thumbnail": track.get("thumbnail"),
            }
        )
    elif album_title:
        add_album(
            {
                "id": None,
                "title": album_title,
                "artist": track.get("channel") or track.get("artist"),
                "thumbnail": track.get("thumbnail"),
            }
        )

    search_query = anchor_query(track, include_album=True)
    if include_search and search_query:
        for album in server.assistant_tool_search_albums(search_query, max(limit * 2, 4)):
            add_album(album)
            if len(albums) >= limit:
                break
    return albums[:limit]


def candidate_sources_for_track(
    track: Optional[Dict[str, Any]],
    *,
    server: Any | None = None,
) -> List[Tuple[str, List[Dict[str, Any]], float]]:
    server = adapt_domain_server(server)
    if not isinstance(track, dict):
        return []
    executor = getattr(server, "recommendation_row_executor", None) or getattr(
        server,
        "recommendation_executor",
    )
    track_id = trim_text(track.get("id"))
    artist_name = trim_text(track.get("channel") or track.get("author") or track.get("artist"))
    futures = {}
    if track_id:
        futures["similar"] = executor.submit(
            server.assistant_tool_get_similar_tracks,
            track_id,
            12,
        )
        futures["collaborative"] = executor.submit(
            server.recommendation_collaborative_neighbor_tracks,
            track_id,
            10,
        )
    if artist_name:
        futures["artist_seed"] = executor.submit(
            search_artist_seed_tracks,
            artist_name,
            8,
            server=server,
        )

    def fetch_album_context() -> List[Dict[str, Any]]:
        album_tracks: List[Dict[str, Any]] = []
        for album in album_candidates_for_track(track, limit=2, server=server):
            album_id = trim_text(album.get("id"))
            if not album_id:
                continue
            album_details = server.assistant_tool_get_album_details(album_id)
            album_tracks.extend(album_details.get("tracks") or [])
        return album_tracks

    futures["album_context"] = executor.submit(fetch_album_context)
    source_results = {
        "similar": [],
        "collaborative": [],
        "artist_seed": [],
        "album_context": [],
    }
    for source_name, future in futures.items():
        try:
            source_results[source_name] = future.result(timeout=12) or []
        except Exception:
            source_results[source_name] = []
    return [
        ("similar", source_results["similar"], 4.8),
        ("collaborative", source_results["collaborative"], 4.6),
        ("artist_seed", source_results["artist_seed"], 3.7),
        ("album_context", source_results["album_context"], 3.5),
    ]
