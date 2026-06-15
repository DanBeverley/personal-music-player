from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
    wait,
)
from threading import Event, Lock, Thread
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
import json
import hashlib
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid

import requests
import yt_dlp
from ytmusicapi import YTMusic

from auralis_backend.contracts import (
    RecommendationInteractionEventRequest,
    RecommendationSearchEventRequest,
    SearchRequest,
)
from auralis_backend.storage.cache_runtime import (
    clear_ttl_namespace as _cache_clear_namespace,
    detail_result_cache,
    detail_result_cache_lock,
    lookup_home_candidates as _cache_lookup_home_candidates,
    lookup_recommendation_track_detail as _cache_lookup_recommendation_track_detail,
    lookup_ttl_cache as _cache_runtime_lookup,
    recommendation_embedding_cache,
    recommendation_embedding_lock,
    search_result_cache,
    search_result_cache_lock,
    store_home_candidates as _cache_store_home_candidates,
    store_recommendation_track_detail as _cache_store_recommendation_track_detail,
    store_ttl_cache as _cache_runtime_store,
    stream_chunk_cache,
    stream_chunk_inflight,
    stream_chunk_inflight_lock,
    stream_chunk_lock,
    stream_info_cache,
    stream_info_inflight,
    stream_info_lock,
)

try:
    import psycopg
except Exception:
    psycopg = None

try:
    from langgraph_assistant import (
        langgraph_runtime_available,
        run_langgraph_assistant,
    )
except Exception as exc:
    def langgraph_runtime_available() -> bool:
        return False

    def run_langgraph_assistant(req: Any, deps: Dict[str, Any]):
        raise RuntimeError(f"LangGraph assistant import failed: {exc}")

from auralis_backend.details.detail_runtime import (
    build_album_details_payload as _detail_build_album_details_payload,
    build_artist_details_payload as _detail_build_artist_details_payload,
    build_track_details_payload as _detail_build_track_details_payload,
)
import auralis_backend.assistant_core_runtime as _assistant_core_runtime
import auralis_backend.assistant_tool_runtime as _assistant_tool_runtime
from auralis_backend.recommend import (
    helper_runtime as _recommendation_helper_runtime,
    maintenance_runtime as _recommendation_maintenance_runtime,
    model_runtime as _recommendation_model_runtime,
    profile_runtime as _recommendation_profile_runtime,
    row_helper_runtime as _recommendation_row_helper_runtime,
    session_runtime as _recommendation_session_runtime,
    store_runtime as _recommendation_store_runtime,
)

try:
    from auralis_backend.domain.ranking import (
        HOME_GLOBAL_DEFAULT_WEIGHTS,
        defaults_for_model as _ranking_defaults_for_model,
        SEARCH_ALBUM_DEFAULT_WEIGHTS,
        SEARCH_ARTIST_DEFAULT_WEIGHTS,
        SEARCH_TRACK_DEFAULT_WEIGHTS,
        model_version as _ranking_model_version,
        score_features as _ranking_score_features,
    )
except Exception:
    SEARCH_TRACK_DEFAULT_WEIGHTS = {
        "source_score": 0.34,
        "retrieval_votes": 0.58,
        "lexical": 0.74,
        "title_lexical": 0.48,
        "query_similarity": 8.0,
        "semantic_query_similarity": 6.0,
        "context_similarity": 2.5,
        "taste_similarity": 1.4,
        "artist_similarity": 2.0,
        "short_similarity": 0.9,
        "long_similarity": 0.5,
        "collab_latent": 5.1,
        "collab_neighbor": 0.9,
        "collab_artist": 0.2,
        "anchor_artist_match": 1.4,
        "popularity": 0.25,
    }
    SEARCH_ARTIST_DEFAULT_WEIGHTS = {
        "source_score": 0.35,
        "retrieval_votes": 0.62,
        "lexical": 0.54,
        "query_similarity": 5.4,
        "semantic_query_similarity": 4.4,
        "context_similarity": 1.8,
        "taste_similarity": 1.1,
        "artist_similarity": 2.6,
        "anchor_artist_similarity": 5.8,
        "anchor_track_similarity": 3.4,
        "collab_artist": 0.34,
    }
    SEARCH_ALBUM_DEFAULT_WEIGHTS = {
        "source_score": 0.3,
        "retrieval_votes": 0.55,
        "lexical": 0.6,
        "query_similarity": 7.6,
        "semantic_query_similarity": 4.2,
        "context_similarity": 1.9,
        "taste_similarity": 1.25,
        "artist_similarity": 1.6,
        "collab_artist": 0.2,
    }
    HOME_GLOBAL_DEFAULT_WEIGHTS = {
        "source_score": 0.36,
        "source_votes": 0.68,
        "taste_similarity": 5.4,
        "short_similarity": 1.8,
        "long_similarity": 1.2,
        "query_similarity": 1.8,
        "artist_similarity": 2.0,
        "anchor_similarity": 2.5,
        "collab_latent": 5.6,
        "collab_neighbor": 1.0,
        "collab_artist": 0.22,
        "offline_bonus": 1.1,
        "library_bonus": 0.65,
        "top_bonus": 1.8,
        "recent_bonus": 0.35,
        "popularity": 0.18,
        "novelty": 0.2,
        "scene_affinity": 1.05,
        "peer_scene_bonus": 0.72,
        "era_affinity": 0.62,
        "adjacent_era_affinity": 0.24,
        "type_affinity": 0.18,
        "script_affinity": 0.12,
    }

    def _ranking_defaults_for_model(model_key: str, fallback: Dict[str, float]) -> Dict[str, float]:
        return dict(fallback or {})

    def _ranking_model_version(model_key: str) -> str:
        return f"local:{model_key}"

    def _ranking_score_features(
        *,
        model_key: str,
        defaults: Dict[str, float],
        features: Dict[str, float],
    ) -> float:
        score = 0.0
        for key, value in features.items():
            score += float(defaults.get(key, 0.0)) * float(value)
        return score

try:
    from auralis_backend.storage.postgres import (
        activate_model_version as _pg_activate_model_version,
        list_model_versions as _pg_list_model_versions,
        list_rollout_events as _pg_list_rollout_events,
        log_request as _pg_log_request,
        rollback_model_version as _pg_rollback_model_version,
    )
