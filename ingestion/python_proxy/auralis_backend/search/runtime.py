from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

from .cache_runtime import lookup_search_result, store_search_result
from .server_adapter import SearchServerAdapter, adapt_search_server
from .upstream_runtime import (
    search_artists as resolve_search_artists,
    search_artists_direct as resolve_search_artists_direct,
    ytdlp_song_search as resolve_ytdlp_song_search,
    ytmusic_song_search as resolve_ytmusic_song_search,
)

_SEARCH_BLEND_TIMEOUT_SECONDS = 1.2
_SUGGEST_UPSTREAM_TIMEOUT_SECONDS = 0.55
_SUGGEST_ARTIST_TIMEOUT_SECONDS = 0.45
_SUGGEST_TRACK_TIMEOUT_SECONDS = 0.55

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


def search_albums_for_artist_name(artist_name: str, *, server: Any | None = None):
    server = _resolve_server(server)
    normalized_artist = trim_text(artist_name)
    if not normalized_artist:
        return {}
    direct_artists = resolve_search_artists_direct(server, normalized_artist, 1)
    if not direct_artists:
        return {}
    artist_id = trim_text(direct_artists[0].get("id"))
    if not artist_id:
        return {}
    try:
        return server.build_artist_details_payload(artist_id)
    except Exception:
        return {}


