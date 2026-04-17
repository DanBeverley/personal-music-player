from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import os
from threading import Lock
import time
from typing import Any, Dict, Iterable, List

from .candidate_expansion import album_candidates_for_track, candidate_sources_for_track
from .server_adapter import adapt_domain_server
from ..search.runtime import (
    search_albums_blended,
    search_albums_direct,
    search_artist_seed_tracks,
    search_artists_direct_cached,
    search_tracks_blended,
    search_tracks_direct,
    semantic_search_anchor_artist_names,
    semantic_search_anchor_tracks,
    semantic_search_lexical_score,
)
from ..search.upstream_runtime import (
    artist_names_from_track_query as resolve_artist_names_from_track_query,
    search_artists as resolve_search_artists,
    search_artists_direct as resolve_search_artists_direct,
    ytmusic_song_search as resolve_ytmusic_song_search,
)

def trim_text(value: str | None) -> str:
    return adapt_domain_server().trim_text(value)


def unique_strings(values, limit: int | None = None) -> List[str]:
    return adapt_domain_server().unique_strings(values, limit)


_SEARCH_RETRIEVAL_CACHE_TTL_SECONDS = max(
    5,
    int(os.environ.get("AURALIS_SEARCH_RETRIEVAL_CACHE_TTL_SECONDS", "120")),
)
_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS = max(
    3.0,
    float(os.environ.get("AURALIS_SEARCH_RETRIEVAL_BUDGET_SECONDS", "8.5")),
)
_SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS = max(
    0.35,
    float(os.environ.get("AURALIS_SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS", "2.25")),
)
_SEARCH_DISABLE_TIMEOUTS = (
    (os.environ.get("AURALIS_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
    or (os.environ.get("AURALIS_SEARCH_DISABLE_TIMEOUTS", "0").strip().lower() in {"1", "true", "yes", "on"})
)
_search_retrieval_cache_lock = Lock()
_search_retrieval_cache: Dict[str, Dict[str, Any]] = {}


def _upsert_entity_candidate(
    combined: Dict[str, Dict[str, Any]],
    *,
    entity_id: str,
    payload: Dict[str, Any],
    source_name: str,
    source_score: float,
) -> None:
    if not entity_id:
        return
    current = combined.get(entity_id)
    if current is None:
        combined[entity_id] = {
            "payload": dict(payload),
            "source_scores": {source_name: float(source_score)},
        }
        return
    current["source_scores"][source_name] = max(
        float(current["source_scores"].get(source_name) or 0.0),
        float(source_score),
    )
    for key, value in payload.items():
        if current["payload"].get(key) in (None, "", 0) and value not in (None, ""):
            current["payload"][key] = value


def _collect_track_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    server,
    tracks: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, raw_track in enumerate(tracks or []):
        normalized = server.normalize_track(raw_track)
        if normalized is None:
            continue
        entity_id = trim_text(normalized.get("id"))
        if not entity_id:
            entity_id = server.recommendation_track_signature(normalized)
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=normalized,
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def _collect_artist_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    artists: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, artist in enumerate(artists or []):
        entity_id = trim_text((artist or {}).get("id"))
        if not entity_id:
            continue
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=dict(artist),
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def _collect_album_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    albums: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, album in enumerate(albums or []):
        entity_id = trim_text((album or {}).get("id"))
        if not entity_id:
            entity_id = f"{trim_text((album or {}).get('title'))}|{trim_text((album or {}).get('artist'))}"
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=dict(album),
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def classify_query_intent(
    *,
    server,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
) -> str:
    normalized_query = trim_text(query).lower()
    if not normalized_query:
        return "mixed"
    if "lyrics" in normalized_query:
        return "lyric"
    if any(
        token in normalized_query
        for token in ["mood", "chill", "focus", "sleep", "ambient", "playlist"]
    ):
        return "mood"
    if normalized_query.startswith("songs like ") or normalized_query.startswith("similar to "):
        return "mixed"

    def lexical(item: Dict[str, Any], *fields: str) -> float:
        values = [item.get(field) for field in fields if item.get(field)]
        return semantic_search_lexical_score(query, *values, server=server)

    top_track_score = lexical(tracks[0], "title", "channel", "album") if tracks else 0.0
    top_artist_score = lexical(artists[0], "name", "description") if artists else 0.0
    top_album_score = lexical(albums[0], "title", "artist") if albums else 0.0

    if top_track_score >= max(top_artist_score, top_album_score) + 0.5:
        return "track"
    if top_artist_score >= max(top_track_score, top_album_score) + 0.5:
        return "artist"
    if top_album_score >= max(top_track_score, top_artist_score) + 0.5:
        return "album"
    return "mixed"


def _retrieval_cache_key(
    legacy_req,
    profile,
    limit: int,
    *,
    server: Any | None = None,
) -> str:
    server = adapt_domain_server(server)
    recent_query_key = "|".join((profile.get("recent_queries") or [])[:4])
    recent_track_key = "|".join(
        trim_text(track.get("id"))
        for track in (profile.get("last_played_tracks") or [])[:4]
        if trim_text(track.get("id"))
    )
    return "||".join(
        [
            trim_text(legacy_req.query).lower(),
            trim_text(getattr(legacy_req, "surface", "") or "search"),
            trim_text(profile.get("user_scope_id") or "guest"),
            recent_query_key,
            recent_track_key,
            str(max(int(limit or 0), 0)),
        ]
    )


def _retrieval_cache_get(cache_key: str) -> Dict[str, Any] | None:
    if not cache_key:
        return None
    with _search_retrieval_cache_lock:
        cached = _search_retrieval_cache.get(cache_key)
        if not cached:
            return None
        if float(cached.get("expires_at") or 0.0) <= time.time():
            _search_retrieval_cache.pop(cache_key, None)
            return None
        return deepcopy(cached.get("payload"))


def _retrieval_cache_set(cache_key: str, payload: Dict[str, Any]) -> None:
    if not cache_key:
        return
    with _search_retrieval_cache_lock:
        _search_retrieval_cache[cache_key] = {
            "expires_at": time.time() + _SEARCH_RETRIEVAL_CACHE_TTL_SECONDS,
            "payload": deepcopy(payload),
        }
        if len(_search_retrieval_cache) > 96:
            expired_keys = [
                key
                for key, entry in _search_retrieval_cache.items()
                if float(entry.get("expires_at") or 0.0) <= time.time()
            ]
            for key in expired_keys:
                _search_retrieval_cache.pop(key, None)
            while len(_search_retrieval_cache) > 96:
                oldest_key = min(
                    _search_retrieval_cache,
                    key=lambda key: float(
                        (_search_retrieval_cache.get(key) or {}).get("expires_at") or 0.0
                    ),
                )
                _search_retrieval_cache.pop(oldest_key, None)


def _search_executor(server: Any):
    return getattr(server, "search_executor", None) or getattr(
        server,
        "recommendation_executor",
    )


def retrieve_search_candidates_fast(
    legacy_req,
    profile,
    *,
    limit: int,
    server: Any | None = None,
) -> Dict[str, Any]:
    server = adapt_domain_server(server)
    query = trim_text(legacy_req.query)
    if not query:
        return {
            "query_intent": "mixed",
            "track_candidates": {},
            "artist_candidates": {},
            "album_candidates": {},
            "anchor_tracks": [],
            "anchor_artist_names": [],
            "normalized_anchor_artists": set(),
            "retriever_counts": {},
            "retrieval_diagnostics": {
                "mode": "fast_query_fallback",
                "cache_hit": False,
                "retrieval_ms": 0,
                "partial_completion": False,
            },
        }

    cache_key = ""
    if not bool(getattr(legacy_req, "force_refresh", False)):
        cache_key = f"fast::{_retrieval_cache_key(legacy_req, profile, limit, server=server)}"
        cached_payload = _retrieval_cache_get(cache_key)
        if cached_payload is not None:
            diagnostics = dict(cached_payload.get("retrieval_diagnostics") or {})
            diagnostics["cache_hit"] = True
            cached_payload["retrieval_diagnostics"] = diagnostics
            return cached_payload

    executor = _search_executor(server)
    retrieval_started_at = time.perf_counter()
    deadline = retrieval_started_at + min(_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS, 3.2)
    track_candidates: Dict[str, Dict[str, Any]] = {}
    artist_candidates: Dict[str, Dict[str, Any]] = {}
    album_candidates: Dict[str, Dict[str, Any]] = {}
    completed_sources: List[str] = []
    timed_out_sources: List[str] = []

    def resolve_future(future, default, source_name: str):
        if future is None:
            return default
        if _SEARCH_DISABLE_TIMEOUTS:
            try:
                result = future.result()
                completed_sources.append(source_name)
                return result
            except Exception:
                timed_out_sources.append(source_name)
                return default
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            timed_out_sources.append(source_name)
            return default
        try:
            result = future.result(
                timeout=min(1.1, _SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS, max(remaining, 0.05))
            )
            completed_sources.append(source_name)
            return result
        except Exception:
            timed_out_sources.append(source_name)
            return default

    fast_track_future = executor.submit(
        search_tracks_direct,
        query,
        max(limit * 2, 18),
        server=server,
    )
    fast_artist_future = executor.submit(
        search_artists_direct_cached,
        query,
        max(limit, 8),
        server=server,
    )
    fast_album_future = executor.submit(
        search_albums_direct,
        query,
        max(limit, 8),
        server=server,
    )

    fast_tracks = resolve_future(fast_track_future, [], "tracks.fast")
    fast_artists = resolve_future(fast_artist_future, [], "artists.fast")
    fast_albums = resolve_future(fast_album_future, [], "albums.fast")

    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=fast_tracks,
        source_name="fast_query",
        base_score=4.3,
    )
    _collect_artist_candidates(
        artist_candidates,
        artists=fast_artists,
        source_name="fast_artist",
        base_score=4.0,
    )
    _collect_album_candidates(
        album_candidates,
        albums=fast_albums,
        source_name="fast_album",
        base_score=3.9,
    )

    anchor_tracks = semantic_search_anchor_tracks(
        legacy_req,
        fast_tracks,
        fast_tracks,
        limit=4,
        server=server,
    )
    anchor_artist_names = semantic_search_anchor_artist_names(anchor_tracks, 6, server=server)
    normalized_anchor_artists = {
        server.normalize_text(name)
        for name in anchor_artist_names
        if server.normalize_text(name)
    }

    payload = {
        "query_intent": classify_query_intent(
            server=server,
            query=query,
            tracks=fast_tracks[:6],
            artists=fast_artists[:6],
            albums=fast_albums[:6],
        ),
        "track_candidates": track_candidates,
        "artist_candidates": artist_candidates,
        "album_candidates": album_candidates,
        "anchor_tracks": anchor_tracks,
        "anchor_artist_names": anchor_artist_names,
        "normalized_anchor_artists": normalized_anchor_artists,
        "retriever_counts": {
            "track_candidates": len(track_candidates),
            "artist_candidates": len(artist_candidates),
            "album_candidates": len(album_candidates),
        },
        "retrieval_diagnostics": {
            "mode": "fast_query_fallback",
            "cache_hit": False,
            "partial_completion": bool(
                track_candidates or artist_candidates or album_candidates
            ),
            "retrieval_ms": int((time.perf_counter() - retrieval_started_at) * 1000),
            "deadline_ms": int(min(_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS, 3.2) * 1000),
            "timeouts_disabled": bool(_SEARCH_DISABLE_TIMEOUTS),
            "completed_sources": completed_sources,
            "timed_out_sources": timed_out_sources,
        },
    }
    if cache_key:
        _retrieval_cache_set(cache_key, payload)
    return payload


def retrieve_search_candidates(
    legacy_req,
    profile,
    *,
    limit: int,
    server: Any | None = None,
) -> Dict[str, Any]:
    server = adapt_domain_server(server)
    query = trim_text(legacy_req.query)
    cache_key = ""
    if not bool(getattr(legacy_req, "force_refresh", False)):
        cache_key = _retrieval_cache_key(legacy_req, profile, limit, server=server)
        cached_payload = _retrieval_cache_get(cache_key)
        if cached_payload is not None:
            diagnostics = dict(cached_payload.get("retrieval_diagnostics") or {})
            diagnostics["cache_hit"] = True
            cached_payload["retrieval_diagnostics"] = diagnostics
            return cached_payload
    collaborative = profile.get("collaborative") or {}
    executor = _search_executor(server)
    retrieval_started_at = time.perf_counter()
    deadline = retrieval_started_at + _SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS

    track_candidates: Dict[str, Dict[str, Any]] = {}
    artist_candidates: Dict[str, Dict[str, Any]] = {}
    album_candidates: Dict[str, Dict[str, Any]] = {}
    timed_out_sources: List[str] = []
    completed_sources: List[str] = []

    def resolve_future(future, default, source_name: str):
        if future is None:
            return default
        if _SEARCH_DISABLE_TIMEOUTS:
            try:
                result = future.result()
                completed_sources.append(source_name)
                return result
            except Exception:
                timed_out_sources.append(source_name)
                return default
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            timed_out_sources.append(source_name)
            return default
        try:
            result = future.result(
                timeout=min(_SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS, max(remaining, 0.05))
            )
            completed_sources.append(source_name)
            return result
        except Exception:
            timed_out_sources.append(source_name)
            return default

    lexical_tracks_future = executor.submit(
        resolve_ytmusic_song_search,
        server,
        query,
        max(limit * 2, 24),
    )
    blended_tracks_future = executor.submit(
        search_tracks_blended,
        query,
        max(limit * 2, 24),
        server=server,
    )
    lexical_tracks = resolve_future(lexical_tracks_future, [], "tracks.lexical")
    blended_tracks = resolve_future(blended_tracks_future, [], "tracks.blended")
    anchor_tracks = semantic_search_anchor_tracks(
        legacy_req,
        lexical_tracks,
        blended_tracks,
        limit=4,
        server=server,
    )
    anchor_artist_names = semantic_search_anchor_artist_names(anchor_tracks, 6, server=server)
    normalized_anchor_artists = {
        server.normalize_text(name)
        for name in anchor_artist_names
        if server.normalize_text(name)
    }

    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=lexical_tracks,
        source_name="lexical",
        base_score=4.2,
    )
    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=blended_tracks,
        source_name="blended",
        base_score=4.8,
    )

    graph_track_ids = list(collaborative.get("candidate_track_ids") or [])[: max(limit * 2, 24)]
    graph_tracks_future = executor.submit(
        server.recommendation_fetch_tracks_for_ids,
        graph_track_ids,
        min(len(graph_track_ids), 32),
    )
    graph_tracks = resolve_future(graph_tracks_future, [], "tracks.graph")
    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=graph_tracks,
        source_name="graph",
        base_score=3.6,
    )

    context_tracks: List[Dict[str, Any]] = []
    context_query_futures = {
        recent_query: executor.submit(search_tracks_blended, recent_query, 6, server=server)
        for recent_query in (profile.get("recent_queries") or [])[:2]
    }
    context_track_source_futures = {
        index: executor.submit(candidate_sources_for_track, track, server=server)
        for index, track in enumerate((profile.get("last_played_tracks") or [])[:2])
    }
    for recent_query in (profile.get("recent_queries") or [])[:2]:
        context_tracks.extend(
            resolve_future(
                context_query_futures.get(recent_query),
                [],
                f"tracks.context_query:{recent_query}",
            )
        )
    for index in range(len((profile.get("last_played_tracks") or [])[:2])):
        for source_name, source_tracks, _base_score in resolve_future(
            context_track_source_futures.get(index),
            [],
            f"tracks.context_track:{index}",
        ):
            if source_name == "artist_seed":
                context_tracks.extend(source_tracks[:4])
    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=context_tracks,
        source_name="context",
        base_score=3.0,
    )

    anchor_similar_futures = {}
    for anchor_index, anchor_track in enumerate(anchor_tracks[:2]):
        anchor_track_id = trim_text(anchor_track.get("id"))
        if not anchor_track_id:
            continue
        anchor_similar_futures[anchor_index] = executor.submit(
            server.assistant_tool_get_similar_tracks,
            anchor_track_id,
            8,
        )
    for anchor_index in sorted(anchor_similar_futures):
        similar_tracks = resolve_future(
            anchor_similar_futures.get(anchor_index),
            [],
            f"tracks.anchor_neighbor:{anchor_index}",
        )
        _collect_track_candidates(
            track_candidates,
            server=server,
            tracks=similar_tracks,
            source_name="anchor_neighbor",
            base_score=4.6,
        )

    artist_seed_tracks: List[Dict[str, Any]] = []
    artist_seed_names = unique_strings(
        [
            *anchor_artist_names,
            *[
                artist_name
                for artist_name, _score in resolve_artist_names_from_track_query(server, query, 4)
            ],
            *(profile.get("top_artists") or [])[:2],
        ],
        8,
    )
    artist_seed_futures = {
        artist_name: executor.submit(search_artist_seed_tracks, artist_name, 6, server=server)
        for artist_name in artist_seed_names[:4]
    }
    for artist_name in artist_seed_names[:4]:
        artist_seed_tracks.extend(
            resolve_future(
                artist_seed_futures.get(artist_name),
                [],
                f"tracks.artist_seed:{artist_name}",
            )
        )
    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=artist_seed_tracks,
        source_name="artist_seed",
        base_score=3.7,
    )

    lexical_artists_future = executor.submit(
        resolve_search_artists_direct,
        server,
        query,
        max(limit, 12),
    )
    broad_artists_future = executor.submit(
        resolve_search_artists,
        server,
        query,
        max(limit, 12),
    )
    lexical_artists = resolve_future(lexical_artists_future, [], "artists.lexical")
    broad_artists = resolve_future(broad_artists_future, [], "artists.broad")
    query_anchor_artists = []
    query_anchor_artist_names = unique_strings(
        [
            *anchor_artist_names,
            *[
                artist_name
                for artist_name, _score in resolve_artist_names_from_track_query(server, query, 4)
            ],
        ],
        6,
    )
    query_anchor_artist_futures = {
        artist_name: executor.submit(resolve_search_artists_direct, server, artist_name, 2)
        for artist_name in query_anchor_artist_names
    }
    for artist_name in query_anchor_artist_names:
        query_anchor_artists.extend(
            resolve_future(
                query_anchor_artist_futures.get(artist_name),
                [],
                f"artists.anchor_query:{artist_name}",
            )
        )
    _collect_artist_candidates(
        artist_candidates,
        artists=lexical_artists,
        source_name="lexical",
        base_score=4.0,
    )
    _collect_artist_candidates(
        artist_candidates,
        artists=broad_artists,
        source_name="broad",
        base_score=4.8,
    )
    _collect_artist_candidates(
        artist_candidates,
        artists=query_anchor_artists,
        source_name="anchor_query",
        base_score=4.9,
    )

    graph_artist_scores = collaborative.get("artist_scores") or {}
    graph_artist_names = [
        artist_name
        for artist_name, _score in sorted(
            graph_artist_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:6]
    ]
    graph_artists = []
    graph_artist_futures = {
        artist_name: executor.submit(resolve_search_artists_direct, server, artist_name, 1)
        for artist_name in graph_artist_names
    }
    for artist_name in graph_artist_names:
        graph_artists.extend(
            resolve_future(
                graph_artist_futures.get(artist_name),
                [],
                f"artists.graph:{artist_name}",
            )
        )
    _collect_artist_candidates(
        artist_candidates,
        artists=graph_artists,
        source_name="graph",
        base_score=3.3,
    )

    context_artists = []
    context_artist_names = unique_strings(
        [*anchor_artist_names, *(profile.get("top_artists") or []), *(profile.get("artist_hints") or [])],
        8,
    )
    context_artist_futures = {
        artist_name: executor.submit(resolve_search_artists, server, artist_name, 2)
        for artist_name in context_artist_names
    }
    for artist_name in context_artist_names:
        context_artists.extend(
            resolve_future(
                context_artist_futures.get(artist_name),
                [],
                f"artists.context:{artist_name}",
            )
        )
    _collect_artist_candidates(
        artist_candidates,
        artists=context_artists,
        source_name="context",
        base_score=3.0,
    )

    related_artists = []
    related_artist_direct_futures = {
        artist_name: executor.submit(resolve_search_artists_direct, server, artist_name, 1)
        for artist_name in anchor_artist_names[:3]
    }
    related_artist_detail_futures = {}
    for artist_name in anchor_artist_names[:3]:
        direct = resolve_future(
            related_artist_direct_futures.get(artist_name),
            [],
            f"artists.anchor_related_lookup:{artist_name}",
        )
        if not direct:
            continue
        artist_id = trim_text(direct[0].get("id"))
        if not artist_id:
            continue
        related_artist_detail_futures[artist_name] = executor.submit(
            server.build_artist_details_payload,
            artist_id,
            enrich_related=True,
        )
    for artist_name in anchor_artist_names[:3]:
        payload = resolve_future(
            related_artist_detail_futures.get(artist_name),
            {},
            f"artists.anchor_related:{artist_name}",
        )
        related_artists.extend((payload.get("related_artists") or [])[:5])
    _collect_artist_candidates(
        artist_candidates,
        artists=related_artists,
        source_name="anchor_related",
        base_score=4.4,
    )

    lexical_albums_future = executor.submit(
        search_albums_blended,
        query,
        max(limit, 12),
        server=server,
    )
    broad_albums_future = executor.submit(
        server.assistant_tool_search_albums,
        query,
        max(limit, 12),
    )
    lexical_albums = resolve_future(lexical_albums_future, [], "albums.lexical")
    broad_albums = resolve_future(broad_albums_future, [], "albums.broad")
    _collect_album_candidates(
        album_candidates,
        albums=lexical_albums,
        source_name="lexical",
        base_score=3.8,
    )
    _collect_album_candidates(
        album_candidates,
        albums=broad_albums,
        source_name="broad",
        base_score=4.4,
    )

    anchor_album_hints = []
    for track in anchor_tracks[:3]:
        anchor_album_hints.extend(
            album_candidates_for_track(
                track,
                limit=1,
                include_search=False,
                server=server,
            )
        )
    _collect_album_candidates(
        album_candidates,
        albums=anchor_album_hints,
        source_name="anchor_track",
        base_score=4.6,
    )

    context_albums = []
    context_album_track_futures = {
        index: executor.submit(album_candidates_for_track, track, limit=2, server=server)
        for index, track in enumerate(anchor_tracks[:2])
    }
    context_album_artist_futures = {
        artist_name: executor.submit(server.assistant_tool_search_albums, f"{artist_name} album", 3)
        for artist_name in (profile.get("top_artists") or [])[:3]
    }
    for index in range(len(anchor_tracks[:2])):
        context_albums.extend(
            resolve_future(
                context_album_track_futures.get(index),
                [],
                f"albums.context_track:{index}",
            )
        )
    for artist_name in (profile.get("top_artists") or [])[:3]:
        context_albums.extend(
            resolve_future(
                context_album_artist_futures.get(artist_name),
                [],
                f"albums.context_artist:{artist_name}",
            )
        )
    _collect_album_candidates(
        album_candidates,
        albums=context_albums,
        source_name="context",
        base_score=3.1,
    )

    ranked_tracks = [entry["payload"] for entry in track_candidates.values()]
    ranked_artists = [entry["payload"] for entry in artist_candidates.values()]
    ranked_albums = [entry["payload"] for entry in album_candidates.values()]
    query_intent = classify_query_intent(
        query=query,
        tracks=ranked_tracks,
        artists=ranked_artists,
        albums=ranked_albums,
    )

    payload = {
        "query_intent": query_intent,
        "track_candidates": track_candidates,
        "artist_candidates": artist_candidates,
        "album_candidates": album_candidates,
        "anchor_tracks": anchor_tracks,
        "anchor_artist_names": anchor_artist_names,
        "normalized_anchor_artists": normalized_anchor_artists,
        "retriever_counts": {
            "tracks": {
                "lexical": len(lexical_tracks),
                "blended": len(blended_tracks),
                "graph": len(graph_tracks),
                "context": len(context_tracks),
                "artist_seed": len(artist_seed_tracks),
            },
            "artists": {
                "lexical": len(lexical_artists),
                "broad": len(broad_artists),
                "anchor_query": len(query_anchor_artists),
                "graph": len(graph_artists),
                "context": len(context_artists) + len(related_artists),
            },
            "albums": {
                "lexical": len(lexical_albums),
                "broad": len(broad_albums),
                "anchor_track": len(anchor_album_hints),
                "context": len(context_albums),
            },
        },
        "retrieval_diagnostics": {
            "cache_hit": False,
            "partial_completion": bool(timed_out_sources),
            "timed_out_sources": timed_out_sources,
            "completed_sources": completed_sources,
            "retrieval_ms": int((time.perf_counter() - retrieval_started_at) * 1000),
            "deadline_ms": 0 if _SEARCH_DISABLE_TIMEOUTS else int(_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS * 1000),
            "timeouts_disabled": bool(_SEARCH_DISABLE_TIMEOUTS),
        },
    }
    if cache_key:
        _retrieval_cache_set(cache_key, payload)
    return payload