except Exception:
    def _pg_log_request(
        *,
        request_id: str,
        request_type: str,
        user_scope_id: str,
        session_id: str = "",
        model_version: str = "",
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        return

    def _pg_list_model_versions(*, model_key: str, limit: int = 20):
        return []

    def _pg_activate_model_version(
        *,
        model_key: str,
        version: str,
        actor: str = "system",
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        return False

    def _pg_rollback_model_version(
        *,
        model_key: str,
        target_version: str = "",
        actor: str = "system",
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        return {"ok": False, "reason": "postgres_unavailable"}

    def _pg_list_rollout_events(*, model_key: str = "", limit: int = 50):
        return []

try:
    from auralis_backend.recommend.precompute import (
        invalidate_user as _nearline_invalidate_user,
        invalidate_user_query as _nearline_invalidate_user_query,
        run_precompute_cycle as _nearline_run_precompute_cycle,
        runtime_snapshot as _nearline_runtime_snapshot,
    )
except Exception:
    def _nearline_invalidate_user(user_scope_id: str) -> None:
        return

    def _nearline_invalidate_user_query(user_scope_id: str, query: str) -> None:
        return

    def _nearline_run_precompute_cycle(*, server=None, force: bool = False) -> Dict[str, Any]:
        return {"enabled": False, "reason": "nearline_precompute_unavailable"}

    def _nearline_runtime_snapshot() -> Dict[str, Any]:
        return {"enabled": False}


def _bind_server(fn):
    def _wrapped(*args, **kwargs):
        return fn(sys.modules[__name__], *args, **kwargs)

    return _wrapped

def extract_thumbnail(data):
    if not data: return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    thumbs = data.get("thumbnails") or data.get("thumbnail")
    if isinstance(thumbs, list) and len(thumbs) > 0:
        return thumbs[-1].get("url")
    elif isinstance(thumbs, dict):
        return thumbs.get("url")
    return None

def extract_artist(data):
    if not data: return "Unknown Artist"
    artists = data.get("artists") or []
    if artists and isinstance(artists, list):
        names = []
        for artist in artists:
            if isinstance(artist, dict):
                name = artist.get("name") or artist.get("artist")
            else:
                name = str(artist or "")
            name = (name or "").strip()
            if name:
                names.append(name)
        if names:
            return ", ".join(names)
    subtitles = data.get("subtitle") or data.get("byline") or data.get("bylineText")
    if isinstance(subtitles, str) and subtitles.strip():
        return subtitles.strip()
    if isinstance(subtitles, list):
        names = []
        for subtitle in subtitles:
            if isinstance(subtitle, dict):
                name = subtitle.get("text") or subtitle.get("name")
            else:
                name = str(subtitle or "")
            name = (name or "").strip()
            if name:
                names.append(name)
        if names:
            return ", ".join(names)
    direct_artist = (
        data.get("artist")
        or data.get("channel")
        or data.get("channelName")
        or data.get("ownerChannelName")
        or data.get("author")
        or data.get("uploader")
    )
    if isinstance(direct_artist, dict):
        name = direct_artist.get("name") or direct_artist.get("artist")
        if name:
            return str(name).strip()
    elif isinstance(direct_artist, str) and direct_artist.strip():
        return direct_artist.strip()
    authors = data.get("authors") or []
    if isinstance(authors, list):
        names = []
        for author in authors:
            if isinstance(author, dict):
                name = author.get("name")
            else:
                name = str(author or "")
            name = (name or "").strip()
            if name:
                names.append(name)
        if names:
            return ", ".join(names)
    author = data.get("author")
    if author:
        return author.get("name") if isinstance(author, dict) else str(author)
    uploader = data.get("uploader")
    return uploader if uploader else "Unknown Artist"


def extract_artist_names(data):
    if not data:
        return []
    names = []
    seen = set()

    def add_name(raw_name):
        text = (raw_name or "").strip()
        normalized = _normalize_text(text)
        if not text or normalized == "unknown artist" or normalized in seen:
            return
        seen.add(normalized)
        names.append(text)

    artists = data.get("artists") or []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict):
                add_name(artist.get("name") or artist.get("artist"))
            else:
                add_name(str(artist or ""))

    direct_artist = (
        data.get("artist")
        or data.get("channel")
        or data.get("channelName")
        or data.get("ownerChannelName")
        or data.get("author")
        or data.get("uploader")
    )
    if isinstance(direct_artist, dict):
        add_name(direct_artist.get("name") or direct_artist.get("artist"))
    else:
        add_name(str(direct_artist or ""))

    if not names:
        add_name(extract_artist(data))
    return names

def extract_album_info(data):
    if not data:
        return None

    album = data.get("album")
    if isinstance(album, dict):
        title = album.get("name") or album.get("title")
        album_id = album.get("id") or album.get("browseId")
        if title:
            return {"id": album_id, "title": title}
    elif isinstance(album, str) and album.strip():
        return {"id": None, "title": album.strip()}

    albums = data.get("albums")
    if isinstance(albums, list) and albums:
        first = albums[0]
        if isinstance(first, dict):
            title = first.get("name") or first.get("title")
            album_id = first.get("id") or first.get("browseId")
            if title:
                return {"id": album_id, "title": title}
        elif isinstance(first, str) and first.strip():
            return {"id": None, "title": first.strip()}
    return None

def normalize_album_results(raw_results):
    albums = []
    seen = set()
    for entry in raw_results or []:
        result_type = (entry.get("resultType") or entry.get("type") or "").lower()
        browse_id = entry.get("browseId") or entry.get("id")
        if result_type and result_type != "album":
            continue
        if not browse_id or browse_id in seen:
            continue
        seen.add(browse_id)
        albums.append({
            "id": browse_id,
            "title": entry.get("title"),
            "artist": extract_artist(entry),
            "thumbnail": extract_thumbnail(entry),
            "year": entry.get("year") or "",
            "track_count": entry.get("trackCount") or entry.get("track_count") or 0,
        })
    return albums


def normalize_artist_results(raw_results):
    artists = []
    seen = set()
    for entry in raw_results or []:
        result_type = (entry.get("resultType") or entry.get("type") or "").lower()
        browse_id = entry.get("browseId") or entry.get("id")
        name = (
            entry.get("artist")
            or entry.get("name")
            or entry.get("title")
            or extract_artist(entry)
        )
        if result_type and result_type not in {"artist", "artists"}:
            continue
        if not browse_id or browse_id in seen or not name:
            continue
        seen.add(browse_id)
        artists.append(
            {
                "id": browse_id,
                "name": name,
                "thumbnail": extract_thumbnail(entry),
                "description": (
                    entry.get("subscribers")
                    or entry.get("description")
                    or entry.get("type")
                    or ""
                ),
            }
        )
    return artists


def _normalize_artist_song_entries(raw_results, fallback_artist: str = ""):
    songs = []
    seen = set()
    for entry in raw_results or []:
        normalized = _normalize_song_result(entry)
        if not normalized:
            continue
        track_id = normalized.get("id")
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        if fallback_artist and not (normalized.get("channel") or "").strip():
            normalized["channel"] = fallback_artist
        songs.append(normalized)
    return songs


def _normalize_artist_album_entries(raw_results, fallback_artist: str = ""):
    albums = []
    seen = set()
    for entry in raw_results or []:
        browse_id = entry.get("browseId") or entry.get("id")
        title = entry.get("title") or entry.get("name")
        if not browse_id or not title or browse_id in seen:
            continue
        seen.add(browse_id)
        albums.append(
            {
                "id": browse_id,
                "title": title,
                "artist": extract_artist(entry) or fallback_artist,
                "thumbnail": extract_thumbnail(entry),
                "year": entry.get("year") or "",
                "track_count": entry.get("trackCount") or entry.get("track_count") or 0,
            }
        )
    return albums


def _normalize_artist_stats(payload):
    stats = []
    for label, value in (
        ("Monthly listeners", payload.get("monthlyListeners")),
        ("Subscribers", payload.get("subscribers")),
        ("Views", payload.get("views")),
    ):
        text = (value or "").strip()
        if not text:
            continue
        stats.append({"label": label, "value": text})
    return stats


_ARTIST_RELATED_HARD_REJECT_TERMS = (
    "tribute",
    "karaoke",
    "cover",
    "covers",
)

_ARTIST_RELATED_SOFT_PENALTY_TERMS = (
    "cast",
    "soundtrack",
    "orchestra",
    "philharmonic",
    "symphony",
    "ensemble",
    "choir",
)


def _artist_related_name_penalty(seed_name: str, candidate_name: str) -> float:
    normalized_seed = _normalize_text(seed_name)
    normalized_candidate = _normalize_text(candidate_name)
    if not normalized_candidate:
        return 8.0
    if normalized_seed and normalized_candidate == normalized_seed:
        return 12.0

    penalty = 0.0
    if any(term in normalized_candidate for term in _ARTIST_RELATED_HARD_REJECT_TERMS):
        penalty += 4.8
    if any(term in normalized_candidate for term in _ARTIST_RELATED_SOFT_PENALTY_TERMS):
        penalty += 1.1
    return penalty


def _artist_detail_reference_vector(artist_payload: Dict[str, Any], top_songs):
    artist_name = _recommendation_trim_text(artist_payload.get("name"))
    if not artist_name:
        return []
    reference_parts = [_recommendation_artist_text(artist_payload)]
    top_song_titles = [
        _recommendation_trim_text(track.get("title"))
        for track in (top_songs or [])[:4]
        if isinstance(track, dict)
    ]
    top_song_titles = [title for title in top_song_titles if title]
    if top_song_titles:
        reference_parts.append(f"known for {'; '.join(top_song_titles)}")
    reference_text = ". ".join(part for part in reference_parts if part)
    reference_key = _recommendation_text_embedding_key("artist_detail_reference", reference_text)
    if not reference_key:
        return []
    embeddings = _recommendation_embed_entries(
        "artist_detail_reference",
        [(reference_key, reference_text)],
    )
    return embeddings.get(reference_key) or []


def _artist_detail_collaborative_related_artists(
    artist_name: str,
    top_songs,
    *,
    limit: int = 6,
):
    model = _recommendation_get_collaborative_model()
    if not isinstance(model, dict) or not model.get("ready"):
        return []

    query_artist_scores = model.get("query_artist_scores") or {}
    seed_queries = _recommendation_unique_strings(
        [
            artist_name,
            *[
                _recommendation_trim_text(track.get("title"))
                for track in (top_songs or [])[:3]
                if isinstance(track, dict)
            ],
        ],
        4,
    )
    weighted_names = {}
    normalized_seed = _normalize_text(artist_name)
    for query_index, query in enumerate(seed_queries):
        query_decay = max(1.0 - (query_index * 0.12), 0.55)
        for artist_key, score in list(
            (query_artist_scores.get(_normalize_text(query)) or {}).items()
        )[:12]:
            if not artist_key or artist_key == normalized_seed:
                continue
            weighted_names[artist_key] = max(
                weighted_names.get(artist_key, 0.0),
                float(score) * query_decay,
            )

    ranked_names = sorted(
        weighted_names.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:limit]
    if not ranked_names:
        return []

    futures = {
        artist_key: recommendation_executor.submit(
            _assistant_tool_search_artists_direct,
            artist_key,
            2,
        )
        for artist_key, _score in ranked_names
    }
    results = []
    for artist_key, score in ranked_names:
        try:
            direct_results = futures[artist_key].result(timeout=6)
        except Exception:
            direct_results = []
        for index, artist in enumerate(direct_results or []):
            candidate = dict(artist)
            candidate["_seed_score"] = max(float(score) * 0.24, 0.22) + max(
                1.1 - (index * 0.18),
                0.35,
            )
            results.append(candidate)
    return results


def _rank_artist_detail_related_artists(
    artist_payload: Dict[str, Any],
    top_songs,
    raw_related_artists,
    *,
    enrich_related: bool = True,
):
    artist_name = _recommendation_trim_text(artist_payload.get("name"))
    normalized_seed = _normalize_text(artist_name)
    if not artist_name:
        return []

    candidates_by_key = {}

    def add_candidate(raw_artist, source_score: float) -> None:
        if not isinstance(raw_artist, dict):
            return
        artist_id = _recommendation_trim_text(raw_artist.get("id"))
        artist_name_value = _recommendation_trim_text(raw_artist.get("name"))
        normalized_name = _normalize_text(artist_name_value)
        if not artist_name_value or not normalized_name or normalized_name == normalized_seed:
            return
        candidate_key = artist_id or normalized_name
        candidate = candidates_by_key.get(candidate_key)
        if candidate is None:
            candidate = dict(raw_artist)
            candidate["_seed_score"] = 0.0
            candidates_by_key[candidate_key] = candidate
        elif not candidate.get("thumbnail") and raw_artist.get("thumbnail"):
            candidate["thumbnail"] = raw_artist.get("thumbnail")
        candidate["_seed_score"] = max(
            float(candidate.get("_seed_score") or 0.0),
            float(source_score),
        )

    for index, related_artist in enumerate(raw_related_artists or []):
        add_candidate(related_artist, max(2.6 - (index * 0.22), 0.55))

    if enrich_related:
        for collaborative_artist in _artist_detail_collaborative_related_artists(
            artist_name,
            top_songs,
        ):
            add_candidate(
                collaborative_artist,
                float(collaborative_artist.get("_seed_score") or 0.0),
            )

    candidates = list(candidates_by_key.values())
    if not candidates:
        return []

    reference_vector = _artist_detail_reference_vector(artist_payload, top_songs)
    embedding_inputs = [artist_payload, *candidates]
    artist_embeddings = _recommendation_artist_embeddings(embedding_inputs)
    fallback_reference_key = _recommendation_artist_embedding_key(artist_payload)
    if not reference_vector and fallback_reference_key:
        reference_vector = artist_embeddings.get(fallback_reference_key) or []

    ranked = []
    for candidate in candidates:
        candidate_key = _recommendation_artist_embedding_key(candidate)
        candidate_vector = artist_embeddings.get(candidate_key) or []
        similarity = _assistant_cosine_similarity(reference_vector, candidate_vector)
        penalty = _artist_related_name_penalty(artist_name, candidate.get("name") or "")
        ranking_score = float(candidate.get("_seed_score") or 0.0) + (similarity * 5.4) - penalty
        if ranking_score <= 0.15:
            continue
        candidate_copy = dict(candidate)
        candidate_copy["score"] = round(ranking_score, 3)
        candidate_copy["ml_similarities"] = {
            "artist_context": round(similarity, 4),
        }
        candidate_copy.pop("_seed_score", None)
        ranked.append(candidate_copy)

    ranked.sort(
        key=lambda item: (
            item.get("score", 0.0),
            len(_normalize_text(item.get("name") or "")),
        ),
        reverse=True,
    )
    return ranked[:12]


def _build_artist_details_payload(artist_id: str, *, enrich_related: bool = True):
    return _detail_build_artist_details_payload(
        sys.modules[__name__],
        artist_id,
        enrich_related=enrich_related,
    )

def _recommended_artist_anchor_penalty(anchor_names, candidate_name: str) -> float:
    normalized_candidate = _normalize_text(candidate_name)
    if not normalized_candidate:
        return 0.0
    penalty = 0.0
    for anchor_name in anchor_names or []:
        normalized_anchor = _normalize_text(anchor_name)
        if not normalized_anchor:
            continue
        if normalized_candidate == normalized_anchor:
            return 8.0
        penalty = max(penalty, _artist_related_name_penalty(anchor_name, candidate_name))
        if (
            normalized_candidate.startswith(normalized_anchor)
            or normalized_anchor.startswith(normalized_candidate)
        ):
            penalty = max(penalty, 1.6)
    return penalty


def _recommended_artist_reference_vectors(anchor_tracks, anchor_artist_names):
    anchor_track_snapshots = _recommendation_unique_snapshot_tracks(anchor_tracks, 6)
    track_embeddings = _recommendation_track_embeddings(anchor_track_snapshots)
    anchor_track_vectors = []
    for index, track in enumerate(anchor_track_snapshots):
        track_key = _recommendation_track_embedding_key(track)
        track_vector = track_embeddings.get(track_key) or []
        if not track_vector:
            continue
        anchor_track_vectors.append((track_vector, max(2.1 - (index * 0.22), 0.65)))

    anchor_artist_entries = []
    for index, artist_name in enumerate(
        _recommendation_unique_strings(anchor_artist_names, 6)
    ):
        text = f"artist {artist_name}"
        key = _recommendation_text_embedding_key(
            "recommended_artist_anchor",
            text,
        )
        anchor_artist_entries.append((key, text, max(2.0 - (index * 0.18), 0.6)))

    artist_embeddings = _recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in anchor_artist_entries],
    )
    anchor_artist_vectors = []
    for key, _text, weight in anchor_artist_entries:
        artist_vector = artist_embeddings.get(key) or []
        if not artist_vector:
            continue
        anchor_artist_vectors.append((artist_vector, weight))

    return {
        "anchor_track_vector": _vector_weighted_average(anchor_track_vectors),
        "anchor_artist_vector": _vector_weighted_average(anchor_artist_vectors),
    }


