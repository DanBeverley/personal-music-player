from __future__ import annotations

from typing import Any, Dict, List, Optional

from .server_adapter import adapt_search_server


class _QuietSearchLogger:
    def debug(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        return None

    def error(self, _message: str) -> None:
        return None

def _search_cache_key(server: Any, query: str, limit: int) -> str:
    server = adapt_search_server(server)
    normalized_query = server.normalize_text(query)
    return f"{normalized_query}|{max(int(limit or 0), 0)}"


def normalize_song_result(server: Any, entry: Optional[Dict[str, Any]]):
    server = adapt_search_server(server)
    if not entry:
        return None
    video_id = entry.get("videoId") or entry.get("video_id") or entry.get("id")
    if not video_id:
        return None
    album_info = server.extract_album_info(entry) or {}
    raw_artists = entry.get("artists")
    artist_entities: List[Dict[str, Any]] = []
    if isinstance(raw_artists, list):
        for raw_artist in raw_artists:
            if not isinstance(raw_artist, dict):
                continue
            artist_name = (
                raw_artist.get("name")
                or raw_artist.get("artist")
                or raw_artist.get("title")
            )
            artist_id = (
                raw_artist.get("id")
                or raw_artist.get("browseId")
                or raw_artist.get("browse_id")
            )
            if not artist_name:
                continue
            artist_entities.append(
                {
                    "id": artist_id,
                    "name": artist_name,
                    "thumbnail": server.extract_thumbnail(raw_artist),
                }
            )
    primary_artist = artist_entities[0] if artist_entities else {}
    return {
        "id": video_id,
        "videoId": video_id,
        "playback_source_id": video_id,
        "playback": {
            "provider": "youtube",
            "source_id": video_id,
        },
        "title": entry.get("title") or entry.get("name") or "Unknown Track",
        "duration": server.parse_duration_seconds(
            entry.get("duration_seconds")
            or entry.get("lengthSeconds")
            or entry.get("length")
            or entry.get("duration")
        ),
        "thumbnail": server.extract_thumbnail(entry),
        "channel": server.extract_artist(entry),
        "artist_id": primary_artist.get("id"),
        "artist_ids": [
            artist.get("id")
            for artist in artist_entities
            if artist.get("id")
        ],
        "artist_entities": artist_entities,
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
        "year": entry.get("year") or "",
        "release_date": entry.get("releaseDate") or entry.get("release_date") or "",
        "result_type": entry.get("resultType") or entry.get("type") or "",
        "video_type": entry.get("videoType") or "",
        "views": entry.get("views") or entry.get("viewCount") or 0,
        "is_explicit": bool(entry.get("isExplicit")),
        # A YTMusic `songs` result is provider-scoped catalog evidence. Preserve
        # that fact so later canonical normalization can derive source authority.
        "provider": "ytmusic",
        "source_provider": "youtube",
        "source_name": "ytmusic",
    }


def ytmusic_song_search(server: Any, query: str, limit: int):
    server = adapt_search_server(server)
    results = []
    seen = set()

    def add_entry(entry):
        normalized = normalize_song_result(server, entry)
        if not normalized:
            return
        track_id = normalized["id"]
        if not track_id or track_id in seen:
            return
        seen.add(track_id)
        results.append(normalized)

    raw_results = server.search_upstream_call_with_retry(
        lambda: server.ytmusic.search(query, filter="songs", limit=limit),
        default=[],
    )

    for entry in raw_results:
        add_entry(entry)
        if len(results) >= limit:
            return results

    if len(results) < limit:
        fallback_results = server.search_upstream_call_with_retry(
            lambda: server.ytmusic.search(query, limit=max(limit * 3, 12)),
            default=[],
        )

        for entry in fallback_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            add_entry(entry)
            if len(results) >= limit:
                break

    return results


def ytdlp_song_search(server: Any, query: str, limit: int):
    server = adapt_search_server(server)
    url = f"ytsearch{limit}:{query}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietSearchLogger(),
        "extract_flat": True,
        "skip_download": True,
    }
    results = []
    seen = set()
    try:
        with server.yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return results

    for entry in info.get("entries", []) or []:
        video_id = entry.get("id") or entry.get("url")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        results.append(
            {
                "id": video_id,
                "title": entry.get("title"),
                "duration": server.parse_duration_seconds(entry.get("duration")),
                "thumbnail": server.extract_thumbnail(entry),
                "channel": server.extract_artist(entry),
                "album": None,
                "album_id": None,
                "provider": "youtube_search",
                "source_name": "youtube_search",
                "source_authority": "search_only",
            }
        )
        if len(results) >= limit:
            break
    return results


