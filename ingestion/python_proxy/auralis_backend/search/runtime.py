from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

from .cache_runtime import lookup_search_result, store_search_result
from .intelligence import load_fuzzy_catalog_entity_memories
from .server_adapter import SearchServerAdapter, adapt_search_server
from .upstream_runtime import (
    search_artists as resolve_search_artists,
    search_artists_direct as resolve_search_artists_direct,
    ytmusic_song_search as resolve_ytmusic_song_search,
)

_SUGGEST_UPSTREAM_TIMEOUT_SECONDS = 0.55

def _resolve_server(server: Any | None = None) -> SearchServerAdapter:
    return adapt_search_server(server)


def trim_text(value: Optional[str]) -> str:
    return _resolve_server().trim_text(value)


def unique_strings(values, limit: Optional[int] = None) -> List[str]:
    return _resolve_server().unique_strings(values, limit)


def _search_executor(server: Any):
    return getattr(server, "search_executor", server.recommendation_executor)


def _search_upstream_executor(server: Any):
    return getattr(server, "search_upstream_executor", _search_executor(server))


def search_query_intent(query: str, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    normalized = server.normalize_text(query)
    if not normalized:
        return "mixed"
    query_tokens = server.query_tokens(query)
    if re.search(r"(youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)", normalized):
        return "track"
    if any(token in normalized for token in ("album", "ep", "ost", "soundtrack")):
        return "album"
    if any(token in normalized for token in ("artist", "band", "singer")):
        return "artist"
    if any(token in normalized for token in ("song", "track", "lyrics")):
        return "track"
    if len(query_tokens) <= 2 and not any(char.isdigit() for char in normalized):
        return "mixed"
    return "mixed"


def search_artist_seed_tracks(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    artists = resolve_search_artists(server, query, 2)
    if not artists:
        return []
    normalized_query = server.normalize_text(query)
    query_tokens = server.query_tokens(query)
    tracks = []
    seen = set()
    for artist in artists:
        artist_name = server.normalize_text(artist.get("name"))
        if normalized_query and artist_name:
            if normalized_query not in artist_name and not any(
                token in artist_name for token in query_tokens
            ):
                continue
        artist_id = trim_text(artist.get("id"))
        if not artist_id:
            continue
        try:
            payload = server.build_artist_details_payload(artist_id)
        except Exception:
            payload = {}
        for track in payload.get("top_songs") or []:
            normalized = server.normalize_track(track)
            track_id = trim_text((normalized or {}).get("id"))
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            tracks.append(normalized)
            if len(tracks) >= limit:
                return tracks
    return tracks


def _search_cache_key(query: str, limit: int, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    normalized_query = server.normalize_text(query)
    return f"{normalized_query}|{max(int(limit or 0), 0)}"


def _has_resolved_album_artist(server: Any, album: Dict[str, Any]) -> bool:
    artist = server.normalize_text((album or {}).get("artist") or "")
    return bool(artist and artist not in {"unknown", "unknown artist"})


def _resolved_albums(server: Any, albums: Any) -> List[Dict[str, Any]]:
    return [
        dict(album)
        for album in list(albums or [])
        if isinstance(album, dict) and _has_resolved_album_artist(server, album)
    ]


def search_tracks_direct(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    # Search pages ask for 24 visible tracks and need headroom for canonical
    # deduplication and relevance admission. The old 24-result provider cap
    # guaranteed a thin shelf whenever even one upload was rejected.
    limit = max(1, min(limit, 48))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("tracks_direct", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    ytmusic_started = time.perf_counter()
    results = resolve_ytmusic_song_search(server, query, max(limit, 12))
    print(
        "[EBB:search][direct] "
        f"query={query[:48]} stage=ytmusic done elapsed_ms={int((time.perf_counter() - ytmusic_started) * 1000)} "
        f"results={len(results or [])}",
        flush=True,
    )
    final_results = [dict(item) for item in list(results or [])[:limit] if isinstance(item, dict)]
    store_search_result("tracks_direct", cache_key, final_results)
    return [dict(item) for item in final_results]


def _normalize_playlist_entries(
    server: Any,
    entries: Any,
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        result_type = str(
            entry.get("resultType") or entry.get("type") or ""
        ).strip().lower()
        if result_type and result_type not in {"playlist", "playlists"}:
            continue
        playlist_id = trim_text(
            entry.get("browseId")
            or entry.get("playlistId")
            or entry.get("id")
        )
        title = trim_text(entry.get("title") or entry.get("name"))
        if not playlist_id or not title or playlist_id in seen:
            continue
        seen.add(playlist_id)
        try:
            track_count = int(
                str(entry.get("itemCount") or "0").replace(",", "")
            )
        except (TypeError, ValueError):
            track_count = 0
        output.append(
            {
                "id": playlist_id,
                "name": title,
                "title": title,
                "thumbnail": server.extract_thumbnail(entry),
                "author": server.extract_artist(entry),
                "track_count": track_count,
                "result_type": "playlist",
                "provider": "ytmusic",
            }
        )
        if len(output) >= limit:
            break
    return output


def search_artists_direct_cached(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(limit, 18))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("artists_direct", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    results = resolve_search_artists_direct(server, query, limit)
    final_results = [dict(item) for item in list(results or [])[:limit] if isinstance(item, dict)]
    store_search_result("artists_direct", cache_key, final_results)
    return [dict(item) for item in final_results]


def search_albums_direct(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(limit, 18))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("albums_direct", cache_key)
    if cached is not None:
        return _resolved_albums(server, cached)[:limit]

    direct_results = server.search_upstream_call_with_retry(
        lambda: server.ytmusic.search(query, filter="albums", limit=max(limit, 8)),
        default=[],
    )
    albums = server.normalize_album_results(direct_results)
    if not albums:
        fallback_results = server.search_upstream_call_with_retry(
            lambda: server.ytmusic.search(query, limit=max(limit * 2, 12)),
            default=[],
        )
        albums = server.normalize_album_results(fallback_results)
    final_results = _resolved_albums(server, albums)[:limit]
    store_search_result("albums_direct", cache_key, final_results)
    return [dict(item) for item in final_results]


def search_playlists_direct(
    query: str,
    limit: int,
    *,
    server: Any | None = None,
):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(int(limit or 1), 36))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("playlists_direct", cache_key)
    if cached is not None:
        return [dict(item) for item in cached[:limit] if isinstance(item, dict)]

    raw_results = server.search_upstream_call_with_retry(
        lambda: server.ytmusic.search(query, filter="playlists", limit=limit),
        default=[],
    )
    output = _normalize_playlist_entries(server, raw_results, limit=limit)
    store_search_result("playlists_direct", cache_key, output)
    return [dict(item) for item in output]


def album_matches_track_anchor(
    track: Dict[str, Any] | None,
    album: Dict[str, Any] | None,
    *,
    server: Any | None = None,
) -> bool:
    server = _resolve_server(server)
    if not isinstance(track, dict) or not isinstance(album, dict):
        return False
    track_album = server.normalize_text(
        track.get("album") or track.get("album_title") or ""
    )
    album_title = server.normalize_text(album.get("title") or "")
    if not track_album or album_title != track_album:
        return False
    track_artist = server.normalize_text(
        track.get("channel") or track.get("artist") or ""
    )
    album_artist = server.normalize_text(album.get("artist") or "")
    return not track_artist or not album_artist or album_artist == track_artist


def search_canonical_album_for_track(
    track: Dict[str, Any] | None,
    *,
    server: Any | None = None,
) -> Dict[str, Any] | None:
    server = _resolve_server(server)
    if not isinstance(track, dict):
        return None
    album_title = trim_text(track.get("album") or track.get("album_title"))
    if not album_title:
        return None
    album_id = trim_text(track.get("album_id"))
    canonical = {
        "id": album_id or None,
        "title": album_title,
        "artist": trim_text(track.get("channel") or track.get("artist")) or None,
        "thumbnail": track.get("thumbnail"),
        "year": track.get("year") or "",
        "track_count": 0,
    }
    if album_id:
        return canonical
    for album in search_albums_direct(album_title, 6, server=server):
        if not album_matches_track_anchor(track, album, server=server):
            continue
        resolved = dict(album)
        for key, value in canonical.items():
            if not resolved.get(key) and value:
                resolved[key] = value
        return resolved
    return canonical


def semantic_search_lexical_score(query: str, *texts, server: Any | None = None) -> float:
    server = _resolve_server(server)
    normalized_query = server.normalize_text(query)
    tokens = server.query_tokens(query)
    normalized_texts = [
        server.normalize_text(text)
        for text in texts
        if server.normalize_text(text)
    ]
    if not normalized_query and not tokens:
        return 0.0
    if not normalized_texts:
        return 0.0

    primary_text = normalized_texts[0]
    combined_text = " ".join(normalized_texts)
    score = 0.0

    if normalized_query and primary_text == normalized_query:
        score += 5.2
    elif normalized_query and normalized_query in primary_text:
        score += 4.2
    elif normalized_query and normalized_query in combined_text:
        score += 3.4

    if tokens:
        token_hits = sum(1 for token in tokens[:6] if token in combined_text)
        if token_hits:
            score += min(token_hits, 4) * 0.62
        if all(token in combined_text for token in tokens[:4]):
            score += 1.15

    return score


def semantic_search_anchor_tracks(
    req,
    direct_pool,
    blended_pool,
    *,
    limit: int = 4,
    server: Any | None = None,
):
    server = _resolve_server(server)
    provided = server.recommendation_unique_snapshot_tracks(
        req.anchor_track_snapshots or [],
        limit,
    )
    if provided:
        return provided

    normalized_query = server.normalize_text(req.query)
    query_tokens = server.query_tokens(req.query)
    seen_signatures = set()
    ranked = []

    def add_anchor(raw_track, index: int, base_score: float) -> None:
        normalized = server.normalize_track(raw_track)
        if normalized is None:
            return
        signature = server.recommendation_track_signature(normalized)
        if not signature or signature in seen_signatures:
            return
        seen_signatures.add(signature)
        title_text = server.normalize_text(normalized.get("title") or "")
        artist_text = server.normalize_text(
            normalized.get("channel") or normalized.get("artist") or ""
        )
        if not title_text and not artist_text:
            return
        score = float(base_score)
        if normalized_query and title_text == normalized_query:
            score += 6.2
        elif normalized_query and normalized_query in title_text:
            score += 4.8
        elif query_tokens and all(token in title_text for token in query_tokens[:5]):
            score += 3.7
        elif query_tokens and any(token in title_text for token in query_tokens[:5]):
            score += 1.45
        if normalized_query and artist_text == normalized_query:
            score -= 0.85
        score -= index * 0.05
        ranked.append((score, normalized))

    for index, track in enumerate(direct_pool or []):
        add_anchor(track, index, 1.4)
    for index, track in enumerate(blended_pool or []):
        add_anchor(track, index, 1.1)

    ranked.sort(key=lambda item: item[0], reverse=True)
    anchors = [track for score, track in ranked if score > 0][:limit]
    if anchors:
        return anchors

    fallback = []
    fallback_seen = set()
    for raw_track in list(direct_pool or []) + list(blended_pool or []):
        normalized = server.normalize_track(raw_track)
        if normalized is None:
            continue
        signature = server.recommendation_track_signature(normalized)
        if not signature or signature in fallback_seen:
            continue
        fallback_seen.add(signature)
        fallback.append(normalized)
        if len(fallback) >= limit:
            break
    return fallback


def semantic_search_anchor_artist_names(anchor_tracks, limit: int = 6, *, server: Any | None = None):
    server = _resolve_server(server)
    return unique_strings(
        [
            artist_name
            for track in anchor_tracks or []
            for artist_name in server.extract_artist_names(track)
        ],
        limit,
    )


def _semantic_suggestion_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", trim_text(value))


def _semantic_track_suggestion_text(
    track: Optional[Dict[str, Any]],
    *,
    server: Any | None = None,
) -> str:
    server = _resolve_server(server)
    if not isinstance(track, dict):
        return ""
    title = _semantic_suggestion_text(track.get("title"))
    artist = _semantic_suggestion_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    if not title:
        return ""
    if artist and server.normalize_text(artist) not in server.normalize_text(title):
        return f"{title} - {artist}"
    return title


def _fast_suggestion_cache_key(req, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    recent_track_ids: List[str] = []
    for raw_track in [
        *(getattr(req, "last_played_tracks", []) or []),
        *(getattr(req, "recent_tracks", []) or []),
        *(getattr(req, "recent_track_snapshots", []) or []),
        *(getattr(req, "top_track_snapshots", []) or []),
        *(getattr(req, "anchor_track_snapshots", []) or []),
    ]:
        if isinstance(raw_track, dict):
            track_id = _suggestion_track_id(raw_track)
            if track_id and track_id not in recent_track_ids:
                recent_track_ids.append(track_id)
        if len(recent_track_ids) >= 24:
            break
    payload = {
        "namespace": "fast_suggestions_v2",
        "query": server.normalize_text(req.query),
        "user_scope_id": server.safe_scope_id(
            getattr(req, "user_scope_id", "guest") or "guest"
        ),
        "limit": max(int(req.limit or 0), 0),
        "recent_track_ids": recent_track_ids,
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _fast_lexical_suggestion_score(
    query: str,
    suggestion_text: str,
    *,
    server: Any | None = None,
) -> float:
    server = _resolve_server(server)
    normalized_query = server.normalize_text(query)
    normalized_text = server.normalize_text(suggestion_text)
    if not normalized_query or not normalized_text:
        return 0.0
    if normalized_text == normalized_query:
        return 0.0
    score = semantic_search_lexical_score(query, suggestion_text, server=server)
    if normalized_text.startswith(normalized_query):
        score += 1.35
    elif normalized_query in normalized_text:
        score += 0.45
    return score


def _suggestion_track_id(track: Dict[str, Any]) -> str:
    for key in ("id", "videoId", "video_id", "track_id"):
        value = trim_text(track.get(key))
        if value:
            return value
    return ""


def _track_suggestion_payload(track: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in dict(track).items()
        if key not in {"raw", "raw_track", "raw_payload"}
    }


def _recent_track_suggestion_items(
    req,
    query: str,
    *,
    server: Any | None = None,
    limit: int = 2,
) -> List[Dict[str, Any]]:
    server = _resolve_server(server)
    normalized_query = server.normalize_text(query)
    query_tokens = server.query_tokens(query)
    if not normalized_query and not query_tokens:
        return []
    raw_tracks = [
        *(getattr(req, "last_played_tracks", []) or []),
        *(getattr(req, "recent_tracks", []) or []),
        *(getattr(req, "recent_track_snapshots", []) or []),
        *(getattr(req, "top_track_snapshots", []) or []),
        *(getattr(req, "anchor_track_snapshots", []) or []),
    ]
    ranked: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_track in enumerate(raw_tracks):
        if not isinstance(raw_track, dict):
            continue
        track = dict(raw_track)
        track_id = _suggestion_track_id(track)
        title = _semantic_suggestion_text(track.get("title") or track.get("name"))
        artist = _semantic_suggestion_text(
            track.get("artist") or track.get("channel") or track.get("author")
        )
        if not title or not track_id or track_id in seen_ids:
            continue
        haystack = server.normalize_text(" ".join([title, artist]))
        title_key = server.normalize_text(title)
        token_hits = sum(1 for token in query_tokens if token and token in haystack)
        matched = (
            normalized_query
            and (normalized_query in haystack or haystack.startswith(normalized_query))
        ) or (query_tokens and token_hits >= max(1, min(len(query_tokens), 2)))
        if not matched:
            continue
        exact_bonus = 1.2 if normalized_query and normalized_query == title_key else 0.0
        prefix_bonus = 0.55 if title_key.startswith(normalized_query) else 0.0
        score = 8.0 + exact_bonus + prefix_bonus + min(token_hits, 4) * 0.18 - index * 0.035
        seen_ids.add(track_id)
        ranked.append(
            {
                "text": _semantic_track_suggestion_text(track, server=server),
                "source_score": score,
                "source_name": "recently_played",
                "suggestion_type": "track_play",
                "direct_play": True,
                "track": _track_suggestion_payload(track),
                "score": round(score, 3),
                "lexical_score": 1.0,
            }
        )
    ranked.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return ranked[: max(0, int(limit or 0))]


def semantic_search_suggestion_items(req, *, server: Any | None = None):
    server = _resolve_server(server)
    query = server.trim_text(req.query)
    limit = max(1, min(req.limit or 5, 8))
    if not query:
        return []

    cache_key = _fast_suggestion_cache_key(req, server=server)
    cached = lookup_search_result("suggestions", cache_key)
    if cached is not None:
        if cached and isinstance(cached[0], dict):
            return list(cached[:limit])
        return [
            {
                "text": _semantic_suggestion_text(value),
                "source_score": 1.0,
                "source_name": "suggestion_cache",
                "suggestion_type": "query",
                "direct_play": False,
            }
            for value in list(cached[:limit])
            if _semantic_suggestion_text(value)
        ]

    normalized_query = server.normalize_text(query)
    candidates = {}
    direct_play_items = _recent_track_suggestion_items(
        req,
        query,
        server=server,
        limit=min(3, limit),
    )
    if direct_play_items and float(direct_play_items[0].get("score") or 0.0) >= 8.55:
        fast_results = direct_play_items[:limit]
        store_search_result("suggestions", cache_key, fast_results)
        return list(fast_results)

    def add_candidate(
        raw_text: Optional[str],
        source_score: float,
        source_name: str,
        suggestion_type: str,
    ) -> None:
        text = _semantic_suggestion_text(raw_text)
        normalized = server.normalize_text(text)
        if not text or not normalized or normalized == normalized_query:
            return
        current = candidates.get(normalized)
        if current is None or float(source_score) > float(current.get("source_score") or 0.0):
            candidates[normalized] = {
                "text": text,
                "source_score": float(source_score),
                "source_name": source_name,
                "suggestion_type": suggestion_type,
            }

    for memory in load_fuzzy_catalog_entity_memories(
        server,
        query=query,
        limit=max(limit * 2, 8),
        scan_limit=600,
    ):
        payload = dict(memory.get("payload") or {})
        entity_type = str(memory.get("entity_type") or "")
        if entity_type == "artist":
            text = payload.get("name") or payload.get("artist")
        elif entity_type == "album":
            text = payload.get("title") or payload.get("name")
        else:
            title = payload.get("title") or payload.get("name")
            artist = payload.get("artist") or payload.get("channel")
            text = f"{title} — {artist}" if title and artist else title
        add_candidate(
            text,
            3.8 + min(float(memory.get("confidence") or 0.0), 1.0),
            "canonical_prefix_index",
            entity_type if entity_type in {"track", "artist", "album"} else "query",
        )

    search_started_at = time.perf_counter()
    upstream_future = None
    if len(candidates) + len(direct_play_items) < limit:
        upstream_future = _search_upstream_executor(server).submit(
            server.ytmusic.get_search_suggestions,
            query,
        )

    try:
        if upstream_future is None:
            upstream_suggestions = []
        else:
            upstream_suggestions = upstream_future.result(timeout=_SUGGEST_UPSTREAM_TIMEOUT_SECONDS)
    except Exception:
        upstream_suggestions = []
    for index, raw_suggestion in enumerate(upstream_suggestions[:8]):
        suggestion_text = (
            raw_suggestion.get("text", "")
            if isinstance(raw_suggestion, dict)
            else str(raw_suggestion or "")
        )
        add_candidate(
            suggestion_text,
            max(4.1 - (index * 0.18), 1.2),
            "upstream_suggestion",
            "query",
        )

    request_recent_queries = unique_strings(
        [
            *(getattr(req, "recent_queries", []) or []),
            *(getattr(req, "taste_queries", []) or []),
        ],
        8,
    )
    for index, recent_query in enumerate(request_recent_queries):
        recent_query_text = _semantic_suggestion_text(recent_query)
        if not recent_query_text:
            continue
        if normalized_query and normalized_query not in server.normalize_text(recent_query_text):
            continue
        add_candidate(
            recent_query_text,
            max(2.2 - (index * 0.12), 0.6),
            "recent_query_history",
            "query",
        )

    if not candidates and not direct_play_items:
        return []

    ranked = []
    for item in candidates.values():
        suggestion_text = item["text"]
        lexical_score = _fast_lexical_suggestion_score(
            query,
            suggestion_text,
            server=server,
        )
        ranking_score = (
            (float(item.get("source_score") or 0.0) * 0.72)
            + lexical_score
        )
        if item.get("suggestion_type") == "artist":
            ranking_score += 0.35
        elif item.get("suggestion_type") == "track":
            ranking_score += 0.18

        ranked.append(
            {
                **item,
                "score": round(ranking_score, 3),
                "lexical_score": round(lexical_score, 4),
            }
        )

    ranked.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("text") or "",
        ),
        reverse=True,
    )

    results = []
    seen_texts = set()
    type_counts = {}
    type_caps = {
        "query": 3,
        "artist": 2,
        "track": 2,
        "track_play": 2,
        "album": 2,
    }
    for item in direct_play_items:
        normalized = server.normalize_text(item.get("text"))
        if normalized:
            seen_texts.add(normalized)
        results.append(item)
    for item in ranked:
        suggestion_type = item.get("suggestion_type") or "query"
        if type_counts.get(suggestion_type, 0) >= type_caps.get(suggestion_type, limit):
            if len(results) + 1 < limit:
                continue
        normalized = server.normalize_text(item.get("text"))
        if normalized and normalized in seen_texts:
            continue
        if normalized:
            seen_texts.add(normalized)
        results.append(item)
        type_counts[suggestion_type] = type_counts.get(suggestion_type, 0) + 1
        if len(results) >= limit:
            break

    store_search_result("suggestions", cache_key, results)
    if (time.perf_counter() - search_started_at) >= 0.75:
        print(
            "[EBB:suggest][slow] "
            f'query="{query}" '
            f"results={len(results)} "
            f"elapsed_ms={int((time.perf_counter() - search_started_at) * 1000)}"
        )
    return list(results)