def _recommended_artists_payload(req: SearchRequest):
    cache_key = _recommended_artists_cache_key(req)
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "recommended_artists",
        cache_key,
    )
    if cached is not None:
        return {
            "status": "success",
            "artists": [dict(item) for item in cached[: max(1, min(req.limit or 8, 12))]],
        }

    surface = _recommendation_trim_text(req.surface or "home_feed") or "home_feed"
    profile = _recommendation_build_profile(req)
    profile_vectors = profile.get("vectors") or {}
    collaborative = profile.get("collaborative") or {}
    listened_artist_names = {
        _normalize_text(name)
        for name in (profile.get("listened_artists") or [])
        if _normalize_text(name)
    }
    query_seeds = _recommendation_unique_strings(
        [
            req.query,
            *(req.recent_queries or []),
            *(req.taste_queries or []),
        ],
        8,
    )
    anchor_tracks = _recommendation_unique_snapshot_tracks(
        req.anchor_track_snapshots or profile.get("anchor_track_snapshots") or [],
        6,
    )
    anchor_artist_names = _recommendation_unique_strings(
        [
            *(req.anchor_artist_hints or []),
            *(profile.get("anchor_artist_hints") or []),
            *(
                artist_name
                for track in anchor_tracks
                for artist_name in extract_artist_names(track)
            ),
        ],
        8,
    )
    weighted_artist_names = {}

    def add_artist_seed(raw_name: Optional[str], weight: float) -> None:
        text = (raw_name or "").strip()
        normalized = _normalize_text(text)
        if not text or not normalized:
            return
        weighted_artist_names[normalized] = max(
            weighted_artist_names.get(normalized, 0.0),
            weight,
        )

    def add_track_artist_seeds(tracks, base_weight: float) -> None:
        for index, track in enumerate(tracks or []):
            for artist_name in extract_artist_names(track):
                add_artist_seed(
                    artist_name,
                    max(base_weight - (index * 0.12), 0.45),
                )

    if surface == "search_results":
        for index, anchor_artist in enumerate(anchor_artist_names):
            add_artist_seed(anchor_artist, max(5.0 - (index * 0.35), 2.0))
        add_track_artist_seeds(anchor_tracks, 4.6)
        if not weighted_artist_names:
            for query in query_seeds:
                for artist_name, score in _artist_names_from_track_query(query, 3):
                    add_artist_seed(artist_name, score + 1.8)
        if not weighted_artist_names:
            for index, item in enumerate(
                sorted(
                    (collaborative.get("artist_scores") or {}).items(),
                    key=lambda entry: entry[1],
                    reverse=True,
                )[:6]
            ):
                artist_key, score = item
                if not artist_key:
                    continue
                add_artist_seed(
                    artist_key,
                    max(float(score) * 0.6, 1.0) - (index * 0.08),
                )
    else:
        for index, artist_hint in enumerate(profile.get("artist_hints") or []):
            add_artist_seed(artist_hint, max(3.8 - (index * 0.18), 1.6))
        add_track_artist_seeds(profile.get("last_played_tracks"), 4.4)
        add_track_artist_seeds(profile.get("top_track_snapshots"), 3.5)
        add_track_artist_seeds(profile.get("recent_track_snapshots"), 3.0)
        for query in query_seeds:
            for artist_name, score in _artist_names_from_track_query(query, 3):
                add_artist_seed(artist_name, score + 1.25)
        for index, item in enumerate(
            sorted(
                (collaborative.get("artist_scores") or {}).items(),
                key=lambda entry: entry[1],
                reverse=True,
            )[:6]
        ):
            artist_key, score = item
            if not artist_key:
                continue
            add_artist_seed(
                artist_key,
                max(float(score) * 0.55, 1.05) - (index * 0.08),
            )

    ranked_seed_names = sorted(
        weighted_artist_names.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    top_seed_names = ranked_seed_names[: (5 if surface == "search_results" else 4)]

    artists = []
    seen_artist_ids = set()
    seen_artist_names = set()
    excluded_artist_names = {
        _normalize_text(name)
        for name in anchor_artist_names
        if _normalize_text(name)
    } if surface == "search_results" else set()

    def add_artist_result(raw_artist, score: float):
        artist_id = (raw_artist.get("id") or "").strip()
        artist_name = (raw_artist.get("name") or "").strip()
        normalized_name = _normalize_text(artist_name)
        if (
            not artist_id
            or not artist_name
            or artist_id in seen_artist_ids
            or normalized_name in seen_artist_names
            or (excluded_artist_names and normalized_name in excluded_artist_names)
        ):
            return
        artist = dict(raw_artist)
        artist["score"] = round(score, 3)
        seen_artist_ids.add(artist_id)
        seen_artist_names.add(normalized_name)
        artists.append(artist)

    direct_search_limit = 3 if surface == "search_results" else 2
    direct_seed_futures = {
        seed_index: recommendation_executor.submit(
            _assistant_tool_search_artists_direct,
            seed_name,
            direct_search_limit,
        )
        for seed_index, (seed_name, _seed_weight) in enumerate(top_seed_names)
    }
    semantic_seed_futures = {}
    if surface == "search_results":
        semantic_seed_futures = {
            seed_index: recommendation_executor.submit(
                _assistant_tool_search_artists,
                seed_name,
                4,
            )
            for seed_index, (seed_name, _seed_weight) in enumerate(top_seed_names[:2])
        }

    direct_results_by_seed = {}
    for seed_index, future in direct_seed_futures.items():
        try:
            direct_results_by_seed[seed_index] = future.result(timeout=8)
        except Exception:
            direct_results_by_seed[seed_index] = []

    semantic_results_by_seed = {}
    for seed_index, future in semantic_seed_futures.items():
        try:
            semantic_results_by_seed[seed_index] = future.result(timeout=8)
        except Exception:
            semantic_results_by_seed[seed_index] = []

    related_artist_futures = {}
    for seed_index, direct_results in direct_results_by_seed.items():
        if not direct_results:
            continue
        if surface == "search_results" and seed_index >= 3:
            continue
        if surface != "search_results" and seed_index >= 2:
            continue
        primary_artist_id = (direct_results[0].get("id") or "").strip()
        if not primary_artist_id:
            continue
        related_artist_futures[seed_index] = recommendation_executor.submit(
            _build_artist_details_payload,
            primary_artist_id,
            enrich_related=(surface == "search_results"),
        )

    for seed_index, (_seed_name, seed_weight) in enumerate(top_seed_names):
        direct_results = direct_results_by_seed.get(seed_index) or []
        for index, artist in enumerate(direct_results):
            add_artist_result(
                artist,
                seed_weight + max(2.0 - (index * 0.28), 0.7),
            )
        for index, artist in enumerate(semantic_results_by_seed.get(seed_index) or []):
            add_artist_result(
                artist,
                max(seed_weight - 0.35 - (index * 0.18), 0.45),
            )
        related_future = related_artist_futures.get(seed_index)
        if related_future is not None:
            try:
                artist_payload = related_future.result(timeout=10)
            except Exception:
                artist_payload = {}
            related_limit = 6 if surface == "search_results" else 4
            for index, related in enumerate(
                (artist_payload.get("related_artists") or [])[:related_limit]
            ):
                add_artist_result(
                    related,
                    max(seed_weight - 0.55 - (index * 0.15), 0.4),
                )

    if not artists and surface == "search_results":
        for query in query_seeds[:2]:
            for index, artist in enumerate(_assistant_tool_search_artists(query, 4)):
                add_artist_result(artist, max(1.25 - (index * 0.18), 0.35))

    reference_vectors = _recommended_artist_reference_vectors(
        anchor_tracks if surface == "search_results" else [],
        anchor_artist_names
        if surface == "search_results"
        else (profile.get("top_artists") or [])[:6],
    )
    artist_embeddings = _recommendation_artist_embeddings(artists)
    ranked_artists = []
    for artist in artists:
        artist_key = _recommendation_artist_embedding_key(artist)
        artist_vector = artist_embeddings.get(artist_key) or []
        seed_score = float(artist.get("score") or 0.0) * 0.45
        similarities = {
            "taste": _assistant_cosine_similarity(
                artist_vector,
                profile_vectors.get("taste_vector") or [],
            ),
            "artist": _assistant_cosine_similarity(
                artist_vector,
                profile_vectors.get("artist_vector") or [],
            ),
            "query": _assistant_cosine_similarity(
                artist_vector,
                profile_vectors.get("query_vector") or [],
            ),
            "short": _assistant_cosine_similarity(
                artist_vector,
                profile_vectors.get("short_term_vector") or [],
            ),
            "long": _assistant_cosine_similarity(
                artist_vector,
                profile_vectors.get("long_term_vector") or [],
            ),
            "anchor_artist": _assistant_cosine_similarity(
                artist_vector,
                reference_vectors.get("anchor_artist_vector") or [],
            ),
            "anchor_track": _assistant_cosine_similarity(
                artist_vector,
                reference_vectors.get("anchor_track_vector") or [],
            ),
        }
        normalized_name = _normalize_text(artist.get("name") or "")
        collaborative_score = float(
            (collaborative.get("artist_scores") or {}).get(normalized_name) or 0.0
        )
        if surface == "search_results":
            ranking_score = (
                (seed_score * 0.38)
                + (similarities["anchor_artist"] * 6.2)
                + (similarities["anchor_track"] * 4.4)
                + (similarities["artist"] * 2.0)
                + (similarities["query"] * 1.2)
                + (similarities["taste"] * 0.9)
                + (similarities["short"] * 0.6)
                + (collaborative_score * 0.18)
            )
            ranking_score -= _recommended_artist_anchor_penalty(
                anchor_artist_names,
                artist.get("name") or "",
            )
        else:
            ranking_score = (
                seed_score
                + (similarities["taste"] * 5.1)
                + (similarities["artist"] * 4.7)
                + (similarities["query"] * 1.9)
                + (similarities["short"] * 1.3)
                + (similarities["long"] * 1.1)
                + (max(similarities["anchor_artist"], similarities["anchor_track"]) * 0.75)
            )
            ranking_score += collaborative_score * 0.4
            if normalized_name in listened_artist_names:
                ranking_score -= 0.9
        artist["score"] = round(ranking_score, 3)
        artist["ml_similarities"] = {
            name: round(value, 4)
            for name, value in similarities.items()
        }
        ranked_artists.append(artist)

    ranked_artists.sort(
        key=lambda item: (
            item.get("score", 0),
            len(_normalize_text(item.get("name") or "")),
        ),
        reverse=True,
    )
    limit = max(1, min(req.limit or 8, 12))
    results = ranked_artists[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "recommended_artists",
        cache_key,
        results,
        RECOMMENDED_ARTISTS_CACHE_TTL_SECONDS,
    )
    return {"status": "success", "artists": [dict(item) for item in results]}

def _normalize_song_result(entry):
    from auralis_backend.search.upstream_runtime import normalize_song_result

    return normalize_song_result(sys.modules[__name__], entry)

def _ytmusic_song_search(query: str, limit: int):
    from auralis_backend.search.upstream_runtime import ytmusic_song_search

    return ytmusic_song_search(sys.modules[__name__], query, limit)

def _ytdlp_song_search(query: str, limit: int):
    from auralis_backend.search.upstream_runtime import ytdlp_song_search

    return ytdlp_song_search(sys.modules[__name__], query, limit)

def lookup_album_for_song(video_id: str, title: str, artist: str):
    candidates = []
    raw_results = _upstream_call_with_retry(
        lambda: ytmusic.search(f"{title} {artist}".strip(), filter="songs", limit=6),
        attempts=UPSTREAM_RETRY_ATTEMPTS,
        backoff_seconds=UPSTREAM_RETRY_BACKOFF_SECONDS,
        default=[],
    )

    for entry in raw_results:
        album_info = extract_album_info(entry)
        if not album_info or not album_info.get("title"):
            continue
        score = 0
        if entry.get("videoId") == video_id:
            score += 4
        if title and entry.get("title", "").strip().lower() == title.strip().lower():
            score += 2
        entry_artist = extract_artist(entry)
        if artist and entry_artist and artist.strip().lower() in entry_artist.strip().lower():
            score += 2
        if album_info.get("id"):
            score += 1
        candidates.append((score, album_info))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def parse_duration_seconds(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        parts = [int(part) for part in text.split(":") if part.isdigit()]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

def normalize_recommendation_track(data):
    if not data:
        return None
    video_id = data.get("videoId") or data.get("video_id") or data.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(data) or {}
    title = (
        data.get("title")
        or data.get("name")
        or data.get("track")
        or data.get("song")
        or "Unknown Track"
    )
    artist = extract_artist(data)
    return {
        "id": video_id,
        "title": title,
        "duration": parse_duration_seconds(
            data.get("duration_seconds")
            or data.get("lengthSeconds")
            or data.get("length")
            or data.get("duration")
        ),
        "thumbnail": extract_thumbnail(data),
        "artist": artist,
        "channel": artist,
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())

def _query_tokens(query: str):
    return [
        token
        for token in re.split(r"[^a-z0-9]+", _normalize_text(query))
        if len(token) >= 3
    ]

def _ollama_headers():
    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    return headers

def _call_ollama_chat(
    messages,
    *,
    schema=None,
    temperature=0.2,
    timeout_seconds=None,
    model_override=None,
):
    payload = {
        "model": (model_override or OLLAMA_MODEL),
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if OLLAMA_KEEP_ALIVE:
        payload["keep_alive"] = OLLAMA_KEEP_ALIVE
    if schema is not None:
        payload["format"] = schema

    response = ollama_http.post(
        f"{OLLAMA_BASE_URL}/chat",
        headers=_ollama_headers(),
        json=payload,
        timeout=(
            OLLAMA_CONNECT_TIMEOUT_SECONDS,
            timeout_seconds or OLLAMA_READ_TIMEOUT_SECONDS,
        ),
    )
    response.raise_for_status()
    data = response.json()
    return ((data.get("message") or {}).get("content") or "").strip()

def _extract_json_from_llm_text(raw: str):
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty model response")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        candidate = text[object_start : object_end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        candidate = text[array_start : array_end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raise ValueError(f"Model did not return valid JSON: {text[:200]}")

def _call_ollama_structured(
    messages,
    *,
    schema,
    temperature=0.2,
    max_attempts=3,
    timeout_seconds=None,
    model_override=None,
):
    last_error = None
    for attempt in range(max_attempts):
        attempt_messages = list(messages)
        if attempt > 0:
            attempt_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous response was invalid. "
                        "Return only a single valid JSON object matching the requested schema. "
                        "Do not include markdown fences, commentary, or prose outside JSON."
                    ),
                }
            )
        raw = _call_ollama_chat(
            attempt_messages,
            schema=schema if attempt == 0 else None,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            model_override=model_override,
        )
        try:
            parsed = _extract_json_from_llm_text(raw)
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"Structured LLM call expected a JSON object but received {type(parsed).__name__}"
                )
            return parsed
        except Exception as exc:
            last_error = exc
    raise last_error or ValueError("Structured LLM call failed")