def search_artists_direct(server: Any, query: str, limit: int):
    server = adapt_search_server(server)
    query = (query or "").strip()
    if not query:
        return []
    cache_key = _search_cache_key(server, query, limit)
    from .cache_runtime import lookup_search_result, store_search_result

    cached = lookup_search_result("artists_direct", cache_key)
    if cached:
        return [dict(item) for item in cached]

    raw_results = server.search_upstream_call_with_retry(
        lambda: server.ytmusic.search(query, filter="artists", limit=limit),
        default=[],
    )
    artists = server.normalize_artist_results(raw_results)
    if not artists:
        fallback_results = server.search_upstream_call_with_retry(
            lambda: server.ytmusic.search(query, limit=max(limit * 3, 12)),
            default=[],
        )
        artists = server.normalize_artist_results(fallback_results)
    normalized_query = server.normalize_text(query)
    tokens = server.query_tokens(query)

    def artist_score(item):
        name = server.normalize_text(item.get("name"))
        if normalized_query and name == normalized_query:
            return 5
        if normalized_query and normalized_query in name:
            return 4
        if tokens and all(token in name for token in tokens):
            return 3
        if tokens and any(token in name for token in tokens):
            return 2
        return 1

    artists.sort(
        key=lambda item: (
            artist_score(item),
            -len(server.normalize_text(item.get("name"))),
        ),
        reverse=True,
    )
    results = artists[:limit]
    if results:
        store_search_result("artists_direct", cache_key, results)
    return [dict(item) for item in results]


def artist_names_from_track_query(server: Any, query: str, limit: int):
    server = adapt_search_server(server)
    normalized_query = server.normalize_text(query)
    tokens = server.query_tokens(query)
    if not normalized_query and not tokens:
        return []

    candidates = []
    raw_tracks = ytmusic_song_search(server, query, max(limit * 3, 12))
    for index, track in enumerate(raw_tracks):
        title_text = server.normalize_text(track.get("title"))
        if not title_text:
            continue
        if normalized_query == title_text:
            match_score = 5.0
        elif normalized_query and normalized_query in title_text:
            match_score = 4.2
        elif tokens and all(token in title_text for token in tokens):
            match_score = 3.4
        elif tokens and any(token in title_text for token in tokens):
            match_score = 2.0
        else:
            continue
        artist_names = server.extract_artist_names(track)
        for artist_name in artist_names:
            candidates.append((artist_name, max(match_score - (index * 0.18), 0.6)))

    weighted = {}
    for artist_name, score in candidates:
        weighted[artist_name] = max(weighted.get(artist_name, 0.0), score)
    ranked = sorted(weighted.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def search_artists(server: Any, query: str, limit: int):
    server = adapt_search_server(server)
    query = (query or "").strip()
    if not query:
        return []
    cache_key = _search_cache_key(server, query, limit)
    from .cache_runtime import lookup_search_result, store_search_result

    cached = lookup_search_result("artists", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    combined = {}

    def upsert_artist(item, source_score: float):
        artist_id = (item.get("id") or "").strip()
        artist_name = (item.get("name") or "").strip()
        normalized_name = server.normalize_text(artist_name)
        if not artist_id or not artist_name or not normalized_name:
            return
        current = combined.get(artist_id)
        score = source_score
        if current is None or score > current.get("score", 0):
            combined[artist_id] = {
                **item,
                "score": score,
            }

    direct_artists = search_artists_direct(server, query, max(limit * 2, 6))
    normalized_query = server.normalize_text(query)
    tokens = server.query_tokens(query)
    for artist in direct_artists:
        name = server.normalize_text(artist.get("name"))
        if normalized_query and name == normalized_query:
            score = 5.0
        elif normalized_query and normalized_query in name:
            score = 4.0
        elif tokens and all(token in name for token in tokens):
            score = 3.1
        elif tokens and any(token in name for token in tokens):
            score = 2.1
        else:
            score = 1.0
        upsert_artist(artist, score)

    for artist_name, seed_score in artist_names_from_track_query(server, query, max(limit, 4)):
        for artist in search_artists_direct(server, artist_name, 2):
            upsert_artist(artist, seed_score + 1.6)

    artists = list(combined.values())
    artists.sort(
        key=lambda item: (
            item.get("score", 0),
            -len(server.normalize_text(item.get("name") or "")),
        ),
        reverse=True,
    )
    results = artists[:limit]
    store_search_result("artists", cache_key, results)
    return [dict(item) for item in results]