def _search_cache_key(query: str, limit: int, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    normalized_query = server.normalize_text(query)
    return f"{normalized_query}|{max(int(limit or 0), 0)}"


def search_tracks_direct(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(limit, 24))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("tracks_direct", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    results = resolve_ytmusic_song_search(server, query, max(limit, 12))
    if not results:
        results = resolve_ytdlp_song_search(server, query, max(limit, 12))
    final_results = [dict(item) for item in list(results or [])[:limit] if isinstance(item, dict)]
    store_search_result("tracks_direct", cache_key, final_results)
    return [dict(item) for item in final_results]


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
        return [dict(item) for item in cached]

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
    final_results = [dict(item) for item in list(albums or [])[:limit] if isinstance(item, dict)]
    store_search_result("albums_direct", cache_key, final_results)
    return [dict(item) for item in final_results]


def search_albums_blended(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(limit, 18))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("albums", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    results = []
    seen = set()

    def add_albums(albums, max_to_add: Optional[int] = None):
        added = 0
        for raw_album in albums or []:
            if not isinstance(raw_album, dict):
                continue
            album_id = trim_text(raw_album.get("id"))
            title = trim_text(raw_album.get("title"))
            artist = trim_text(raw_album.get("artist"))
            key = album_id or f"{server.normalize_text(title)}|{server.normalize_text(artist)}"
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(raw_album)
            added += 1
            if len(results) >= limit:
                break
            if max_to_add is not None and added >= max_to_add:
                break

    direct_results = server.upstream_call_with_retry(
        lambda: server.ytmusic.search(query, filter="albums", limit=max(limit, 8)),
        default=[],
    )
    direct_albums = server.normalize_album_results(direct_results)
    add_albums(direct_albums, max_to_add=min(max(4, limit // 2), 8))

    track_anchor_future = _search_executor(server).submit(
        resolve_ytmusic_song_search,
        server,
        query,
        max(6, limit),
    )
    query_artist_future = _search_executor(server).submit(
        resolve_search_artists,
        server,
        query,
        2,
    )

    try:
        track_anchor_results = track_anchor_future.result(timeout=_SEARCH_BLEND_TIMEOUT_SECONDS)
    except Exception:
        track_anchor_results = []

    anchor_artist_payload_futures = {}
    for index, track in enumerate(track_anchor_results[:4]):
        artist_name = trim_text(track.get("channel"))
        if not artist_name:
            continue
        anchor_artist_payload_futures[index] = _search_executor(server).submit(
            search_albums_for_artist_name,
            artist_name,
            server=server,
        )

    for index, track in enumerate(track_anchor_results[:4]):
        album_title = trim_text(track.get("album"))
        album_id = trim_text(track.get("album_id"))
        if album_title:
            add_albums(
                [
                    {
                        "id": album_id or None,
                        "title": album_title,
                        "artist": track.get("channel"),
                        "thumbnail": track.get("thumbnail"),
                    }
                ],
                max_to_add=1,
            )
        try:
            artist_payload = anchor_artist_payload_futures[index].result(timeout=_SEARCH_BLEND_TIMEOUT_SECONDS)
        except Exception:
            artist_payload = {}
        add_albums(
            artist_payload.get("albums") or [],
            max_to_add=max(2, 4 - index),
        )
        if len(results) >= limit:
            return results[:limit]

    if len(results) < limit:
        try:
            direct_artists = query_artist_future.result(timeout=_SEARCH_BLEND_TIMEOUT_SECONDS)
        except Exception:
            direct_artists = []
        for artist in direct_artists:
            artist_id = trim_text(artist.get("id"))
            if not artist_id:
                continue
            try:
                artist_payload = server.build_artist_details_payload(artist_id)
            except Exception:
                artist_payload = {}
            add_albums(artist_payload.get("albums") or [], max_to_add=4)
            if len(results) >= limit:
                break

    if len(results) < limit:
        fallback_results = server.upstream_call_with_retry(
            lambda: server.ytmusic.search(query, limit=max(limit * 3, 12)),
            default=[],
        )
        add_albums(server.normalize_album_results(fallback_results))

    final_results = results[:limit]
    store_search_result("albums", cache_key, final_results)
    return [dict(item) for item in final_results]


def search_tracks_blended(query: str, limit: int, *, server: Any | None = None):
    server = _resolve_server(server)
    query = (query or "").strip()
    limit = max(1, min(limit, 30))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit, server=server)
    cached = lookup_search_result("tracks", cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    direct_pool = resolve_ytmusic_song_search(server, query, max(limit, 14))
    if not direct_pool:
        direct_pool = resolve_ytdlp_song_search(server, query, max(limit, 14))

    results = []
    seen = set()

    def add_tracks(tracks, max_to_add: Optional[int] = None):
        added = 0
        for raw_track in tracks or []:
            normalized = server.normalize_track(raw_track)
            if normalized is None:
                continue
            track_id = trim_text(normalized.get("id"))
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            results.append(normalized)
            added += 1
            if len(results) >= limit:
                break
            if max_to_add is not None and added >= max_to_add:
                break

    add_tracks(direct_pool, max_to_add=min(max(6, limit // 3), 10))

    anchor_track = results[0] if results else (direct_pool[0] if direct_pool else None)
    query_tokens = server.query_tokens(query)
    similar_tracks_future = None
    if anchor_track is not None and anchor_track.get("id"):
        anchor_text = server.normalize_text(
            f"{anchor_track.get('title') or ''} {anchor_track.get('channel') or ''}"
        )
        if not query_tokens or any(token in anchor_text for token in query_tokens):
            similar_tracks_future = _search_executor(server).submit(
                server.assistant_tool_get_similar_tracks,
                anchor_track["id"],
                max(6, min(10, limit // 2)),
            )
    artist_seed_future = _search_executor(server).submit(
        search_artist_seed_tracks,
        query,
        max(4, min(8, limit // 3)),
        server=server,
    )

    if similar_tracks_future is not None:
        try:
            similar_tracks = similar_tracks_future.result(timeout=_SEARCH_BLEND_TIMEOUT_SECONDS)
        except Exception:
            similar_tracks = []
        add_tracks(
            similar_tracks,
            max_to_add=max(6, min(10, limit // 2)),
        )

    if len(results) < limit:
        try:
            artist_seed_tracks = artist_seed_future.result(timeout=_SEARCH_BLEND_TIMEOUT_SECONDS)
        except Exception:
            artist_seed_tracks = []
        add_tracks(
            artist_seed_tracks,
            max_to_add=max(4, min(8, limit // 3)),
        )

    if len(results) < limit:
        add_tracks(direct_pool)

    if len(results) < limit:
        broad_results = server.upstream_call_with_retry(
            lambda: server.ytmusic.search(query, limit=max(limit * 4, 24)),
            default=[],
        )
        filtered = []
        for entry in broad_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            filtered.append(entry)
        add_tracks(filtered)

    if len(results) < limit:
        add_tracks(resolve_ytdlp_song_search(server, query, limit))

    final_results = results[:limit]
    store_search_result("tracks", cache_key, final_results)
    return [dict(item) for item in final_results]


def semantic_search_cache_key(req, namespace: str, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    payload = {
        "namespace": namespace,
        "limit": max(int(req.limit or 0), 0),
        "profile_key": server.recommendation_profile_key(req),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


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


def semantic_search_vectors(req, profile, *, server: Any | None = None):
    server = _resolve_server(server)
    query_text = server.trim_text(req.query)
    query_vector = []
    if query_text:
        query_key = server.recommendation_text_embedding_key("semantic_search_query", query_text)
        if query_key:
            text_embeddings = server.recommendation_embed_entries(
                "text",
                [(query_key, query_text)],
            )
            query_vector = text_embeddings.get(query_key) or []

    vectors = profile.get("vectors") or {}
    semantic_query_vector = server.vector_weighted_average(
        [
            (query_vector, 2.2),
            (vectors.get("query_vector") or [], 0.95),
            (vectors.get("artist_vector") or [], 0.35),
        ]
    )
    semantic_context_vector = server.vector_weighted_average(
        [
            (semantic_query_vector, 1.55),
            (vectors.get("taste_vector") or [], 0.75),
            (vectors.get("short_term_vector") or [], 0.35),
        ]
    )
    return {
        "current_query_vector": query_vector,
        "semantic_query_vector": semantic_query_vector,
        "semantic_context_vector": semantic_context_vector,
    }


def semantic_search_vector_similarities(
    candidate_vector,
    search_vectors,
    profile,
    *,
    server: Any | None = None,
):
    server = _resolve_server(server)
    vectors = profile.get("vectors") or {}
    if not candidate_vector:
        return {
            "query": 0.0,
            "semantic_query": 0.0,
            "context": 0.0,
            "taste": 0.0,
            "artist": 0.0,
            "short": 0.0,
            "long": 0.0,
        }
    return {
        "query": server.cosine_similarity(
            candidate_vector,
            search_vectors.get("current_query_vector") or [],
        ),
        "semantic_query": server.cosine_similarity(
            candidate_vector,
            search_vectors.get("semantic_query_vector") or [],
        ),
        "context": server.cosine_similarity(
            candidate_vector,
            search_vectors.get("semantic_context_vector") or [],
        ),
        "taste": server.cosine_similarity(
            candidate_vector,
            vectors.get("taste_vector") or [],
        ),
        "artist": server.cosine_similarity(
            candidate_vector,
            vectors.get("artist_vector") or [],
        ),
        "short": server.cosine_similarity(
            candidate_vector,
            vectors.get("short_term_vector") or [],
        ),
        "long": server.cosine_similarity(
            candidate_vector,
            vectors.get("long_term_vector") or [],
        ),
    }


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


def _semantic_album_suggestion_text(
    album: Optional[Dict[str, Any]],
    *,
    server: Any | None = None,
) -> str:
    server = _resolve_server(server)
    if not isinstance(album, dict):
        return ""
    title = _semantic_suggestion_text(album.get("title"))
    artist = _semantic_suggestion_text(album.get("artist"))
    if not title:
        return ""
    if artist and server.normalize_text(artist) not in server.normalize_text(title):
        return f"{title} - {artist}"
    return title


def _fast_suggestion_cache_key(req, *, server: Any | None = None) -> str:
    server = _resolve_server(server)
    payload = {
        "namespace": "fast_suggestions_v1",
        "query": server.normalize_text(req.query),
        "user_scope_id": server.safe_scope_id(
            getattr(req, "user_scope_id", "guest") or "guest"
        ),
        "limit": max(int(req.limit or 0), 0),
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


def semantic_search_suggestions(req, *, server: Any | None = None):
    server = _resolve_server(server)
    query = server.trim_text(req.query)
    limit = max(1, min(req.limit or 5, 8))
    if not query:
        return []

    cache_key = _fast_suggestion_cache_key(req, server=server)
    cached = lookup_search_result("suggestions", cache_key)
    if cached is not None:
        return list(cached[:limit])

    normalized_query = server.normalize_text(query)
    candidates = {}

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

    search_started_at = time.perf_counter()
    upstream_future = _search_upstream_executor(server).submit(
        server.ytmusic.get_search_suggestions,
        query,
    )
    artist_future = _search_executor(server).submit(
        resolve_search_artists_direct,
        server,
        query,
        3,
    )
    track_future = _search_executor(server).submit(
        resolve_ytmusic_song_search,
        server,
        query,
        4,
    )

    try:
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

    try:
            direct_artists = artist_future.result(timeout=_SUGGEST_ARTIST_TIMEOUT_SECONDS)
    except Exception:
        direct_artists = []
    for index, artist in enumerate(direct_artists[:3]):
        add_candidate(
            artist.get("name"),
            max(3.7 - (index * 0.16), 1.0),
            "direct_artist_search",
            "artist",
        )

    try:
            direct_tracks = track_future.result(timeout=_SUGGEST_TRACK_TIMEOUT_SECONDS)
    except Exception:
        direct_tracks = []
    for index, track in enumerate(direct_tracks[:4]):
        add_candidate(
            _semantic_track_suggestion_text(track, server=server),
            max(3.2 - (index * 0.15), 0.9),
            "direct_song_search",
            "track",
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

    if not candidates:
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
    type_counts = {}
    type_caps = {
        "query": 3,
        "artist": 2,
        "track": 2,
        "album": 2,
    }
    for item in ranked:
        suggestion_type = item.get("suggestion_type") or "query"
        if type_counts.get(suggestion_type, 0) >= type_caps.get(suggestion_type, limit):
            if len(results) + 1 < limit:
                continue
        results.append(item["text"])
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