def _playlist_lookup_by_name(playlists, target_name):
    normalized_target = _normalize_text(target_name)
    if not normalized_target:
        return None
    exact_match = None
    fuzzy_match = None
    for playlist in playlists:
        name = _normalize_text(playlist.name)
        if not name:
            continue
        if name == normalized_target:
            exact_match = playlist
            break
        if normalized_target in name or name in normalized_target:
            fuzzy_match = fuzzy_match or playlist
    return exact_match or fuzzy_match


def _summarize_artist_description(text: str, *, max_chars: int = 320) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
        if sentence.strip()
    ]
    summary_parts = []
    for sentence in sentences[:3]:
        projected = " ".join([*summary_parts, sentence]).strip()
        if summary_parts and len(projected) > max_chars:
            break
        summary_parts.append(sentence)
        if len(summary_parts) >= 2 and len(projected) >= min(180, max_chars):
            break
    summary = " ".join(summary_parts).strip() or cleaned[:max_chars].strip()
    if len(summary) < len(cleaned) and not summary.endswith(("...", ".", "!", "?")):
        summary = summary.rstrip(",;:- ") + "..."
    return summary


def _trace_start(
    request_type: str,
    *,
    user_scope_id: str = "guest",
    surface: str = "",
    query: str = "",
) -> Dict[str, Any]:
    return {
        "request_id": str(uuid.uuid4()),
        "request_type": request_type,
        "user_scope_id": _assistant_safe_scope_id(user_scope_id or "guest"),
        "surface": _recommendation_trim_text(surface),
        "query": _recommendation_trim_text(query),
        "started_at": time.perf_counter(),
        "stage_ms": {},
        "candidate_counts": {},
        "source_counts": {},
        "ranking_meta": {},
        "status": "started",
        "error": "",
    }


def _trace_stage(trace: Optional[Dict[str, Any]], stage: str, started_at: float) -> None:
    if not isinstance(trace, dict):
        return
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    stage_ms = trace.setdefault("stage_ms", {})
    stage_ms[stage] = stage_ms.get(stage, 0) + max(elapsed_ms, 0)


def _trace_put(trace: Optional[Dict[str, Any]], group: str, key: str, value: Any) -> None:
    if not isinstance(trace, dict):
        return
    payload = trace.setdefault(group, {})
    payload[key] = value

def _trace_finalize(trace: Optional[Dict[str, Any]], *, status: str = "success", error: str = "") -> Dict[str, Any]:
    if not isinstance(trace, dict):
        return {}
    trace["status"] = status
    trace["error"] = _recommendation_trim_text(error)
    total_ms = int((time.perf_counter() - float(trace.get("started_at") or 0.0)) * 1000)
    trace["total_ms"] = max(total_ms, 0)
    return trace


def _trace_diagnostics(trace: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(trace, dict):
        return {}
    return {
        "request_id": trace.get("request_id") or "",
        "request_type": trace.get("request_type") or "",
        "status": trace.get("status") or "unknown",
        "stage_timings_ms": dict(trace.get("stage_ms") or {}),
        "candidate_counts": dict(trace.get("candidate_counts") or {}),
        "source_counts": dict(trace.get("source_counts") or {}),
        "ranking_meta": dict(trace.get("ranking_meta") or {}),
        "total_ms": int(trace.get("total_ms") or 0),
        "error": trace.get("error") or "",
    }


def _trace_log_request(
    trace: Optional[Dict[str, Any]],
    *,
    request_type: str,
    user_scope_id: str,
    session_id: str = "",
    model_version: str = "",
) -> None:
    if not isinstance(trace, dict):
        return
    diagnostics = _trace_diagnostics(trace)
    try:
        _pg_log_request(
            request_id=(trace.get("request_id") or str(uuid.uuid4())),
            request_type=request_type,
            user_scope_id=_assistant_safe_scope_id(user_scope_id or "guest"),
            session_id=_recommendation_trim_text(session_id),
            model_version=_recommendation_trim_text(model_version),
            diagnostics=diagnostics,
        )
    except Exception:
        return


def _upstream_call_with_retry(
    fn,
    *,
    attempts: int = 2,
    backoff_seconds: float = 0.12,
    default=None,
):
    for attempt in range(max(1, int(attempts or 1))):
        try:
            return fn()
        except Exception:
            if attempt + 1 >= max(1, int(attempts or 1)):
                break
            time.sleep(max(0.0, backoff_seconds) * (attempt + 1))
    return default


def _executor_call_with_retry(
    fn,
    *,
    executor,
    attempts: int = 2,
    backoff_seconds: float = 0.12,
    timeout_seconds: float | None = None,
    default=None,
):
    for attempt in range(max(1, int(attempts or 1))):
        future = None
        try:
            future = executor.submit(fn)
            if timeout_seconds is None:
                return future.result()
            return future.result(timeout=max(0.05, float(timeout_seconds)))
        except Exception:
            if future is not None:
                future.cancel()
            if attempt + 1 >= max(1, int(attempts or 1)):
                break
            time.sleep(max(0.0, backoff_seconds) * (attempt + 1))
    return default


def _search_upstream_call_with_retry(
    fn,
    *,
    attempts: int = 2,
    backoff_seconds: float = 0.12,
    timeout_seconds: float | None = None,
    default=None,
):
    return _executor_call_with_retry(
        fn,
        executor=search_upstream_executor,
        attempts=attempts,
        backoff_seconds=backoff_seconds,
        timeout_seconds=timeout_seconds or SEARCH_UPSTREAM_TIMEOUT_SECONDS,
        default=default,
    )


recommendation_bootstrap_thread = None
recommendation_bootstrap_lock = Lock()


def _recommendation_worker_runtime_unhealthy() -> bool:
    if not RECOMMENDATION_EXTERNAL_WORKER:
        return False
    try:
        runtime = dict(_recommendation_runtime_snapshot() or {})
    except Exception:
        return True
    worker_status = str(runtime.get("worker_status") or "").strip().lower()
    last_heartbeat_at = float(runtime.get("worker_last_heartbeat_at") or 0.0)
    if worker_status != "running" or last_heartbeat_at <= 0:
        return True
    heartbeat_age_seconds = max(0.0, time.time() - last_heartbeat_at)
    max_heartbeat_age_seconds = max(
        60.0,
        float(RECOMMENDATION_SYNC_INTERVAL_SECONDS) * 2.0,
    )
    return heartbeat_age_seconds > max_heartbeat_age_seconds


def _start_recommendation_bootstrap_thread() -> bool:
    global recommendation_bootstrap_thread
    with recommendation_bootstrap_lock:
        if (
            recommendation_bootstrap_thread is not None
            and recommendation_bootstrap_thread.is_alive()
        ):
            return False
        recommendation_bootstrap_thread = Thread(
            target=_recommendation_bootstrap_once,
            name="recommendation-bootstrap",
            daemon=True,
        )
        recommendation_bootstrap_thread.start()
        return True


def startup_recommendation_runtime():
    _recommendation_init_store_db()
    if RECOMMENDATION_ENABLE_SCHEDULER and not RECOMMENDATION_EXTERNAL_WORKER:
        _recommendation_start_scheduler()
        return
    if RECOMMENDATION_EXTERNAL_WORKER:
        if _recommendation_worker_runtime_unhealthy():
            _start_recommendation_bootstrap_thread()
        return
    _start_recommendation_bootstrap_thread()

def shutdown_recommendation_runtime():
    _recommendation_stop_scheduler()

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ytmusic = YTMusic()
STREAM_INFO_TTL_SECONDS = 21600
STREAM_WARM_CHUNK_BYTES = int(os.environ.get("STREAM_WARM_CHUNK_BYTES", "786432"))
STREAM_CHUNK_TTL_SECONDS = 1800
STREAM_WARM_WORKERS = int(os.environ.get("STREAM_WARM_WORKERS", "8"))
PREPARE_SESSION_WORKERS = int(os.environ.get("PREPARE_SESSION_WORKERS", "4"))
PREPARE_SESSION_MAX_LOOKAHEAD = int(os.environ.get("PREPARE_SESSION_MAX_LOOKAHEAD", "18"))
RECOMMENDATION_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_CACHE_TTL_SECONDS", "180"))
RECOMMENDATION_FEED_SESSION_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_FEED_SESSION_TTL_SECONDS", "900"))
RECOMMENDATION_PROFILE_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_PROFILE_CACHE_TTL_SECONDS", "300"))
RECOMMENDATION_TRACK_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_TRACK_CACHE_TTL_SECONDS", "900"))
RECOMMENDATION_TRACK_LOOKUP_EXTRA = max(
    2,
    int(os.environ.get("RECOMMENDATION_TRACK_LOOKUP_EXTRA", "8")),
)
RECOMMENDATION_TRACK_FETCH_BUDGET_SECONDS = max(
    2.0,
    float(os.environ.get("RECOMMENDATION_TRACK_FETCH_BUDGET_SECONDS", "6.0")),
)
RECOMMENDATION_TRACK_FETCH_PER_FUTURE_TIMEOUT_SECONDS = max(
    0.5,
    float(
        os.environ.get(
            "RECOMMENDATION_TRACK_FETCH_PER_FUTURE_TIMEOUT_SECONDS",
            "2.0",
        )
    ),
)
RECOMMENDATION_ROW_PAGE_SIZE = int(os.environ.get("RECOMMENDATION_ROW_PAGE_SIZE", "8"))
RECOMMENDATION_EMBED_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_EMBED_CACHE_TTL_SECONDS", "1800"))
SEARCH_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("SEARCH_RESULT_CACHE_TTL_SECONDS", "600"))
DETAIL_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("DETAIL_RESULT_CACHE_TTL_SECONDS", "1800"))
SEARCH_EXECUTOR_WORKERS = max(
    2,
    int(os.environ.get("SEARCH_EXECUTOR_WORKERS", "6")),
)
SEARCH_UPSTREAM_WORKERS = max(
    2,
    int(os.environ.get("SEARCH_UPSTREAM_WORKERS", "4")),
)
SEARCH_UPSTREAM_TIMEOUT_SECONDS = max(
    0.8,
    float(os.environ.get("SEARCH_UPSTREAM_TIMEOUT_SECONDS", "2.4")),
)
RECOMMENDED_ARTISTS_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDED_ARTISTS_CACHE_TTL_SECONDS", "600"))
SEARCH_TRACK_CANDIDATE_MIN = int(os.environ.get("SEARCH_TRACK_CANDIDATE_MIN", "24"))
SEARCH_TRACK_CANDIDATE_MAX = int(os.environ.get("SEARCH_TRACK_CANDIDATE_MAX", "72"))
SEARCH_ALBUM_CANDIDATE_MAX = int(os.environ.get("SEARCH_ALBUM_CANDIDATE_MAX", "24"))
SEARCH_ARTIST_CANDIDATE_MAX = int(os.environ.get("SEARCH_ARTIST_CANDIDATE_MAX", "32"))
SEARCH_METADATA_ENRICH_LIMIT = int(os.environ.get("SEARCH_METADATA_ENRICH_LIMIT", "12"))
SEARCH_METADATA_ENRICH_BUDGET_SECONDS = max(
    1.0,
    float(os.environ.get("SEARCH_METADATA_ENRICH_BUDGET_SECONDS", "4.0")),
)
SEARCH_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS = max(
    0.25,
    float(
        os.environ.get(
            "SEARCH_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS",
            "1.2",
        )
    ),
)
RECOMMENDATION_METADATA_ENRICH_LIMIT = int(
    os.environ.get("RECOMMENDATION_METADATA_ENRICH_LIMIT", "12")
)
RECOMMENDATION_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS = max(
    0.4,
    float(
        os.environ.get(
            "RECOMMENDATION_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS",
            "1.8",
        )
    ),
)
RECOMMENDATION_CANDIDATE_SOURCE_TIMEOUT_SECONDS = max(
    1.0,
    float(
        os.environ.get(
            "RECOMMENDATION_CANDIDATE_SOURCE_TIMEOUT_SECONDS",
            "4.0",
        )
    ),
)
UPSTREAM_RETRY_ATTEMPTS = int(os.environ.get("UPSTREAM_RETRY_ATTEMPTS", "2"))
UPSTREAM_RETRY_BACKOFF_SECONDS = float(
    os.environ.get("UPSTREAM_RETRY_BACKOFF_SECONDS", "0.12")
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/api").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m2.7:cloud")
OLLAMA_THINKING_MODEL = os.environ.get("OLLAMA_THINKING_MODEL", OLLAMA_MODEL).strip() or OLLAMA_MODEL
OLLAMA_FAST_MODEL = os.environ.get("OLLAMA_FAST_MODEL", "rnj-1:8b-cloud").strip() or "rnj-1:8b-cloud"
OLLAMA_PLANNER_MODEL = os.environ.get("OLLAMA_PLANNER_MODEL", OLLAMA_FAST_MODEL).strip() or OLLAMA_FAST_MODEL
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", OLLAMA_MODEL).strip()
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "15m").strip()
OLLAMA_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_CONNECT_TIMEOUT_SECONDS", "10"))
OLLAMA_READ_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_READ_TIMEOUT_SECONDS", "90"))
ASSISTANT_EMBED_OLLAMA_TIMEOUT_SECONDS = max(
    0.5,
    float(os.environ.get("ASSISTANT_EMBED_OLLAMA_TIMEOUT_SECONDS", "2.5")),
)
ASSISTANT_EMBED_OLLAMA_COOLDOWN_SECONDS = max(
    0.0,
    float(os.environ.get("ASSISTANT_EMBED_OLLAMA_COOLDOWN_SECONDS", "90")),
)
ASSISTANT_EMBED_BACKEND = os.environ.get("ASSISTANT_EMBED_BACKEND", "local").strip().lower()
ASSISTANT_EMBED_DIM = int(os.environ.get("ASSISTANT_EMBED_DIM", "256"))
USE_LANGGRAPH_ASSISTANT = os.environ.get("USE_LANGGRAPH_ASSISTANT", "1").strip().lower() not in {"0", "false", "no"}
ASSISTANT_VECTOR_BACKEND = os.environ.get("ASSISTANT_VECTOR_BACKEND", "sqlite").strip().lower()
ASSISTANT_PGVECTOR_DSN = os.environ.get("ASSISTANT_PGVECTOR_DSN", os.environ.get("DATABASE_URL", "")).strip()


def _server_runtime_path(filename: str, env_var: str = "") -> str:
    override = (os.environ.get(env_var or "") or "").strip() if env_var else ""
    if override:
        return os.path.abspath(override)
    runtime_dir = (
        os.environ.get("AURALIS_PROXY_RUNTIME_DIR")
        or os.environ.get("AURALIS_RUNTIME_DIR")
        or os.path.join(tempfile.gettempdir(), "auralis_proxy")
    )
    os.makedirs(runtime_dir, exist_ok=True)
    return os.path.join(os.path.abspath(runtime_dir), filename)


ASSISTANT_MEMORY_DB_PATH = _server_runtime_path(
    "assistant_memory.sqlite",
    "AURALIS_ASSISTANT_MEMORY_DB_PATH",
)
RECOMMENDATION_SYNC_DATABASE_DSN = os.environ.get(
    "RECOMMENDATION_SYNC_DATABASE_DSN",
    ASSISTANT_PGVECTOR_DSN,
).strip()
RECOMMENDATION_STORE_DB_PATH = _server_runtime_path(
    "recommendation_store.sqlite",
    "AURALIS_RECOMMENDATION_STORE_DB_PATH",
)
RECOMMENDATION_MODEL_CACHE_TTL_SECONDS = int(
    os.environ.get("RECOMMENDATION_MODEL_CACHE_TTL_SECONDS", "180")
)
RECOMMENDATION_MODEL_STALE_REFRESH_ENABLED = os.environ.get(
    "RECOMMENDATION_MODEL_STALE_REFRESH_ENABLED",
    "1",
).strip().lower() not in {"0", "false", "no"}
RECOMMENDATION_MODEL_MIN_EVENTS = int(
    os.environ.get("RECOMMENDATION_MODEL_MIN_EVENTS", "24")
)
RECOMMENDATION_MODEL_MAX_EVENTS = int(
    os.environ.get("RECOMMENDATION_MODEL_MAX_EVENTS", "50000")
)
RECOMMENDATION_MODEL_FACTOR_DIM = int(
    os.environ.get("RECOMMENDATION_MODEL_FACTOR_DIM", "20")
)
RECOMMENDATION_MODEL_EPOCHS = int(
    os.environ.get("RECOMMENDATION_MODEL_EPOCHS", "4")
)
RECOMMENDATION_MODEL_NEIGHBOR_LIMIT = int(
    os.environ.get("RECOMMENDATION_MODEL_NEIGHBOR_LIMIT", "24")
)
RECOMMENDATION_SYNC_BATCH_SIZE = int(
    os.environ.get("RECOMMENDATION_SYNC_BATCH_SIZE", "1000")
)
RECOMMENDATION_ENABLE_SCHEDULER = os.environ.get(
    "RECOMMENDATION_ENABLE_SCHEDULER",
    "1",
).strip().lower() not in {"0", "false", "no"}
RECOMMENDATION_EXTERNAL_WORKER = os.environ.get(
    "RECOMMENDATION_EXTERNAL_WORKER",
    "0",
).strip().lower() not in {"0", "false", "no"}
RECOMMENDATION_SYNC_INTERVAL_SECONDS = int(
    os.environ.get("RECOMMENDATION_SYNC_INTERVAL_SECONDS", "300")
)
RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS = int(
    os.environ.get("RECOMMENDATION_SYNC_FAILURE_RETRY_SECONDS", "300")
)
RECOMMENDATION_TRAIN_INTERVAL_SECONDS = int(
    os.environ.get("RECOMMENDATION_TRAIN_INTERVAL_SECONDS", "900")
)
RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS = int(
    os.environ.get("RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS", "900")
)
RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS = int(
    os.environ.get("RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS", "168")
)
RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS = int(
    os.environ.get("RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS", "120")
)
RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN = float(
    os.environ.get("RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN", "0.03")
)
RECOMMENDATION_PROMOTE_WINNER = os.environ.get(
    "RECOMMENDATION_PROMOTE_WINNER",
    "1",
).strip().lower() not in {"0", "false", "no"}
RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS = int(
    os.environ.get("RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS", "172800")
)
RECOMMENDATION_EXPERIMENT_KEY = os.environ.get(
    "RECOMMENDATION_EXPERIMENT_KEY",
    "feed_ranking_v2",
).strip() or "feed_ranking_v2"
RECOMMENDATION_ROW_BUILD_WORKERS = max(
    1,
    int(os.environ.get("RECOMMENDATION_ROW_BUILD_WORKERS", "4")),
)
RECOMMENDATION_EXECUTOR_WORKERS = max(
    6,
    int(os.environ.get("RECOMMENDATION_EXECUTOR_WORKERS", "12")),
)
PRECOMPUTE_EXECUTOR_WORKERS = max(
    2,
    int(os.environ.get("PRECOMPUTE_EXECUTOR_WORKERS", "4")),
)
RECOMMENDATION_ROW_BUILDER_TIMEOUT_SECONDS = max(
    4.0,
    float(os.environ.get("RECOMMENDATION_ROW_BUILDER_TIMEOUT_SECONDS", "8.0")),
)
RECOMMENDATION_ROW_FINALIZE_BUDGET_SECONDS = max(
    2.0,
    float(os.environ.get("RECOMMENDATION_ROW_FINALIZE_BUDGET_SECONDS", "8.0")),
)
RECOMMENDATION_REQUIRED_ROWS = tuple(
    item.strip()
    for item in os.environ.get(
        "RECOMMENDATION_REQUIRED_ROWS",
        "continue_listening,because_you_played,quiet_picks",
    ).split(",")
    if item.strip()
)
RECOMMENDATION_QUERY_DERIVED_SOURCE_SHARE_CAP = min(
    0.9,
    max(
        0.2,
        float(
            os.environ.get(
                "RECOMMENDATION_QUERY_DERIVED_SOURCE_SHARE_CAP",
                "0.45",
            )
        ),
    ),
)
RECOMMENDATION_QUERY_DERIVED_SOURCE_ITEM_CAP = max(
    2,
    int(os.environ.get("RECOMMENDATION_QUERY_DERIVED_SOURCE_ITEM_CAP", "7")),
)
RECOMMENDATION_QUIET_FALLBACK_LIMIT = max(
    8,
    int(os.environ.get("RECOMMENDATION_QUIET_FALLBACK_LIMIT", "28")),
)
RECOMMENDATION_MODEL_EXPORT_DIR = os.environ.get(
    "RECOMMENDATION_MODEL_EXPORT_DIR",
    os.path.join(os.getcwd(), "downloads", "recommendation_models"),
).strip()
prepare_metrics = deque(maxlen=180)
prepare_metrics_lock = Lock()
assistant_embed_ollama_backoff_lock = Lock()
assistant_embed_ollama_backoff_until = 0.0
recommendation_store_lock = Lock()
recommendation_scheduler_stop = Event()
recommendation_scheduler_thread = None
stream_warm_executor = ThreadPoolExecutor(max_workers=STREAM_WARM_WORKERS)
recommendation_executor = ThreadPoolExecutor(
    max_workers=RECOMMENDATION_EXECUTOR_WORKERS
)
precompute_executor = ThreadPoolExecutor(max_workers=PRECOMPUTE_EXECUTOR_WORKERS)
search_executor = ThreadPoolExecutor(max_workers=SEARCH_EXECUTOR_WORKERS)
search_upstream_executor = ThreadPoolExecutor(max_workers=SEARCH_UPSTREAM_WORKERS)
recommendation_row_executor = ThreadPoolExecutor(
    max_workers=RECOMMENDATION_ROW_BUILD_WORKERS
)
upstream_http = requests.Session()
ollama_http = requests.Session()
assistant_memory_lock = Lock()

def _cache_lookup(cache_root, lock: Lock, namespace: str, key: str):
    return _cache_runtime_lookup(cache_root, lock, namespace, key)


def _cache_store(cache_root, lock: Lock, namespace: str, key: str, value, ttl_seconds: int):
    _cache_runtime_store(cache_root, lock, namespace, key, value, ttl_seconds)


def _search_cache_key(query: str, limit: int) -> str:
    normalized_query = _normalize_text(query)
    return f"{normalized_query}|{max(int(limit or 0), 0)}"


def _recommended_artists_cache_key(req: SearchRequest) -> str:
    payload = {
        "user_scope_id": _recommendation_trim_text(req.user_scope_id or "guest"),
        "limit": max(int(req.limit or 0), 0),
        "surface": _recommendation_trim_text(req.surface or "home_feed") or "home_feed",
        "artist_hints": _recommendation_unique_strings(req.artist_hints, 12),
        "anchor_artist_hints": _recommendation_unique_strings(req.anchor_artist_hints, 12),
        "recent_queries": _recommendation_unique_strings(req.recent_queries, 12),
        "taste_queries": _recommendation_unique_strings(req.taste_queries, 12),
        "recent_track_ids": [
            track.get("id")
            for track in _recommendation_unique_snapshot_tracks(req.recent_track_snapshots, 12)
        ],
        "top_track_ids": [
            track.get("id")
            for track in _recommendation_unique_snapshot_tracks(req.top_track_snapshots, 12)
        ],
        "last_played_ids": [
            track.get("id")
            for track in _recommendation_unique_snapshot_tracks(req.last_played_tracks, 12)
        ],
        "anchor_track_ids": [
            track.get("id")
            for track in _recommendation_unique_snapshot_tracks(req.anchor_track_snapshots, 8)
        ],
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

_assistant_db_connection = _bind_server(_assistant_core_runtime.assistant_db_connection)
_assistant_pgvector_enabled = _bind_server(_assistant_core_runtime.assistant_pgvector_enabled)
_assistant_pgvector_connection = _bind_server(_assistant_core_runtime.assistant_pgvector_connection)
_assistant_vector_literal = _bind_server(_assistant_core_runtime.assistant_vector_literal)

_assistant_init_memory_db = _bind_server(_assistant_core_runtime.assistant_init_memory_db)

_assistant_safe_scope_id = _bind_server(_assistant_core_runtime.assistant_safe_scope_id)


_assistant_now_timestamp = _bind_server(_assistant_core_runtime.assistant_now_timestamp)


_assistant_default_session_title = _bind_server(_assistant_core_runtime.assistant_default_session_title)


_assistant_preview_text = _bind_server(_assistant_core_runtime.assistant_preview_text)

_assistant_init_session_db = _bind_server(_assistant_core_runtime.assistant_init_session_db)


_assistant_session_summary_from_row = _bind_server(_assistant_core_runtime.assistant_session_summary_from_row)


_assistant_list_sessions = _bind_server(_assistant_core_runtime.assistant_list_sessions)


_assistant_get_session = _bind_server(_assistant_core_runtime.assistant_get_session)


_assistant_create_session = _bind_server(_assistant_core_runtime.assistant_create_session)


_assistant_touch_session = _bind_server(_assistant_core_runtime.assistant_touch_session)


_assistant_store_session_message = _bind_server(_assistant_core_runtime.assistant_store_session_message)


_assistant_get_session_messages = _bind_server(_assistant_core_runtime.assistant_get_session_messages)


_assistant_get_session_detail = _bind_server(_assistant_core_runtime.assistant_get_session_detail)


_assistant_update_session = _bind_server(_assistant_core_runtime.assistant_update_session)


_assistant_delete_session = _bind_server(_assistant_core_runtime.assistant_delete_session)

_assistant_embed_texts = _bind_server(_assistant_core_runtime.assistant_embed_texts)

_assistant_cosine_similarity = _bind_server(_assistant_core_runtime.assistant_cosine_similarity)


def _vector_normalize(values: Optional[List[float]]):
    if not values:
        return []
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [float(value) / norm for value in values]


def _vector_weighted_average(weighted_vectors):
    valid = [
        (vector, float(weight))
        for vector, weight in weighted_vectors
        if vector and float(weight) > 0
    ]
    if not valid:
        return []
    dimension = len(valid[0][0])
    blended = [0.0] * dimension
    total_weight = 0.0
    for vector, weight in valid:
        if len(vector) != dimension:
            continue
        total_weight += weight
        for index, value in enumerate(vector):
            blended[index] += float(value) * weight
    if total_weight <= 0:
        return []
    blended = [value / total_weight for value in blended]
    return _vector_normalize(blended)


_recommendation_track_text = _recommendation_helper_runtime._recommendation_track_text

_recommendation_artist_text = _recommendation_helper_runtime._recommendation_artist_text

_recommendation_album_text = _recommendation_helper_runtime._recommendation_album_text

_recommendation_artist_embedding_key = _recommendation_helper_runtime._recommendation_artist_embedding_key

_recommendation_album_embedding_key = _recommendation_helper_runtime._recommendation_album_embedding_key

_recommendation_track_embedding_key = _recommendation_helper_runtime._recommendation_track_embedding_key

_recommendation_text_embedding_key = _recommendation_helper_runtime._recommendation_text_embedding_key

_recommendation_cached_embedding = _recommendation_helper_runtime._recommendation_cached_embedding

_recommendation_store_embedding = _recommendation_helper_runtime._recommendation_store_embedding

_recommendation_embed_entries = _recommendation_helper_runtime._recommendation_embed_entries

_recommendation_track_embeddings = _recommendation_helper_runtime._recommendation_track_embeddings

_recommendation_artist_embeddings = _recommendation_helper_runtime._recommendation_artist_embeddings

_recommendation_album_embeddings = _recommendation_helper_runtime._recommendation_album_embeddings

_assistant_store_memory = _bind_server(_assistant_core_runtime.assistant_store_memory)

_assistant_query_memory = _bind_server(_assistant_core_runtime.assistant_query_memory)

_assistant_track_from_context = _bind_server(_assistant_tool_runtime.assistant_track_from_context)

_assistant_all_context_tracks = _bind_server(_assistant_tool_runtime.assistant_all_context_tracks)

_assistant_tool_search_tracks = _bind_server(_assistant_tool_runtime.assistant_tool_search_tracks)


_assistant_tool_search_albums = _bind_server(_assistant_tool_runtime.assistant_tool_search_albums)


_assistant_tool_search_artists_direct = _bind_server(
    _assistant_tool_runtime.assistant_tool_search_artists_direct
)


_artist_names_from_track_query = _bind_server(
    _assistant_tool_runtime.artist_names_from_track_query
)


_assistant_tool_search_artists = _bind_server(_assistant_tool_runtime.assistant_tool_search_artists)


_build_track_details_payload = _bind_server(_assistant_tool_runtime.build_track_details_payload)


_build_album_details_payload = _bind_server(_assistant_tool_runtime.build_album_details_payload)


_assistant_tool_get_track_details = _bind_server(
    _assistant_tool_runtime.assistant_tool_get_track_details
)


_assistant_tool_get_album_details = _bind_server(
    _assistant_tool_runtime.assistant_tool_get_album_details
)


_assistant_tool_get_similar_tracks = _bind_server(
    _assistant_tool_runtime.assistant_tool_get_similar_tracks
)


_track_metadata_incomplete = _bind_server(_assistant_tool_runtime.track_metadata_incomplete)


_merge_track_metadata = _bind_server(_assistant_tool_runtime.merge_track_metadata)


_recommendation_enrich_track_metadata = _bind_server(
    _assistant_tool_runtime.recommendation_enrich_track_metadata
)


_assistant_tool_get_user_taste_profile = _bind_server(
    _assistant_tool_runtime.assistant_tool_get_user_taste_profile
)

_assistant_tool_use_context_tracks = _bind_server(
    _assistant_tool_runtime.assistant_tool_use_context_tracks
)

_assistant_tool_list_playlists = _bind_server(_assistant_tool_runtime.assistant_tool_list_playlists)

_assistant_attach_reasons_runtime = _bind_server(
    _assistant_tool_runtime.assistant_attach_reasons_runtime
)

_assistant_store_turn_memory = _bind_server(_assistant_tool_runtime.assistant_store_turn_memory)

_assistant_initial_memory_queries = _bind_server(
    _assistant_tool_runtime.assistant_initial_memory_queries
)

_assistant_merge_memory_hits = _bind_server(_assistant_tool_runtime.assistant_merge_memory_hits)

_assistant_playlist_options = _bind_server(_assistant_tool_runtime.assistant_playlist_options)

_assistant_model_for_request = _bind_server(_assistant_tool_runtime.assistant_model_for_request)

_assistant_langgraph_deps = _bind_server(_assistant_tool_runtime.assistant_langgraph_deps)

_assistant_fallback_chat_reply = _bind_server(_assistant_tool_runtime.assistant_fallback_chat_reply)

_assistant_init_memory_db()
_assistant_init_session_db()

def recommendation_interaction_event(req: RecommendationInteractionEventRequest):
    from auralis_backend.recommend.admin_runtime import interaction_event

    return interaction_event(sys.modules[__name__], req)


def recommendation_search_interaction(req: RecommendationSearchEventRequest):
    from auralis_backend.recommend.admin_runtime import search_interaction

    return search_interaction(sys.modules[__name__], req)


def _fallback_home_candidates(limit: int):
    cached = _cache_lookup_home_candidates(limit)
    if cached:
        return cached

    results = []
    try:
        home_feed = ytmusic.get_home(limit=limit)
    except Exception:
        home_feed = []

    for carousel in home_feed:
        for item in carousel.get("contents", []):
            track = normalize_recommendation_track(item)
            if track is None:
                continue
            results.append((track, 1.0))
            if len(results) >= limit + 10:
                return results

    if len(results) < 5:
        try:
            backup = ytmusic.search("trending hit songs", filter="songs", limit=limit)
        except Exception:
            backup = []
        for entry in backup:
            track = normalize_recommendation_track(entry)
            if track is None:
                continue
            results.append((track, 0.7))
            if len(results) >= limit + 10:
                break

    if results:
        _cache_store_home_candidates(
            results,
            ttl_seconds=RECOMMENDATION_CACHE_TTL_SECONDS,
            limit=limit,
        )
    return results
_recommendation_trim_text = _recommendation_helper_runtime._recommendation_trim_text

_recommendation_unique_strings = _recommendation_helper_runtime._recommendation_unique_strings

_recommendation_unique_track_ids = _recommendation_helper_runtime._recommendation_unique_track_ids

_recommendation_unique_snapshot_tracks = _recommendation_helper_runtime._recommendation_unique_snapshot_tracks

_recommendation_track_from_details = _recommendation_helper_runtime._recommendation_track_from_details

_recommendation_track_from_song_payload = _recommendation_helper_runtime._recommendation_track_from_song_payload

_recommendation_fetch_track_for_id_lightweight = _recommendation_helper_runtime._recommendation_fetch_track_for_id_lightweight

_recommendation_cached_track = _recommendation_helper_runtime._recommendation_cached_track

_recommendation_store_cached_track = _recommendation_helper_runtime._recommendation_store_cached_track

_recommendation_fetch_track_for_id = _recommendation_helper_runtime._recommendation_fetch_track_for_id

_recommendation_store_connection = _bind_server(
    _recommendation_store_runtime.open_recommendation_store_connection
)


_recommendation_init_store_db = _bind_server(
    _recommendation_store_runtime.init_recommendation_store
)


_recommendation_external_pg_connection = _recommendation_maintenance_runtime._recommendation_external_pg_connection

_recommendation_event_weight = _recommendation_maintenance_runtime._recommendation_event_weight

_recommendation_sync_state_get = _recommendation_maintenance_runtime._recommendation_sync_state_get

_recommendation_sync_state_set = _recommendation_maintenance_runtime._recommendation_sync_state_set

_recommendation_sync_state_float = _recommendation_maintenance_runtime._recommendation_sync_state_float

_recommendation_mark_external_sync_failure = _recommendation_maintenance_runtime._recommendation_mark_external_sync_failure

_recommendation_clear_external_sync_failure = _recommendation_maintenance_runtime._recommendation_clear_external_sync_failure

_recommendation_classify_external_sync_error = _recommendation_maintenance_runtime._recommendation_classify_external_sync_error

_recommendation_mark_external_sync_success = _recommendation_maintenance_runtime._recommendation_mark_external_sync_success

_recommendation_external_sync_health_snapshot = _recommendation_maintenance_runtime._recommendation_external_sync_health_snapshot

_recommendation_should_retry_external_sync = _recommendation_maintenance_runtime._recommendation_should_retry_external_sync

_recommendation_active_promotion = _recommendation_maintenance_runtime._recommendation_active_promotion

_recommendation_find_recent_impression = _recommendation_maintenance_runtime._recommendation_find_recent_impression

_recommendation_attribute_interaction_event = _recommendation_maintenance_runtime._recommendation_attribute_interaction_event

_recommendation_experiment_dashboard = _recommendation_maintenance_runtime._recommendation_experiment_dashboard

_recommendation_evaluate_experiment = _recommendation_maintenance_runtime._recommendation_evaluate_experiment

_recommendation_runtime_snapshot = _recommendation_maintenance_runtime._recommendation_runtime_snapshot

_recommendation_store_search_event = _recommendation_maintenance_runtime._recommendation_store_search_event

_recommendation_feature_store_upsert_many = _recommendation_maintenance_runtime._recommendation_feature_store_upsert_many

_recommendation_assignment_for_user = _recommendation_maintenance_runtime._recommendation_assignment_for_user

_recommendation_record_impressions = _recommendation_maintenance_runtime._recommendation_record_impressions

_recommendation_invalidate_collaborative_cache = _recommendation_maintenance_runtime._recommendation_invalidate_collaborative_cache

_recommendation_store_interaction_event = _recommendation_maintenance_runtime._recommendation_store_interaction_event

_recommendation_sync_external_events = _recommendation_maintenance_runtime._recommendation_sync_external_events

_recommendation_model_source_signature = _recommendation_maintenance_runtime._recommendation_model_source_signature

_recommendation_seeded_vector = _recommendation_maintenance_runtime._recommendation_seeded_vector

_recommendation_vector_dot = _recommendation_maintenance_runtime._recommendation_vector_dot

_recommendation_sigmoid = _recommendation_maintenance_runtime._recommendation_sigmoid

_recommendation_sample_negative_item = _recommendation_maintenance_runtime._recommendation_sample_negative_item

_recommendation_round_vector = _recommendation_maintenance_runtime._recommendation_round_vector

_recommendation_train_collaborative_model = _recommendation_maintenance_runtime._recommendation_train_collaborative_model

_recommendation_materialize_feature_store = _recommendation_maintenance_runtime._recommendation_materialize_feature_store

_recommendation_export_model_artifact = _recommendation_maintenance_runtime._recommendation_export_model_artifact

_recommendation_store_collaborative_model = _recommendation_maintenance_runtime._recommendation_store_collaborative_model

_recommendation_run_maintenance_cycle = _recommendation_maintenance_runtime._recommendation_run_maintenance_cycle

_recommendation_bootstrap_once = _recommendation_maintenance_runtime._recommendation_bootstrap_once

_recommendation_worker_heartbeat = _recommendation_maintenance_runtime._recommendation_worker_heartbeat

_recommendation_scheduler_loop = _recommendation_maintenance_runtime._recommendation_scheduler_loop

_recommendation_start_scheduler = _recommendation_maintenance_runtime._recommendation_start_scheduler

_recommendation_stop_scheduler = _recommendation_maintenance_runtime._recommendation_stop_scheduler

run_recommendation_worker_forever = (
    _recommendation_maintenance_runtime.run_recommendation_worker_forever
)

_recommendation_cache_model_artifact = _bind_server(
    _recommendation_model_runtime.cache_model_artifact
)


_recommendation_schedule_model_refresh = _bind_server(
    _recommendation_model_runtime.schedule_model_refresh
)


_recommendation_get_collaborative_model = _bind_server(
    _recommendation_model_runtime.get_collaborative_model
)


_recommendation_build_collaborative_profile = _bind_server(
    _recommendation_profile_runtime.build_collaborative_profile
)


_recommendation_collaborative_neighbor_tracks = _recommendation_row_helper_runtime._recommendation_collaborative_neighbor_tracks

_recommendation_collaborative_track_scores = _recommendation_row_helper_runtime._recommendation_collaborative_track_scores

_recommendation_fetch_tracks_for_ids = _recommendation_row_helper_runtime._recommendation_fetch_tracks_for_ids

_recommendation_track_signature = _recommendation_row_helper_runtime._recommendation_track_signature

_recommendation_feed_session_key = _bind_server(
    _recommendation_session_runtime.feed_session_key
)


_recommendation_load_feed_session = _bind_server(
    _recommendation_session_runtime.load_feed_session
)

_recommendation_prune_feed_cache = _bind_server(
    _recommendation_session_runtime.prune_feed_cache
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)




