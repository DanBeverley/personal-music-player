from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import json
import hashlib
import math
import os
import random
import re
import sqlite3
import time
import traceback
import uuid

import requests
import yt_dlp
from ytmusicapi import YTMusic

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
    cache_key = _recommendation_trim_text(artist_id)
    if cache_key:
        cache_key = f"{cache_key}:{'expanded' if enrich_related else 'basic'}"
    if cache_key:
        cached = _cache_lookup(
            detail_result_cache,
            detail_result_cache_lock,
            "artist",
            cache_key,
        )
        if cached is not None:
            return dict(cached)

    artist = ytmusic.get_artist(artist_id)
    name = artist.get("name") or "Unknown Artist"
    songs_section = artist.get("songs") or {}
    album_section = artist.get("albums") or {}
    related_section = artist.get("related") or {}

    top_songs = _normalize_artist_song_entries(
        songs_section.get("results") or [],
        fallback_artist=name,
    )

    albums = _normalize_artist_album_entries(
        album_section.get("results") or [],
        fallback_artist=name,
    )
    album_browse_id = album_section.get("browseId")
    album_params = album_section.get("params")
    if album_browse_id and album_params:
        try:
            more_albums = ytmusic.get_artist_albums(
                album_browse_id,
                album_params,
                limit=12,
            )
        except Exception:
            more_albums = []
        for album in _normalize_artist_album_entries(more_albums, fallback_artist=name):
            album_id = album.get("id")
            if album_id and any(existing.get("id") == album_id for existing in albums):
                continue
            albums.append(album)
            if len(albums) >= 12:
                break

    related_artists = _rank_artist_detail_related_artists(
        {
            "id": artist_id,
            "name": name,
            "description": artist.get("description") or "",
            "thumbnail": extract_thumbnail(artist),
        },
        top_songs,
        normalize_artist_results(related_section.get("results") or []),
        enrich_related=enrich_related,
    )

    payload = {
        "status": "success",
        "id": artist_id,
        "name": name,
        "description": artist.get("description") or "",
        "thumbnail": extract_thumbnail(artist),
        "stats": _normalize_artist_stats(artist),
        "top_songs": top_songs[:12],
        "albums": albums[:12],
        "related_artists": related_artists[:12],
    }
    if cache_key:
        _cache_store(
            detail_result_cache,
            detail_result_cache_lock,
            "artist",
            cache_key,
            payload,
            DETAIL_RESULT_CACHE_TTL_SECONDS,
        )
    return payload


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

    query_seeds = _recommendation_unique_strings(
        [
            *(req.recent_queries or []),
            *(req.taste_queries or []),
        ],
        8,
    )
    query_artist_futures = {
        query: recommendation_executor.submit(
            _assistant_tool_search_artists_direct,
            query,
            2,
        )
        for query in query_seeds
    }
    profile = _recommendation_build_profile(req)
    profile_vectors = profile.get("vectors") or {}
    collaborative = profile.get("collaborative") or {}
    listened_artist_names = {
        _normalize_text(name)
        for name in (profile.get("listened_artists") or [])
        if _normalize_text(name)
    }
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

    for index, artist_hint in enumerate(req.artist_hints or []):
        add_artist_seed(artist_hint, max(3.8 - (index * 0.18), 1.6))

    add_track_artist_seeds(req.last_played_tracks, 4.4)
    add_track_artist_seeds(req.top_track_snapshots, 3.5)
    add_track_artist_seeds(req.recent_track_snapshots, 3.0)

    query_artist_results = {}
    for query, future in query_artist_futures.items():
        try:
            query_artist_results[query] = future.result(timeout=6)
        except Exception:
            query_artist_results[query] = []

    for query in query_seeds:
        for artist_name, score in _artist_names_from_track_query(query, 3):
            add_artist_seed(artist_name, score + 1.3)
        for artist in query_artist_results.get(query) or []:
            add_artist_seed(artist.get("name"), 2.1)
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
        add_artist_seed(artist_key, max(float(score) * 0.55, 1.1) - (index * 0.08))

    ranked_seed_names = sorted(
        weighted_artist_names.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    top_seed_names = ranked_seed_names[:4]

    artists = []
    seen_artist_ids = set()
    seen_artist_names = set()

    def add_artist_result(raw_artist, score: float):
        artist_id = (raw_artist.get("id") or "").strip()
        artist_name = (raw_artist.get("name") or "").strip()
        normalized_name = _normalize_text(artist_name)
        if (
            not artist_id
            or not artist_name
            or artist_id in seen_artist_ids
            or normalized_name in seen_artist_names
        ):
            return
        artist = dict(raw_artist)
        artist["score"] = round(score, 3)
        seen_artist_ids.add(artist_id)
        seen_artist_names.add(normalized_name)
        artists.append(artist)

    direct_seed_futures = {
        seed_index: recommendation_executor.submit(
            _assistant_tool_search_artists_direct,
            seed_name,
            2,
        )
        for seed_index, (seed_name, _seed_weight) in enumerate(top_seed_names)
    }
    direct_results_by_seed = {}
    for seed_index, future in direct_seed_futures.items():
        try:
            direct_results_by_seed[seed_index] = future.result(timeout=8)
        except Exception:
            direct_results_by_seed[seed_index] = []

    related_artist_futures = {}
    for seed_index, direct_results in direct_results_by_seed.items():
        if not direct_results or seed_index >= 2:
            continue
        primary_artist_id = (direct_results[0].get("id") or "").strip()
        if not primary_artist_id:
            continue
        related_artist_futures[seed_index] = recommendation_executor.submit(
            _build_artist_details_payload,
            primary_artist_id,
            enrich_related=False,
        )

    for seed_index, (seed_name, seed_weight) in enumerate(top_seed_names):
        direct_results = direct_results_by_seed.get(seed_index) or []
        for index, artist in enumerate(direct_results):
            add_artist_result(artist, seed_weight + max(1.8 - (index * 0.3), 0.6))
        related_future = related_artist_futures.get(seed_index)
        if related_future is not None:
            try:
                artist_payload = related_future.result(timeout=10)
            except Exception:
                artist_payload = {}
            for index, related in enumerate((artist_payload.get("related_artists") or [])[:4]):
                add_artist_result(related, max(seed_weight - 0.7 - (index * 0.16), 0.35))

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
        }
        ranking_score = (
            seed_score
            + (similarities["taste"] * 5.1)
            + (similarities["artist"] * 4.7)
            + (similarities["query"] * 1.9)
            + (similarities["short"] * 1.3)
            + (similarities["long"] * 1.1)
        )
        normalized_name = _normalize_text(artist.get("name") or "")
        ranking_score += float((collaborative.get("artist_scores") or {}).get(normalized_name) or 0.0) * 0.4
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
    if not entry:
        return None
    video_id = entry.get("videoId") or entry.get("video_id") or entry.get("id")
    if not video_id:
        return None
    album_info = extract_album_info(entry) or {}
    return {
        "id": video_id,
        "title": entry.get("title") or entry.get("name") or "Unknown Track",
        "duration": parse_duration_seconds(
            entry.get("duration_seconds")
            or entry.get("lengthSeconds")
            or entry.get("length")
            or entry.get("duration")
        ),
        "thumbnail": extract_thumbnail(entry),
        "channel": extract_artist(entry),
        "album": album_info.get("title"),
        "album_id": album_info.get("id"),
    }

def _ytmusic_song_search(query: str, limit: int):
    results = []
    seen = set()

    def add_entry(entry):
        normalized = _normalize_song_result(entry)
        if not normalized:
            return
        track_id = normalized["id"]
        if not track_id or track_id in seen:
            return
        seen.add(track_id)
        results.append(normalized)

    try:
        raw_results = ytmusic.search(query, filter="songs", limit=limit)
    except Exception:
        raw_results = []

    for entry in raw_results:
        add_entry(entry)
        if len(results) >= limit:
            return results

    if len(results) < limit:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []

        for entry in fallback_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            add_entry(entry)
            if len(results) >= limit:
                break

    return results

def _ytdlp_song_search(query: str, limit: int):
    url = f"ytsearch{limit}:{query}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
    }
    results = []
    seen = set()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return results

    for entry in info.get("entries", []) or []:
        video_id = entry.get("id") or entry.get("url")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        results.append({
            "id": video_id,
            "title": entry.get("title"),
            "duration": parse_duration_seconds(entry.get("duration")),
            "thumbnail": extract_thumbnail(entry),
            "channel": extract_artist(entry),
            "album": None,
            "album_id": None,
        })
        if len(results) >= limit:
            break
    return results

def lookup_album_for_song(video_id: str, title: str, artist: str):
    candidates = []
    try:
        raw_results = ytmusic.search(f"{title} {artist}".strip(), filter="songs", limit=6)
    except Exception:
        raw_results = []

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
        "channel": extract_artist(data),
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

app = FastAPI(title="Auralis Proxy & Recommendation Engine")

# Allow Flutter emulator to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_recommendation_runtime():
    _recommendation_init_store_db()
    if RECOMMENDATION_ENABLE_SCHEDULER and not RECOMMENDATION_EXTERNAL_WORKER:
        _recommendation_start_scheduler()
        return
    if not RECOMMENDATION_EXTERNAL_WORKER:
        bootstrap_thread = Thread(
            target=_recommendation_bootstrap_once,
            name="recommendation-bootstrap",
            daemon=True,
        )
        bootstrap_thread.start()


@app.on_event("shutdown")
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
RECOMMENDATION_ROW_PAGE_SIZE = int(os.environ.get("RECOMMENDATION_ROW_PAGE_SIZE", "8"))
RECOMMENDATION_EMBED_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDATION_EMBED_CACHE_TTL_SECONDS", "1800"))
SEARCH_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("SEARCH_RESULT_CACHE_TTL_SECONDS", "600"))
DETAIL_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("DETAIL_RESULT_CACHE_TTL_SECONDS", "1800"))
RECOMMENDED_ARTISTS_CACHE_TTL_SECONDS = int(os.environ.get("RECOMMENDED_ARTISTS_CACHE_TTL_SECONDS", "600"))
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
ASSISTANT_EMBED_BACKEND = os.environ.get("ASSISTANT_EMBED_BACKEND", "local").strip().lower()
ASSISTANT_EMBED_DIM = int(os.environ.get("ASSISTANT_EMBED_DIM", "256"))
USE_LANGGRAPH_ASSISTANT = os.environ.get("USE_LANGGRAPH_ASSISTANT", "1").strip().lower() not in {"0", "false", "no"}
ASSISTANT_VECTOR_BACKEND = os.environ.get("ASSISTANT_VECTOR_BACKEND", "sqlite").strip().lower()
ASSISTANT_PGVECTOR_DSN = os.environ.get("ASSISTANT_PGVECTOR_DSN", os.environ.get("DATABASE_URL", "")).strip()
ASSISTANT_MEMORY_DB_PATH = os.path.join(os.getcwd(), "assistant_memory.sqlite")
RECOMMENDATION_SYNC_DATABASE_DSN = os.environ.get(
    "RECOMMENDATION_SYNC_DATABASE_DSN",
    ASSISTANT_PGVECTOR_DSN,
).strip()
RECOMMENDATION_STORE_DB_PATH = os.path.join(os.getcwd(), "recommendation_store.sqlite")
RECOMMENDATION_MODEL_CACHE_TTL_SECONDS = int(
    os.environ.get("RECOMMENDATION_MODEL_CACHE_TTL_SECONDS", "180")
)
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
RECOMMENDATION_MODEL_EXPORT_DIR = os.environ.get(
    "RECOMMENDATION_MODEL_EXPORT_DIR",
    os.path.join(os.getcwd(), "downloads", "recommendation_models"),
).strip()
stream_info_cache = {}
stream_info_inflight = {}
stream_info_lock = Lock()
stream_chunk_cache = {}
stream_chunk_lock = Lock()
stream_chunk_inflight = {}
stream_chunk_inflight_lock = Lock()
prepare_metrics = deque(maxlen=180)
prepare_metrics_lock = Lock()
recommendation_cache = {}
recommendation_cache_lock = Lock()
home_candidates_cache = {"expires_at": 0, "results": []}
home_candidates_lock = Lock()
recommendation_profile_cache = {}
recommendation_profile_lock = Lock()
recommendation_track_details_cache = {}
recommendation_track_details_lock = Lock()
recommendation_feed_sessions = {}
recommendation_feed_index = {}
recommendation_feed_lock = Lock()
search_result_cache = {
    "tracks": {},
    "albums": {},
    "artists_direct": {},
    "artists": {},
    "recommended_artists": {},
    "suggestions": {},
}
search_result_cache_lock = Lock()
detail_result_cache = {
    "track": {},
    "album": {},
    "artist": {},
}
detail_result_cache_lock = Lock()
recommendation_embedding_cache = {
    "track": {},
    "artist": {},
    "text": {},
    "album": {},
}
recommendation_embedding_lock = Lock()
recommendation_model_cache = {
    "artifact": None,
    "source_signature": "",
    "expires_at": 0,
}
recommendation_model_lock = Lock()
recommendation_store_lock = Lock()
recommendation_scheduler_stop = Event()
recommendation_scheduler_thread = None
stream_warm_executor = ThreadPoolExecutor(max_workers=STREAM_WARM_WORKERS)
recommendation_executor = ThreadPoolExecutor(max_workers=6)
upstream_http = requests.Session()
ollama_http = requests.Session()
assistant_memory_lock = Lock()

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    seed_id: str = None
    seed_ids: List[str] = Field(default_factory=list)
    taste_queries: List[str] = Field(default_factory=list)
    artist_hints: List[str] = Field(default_factory=list)
    album_hints: List[str] = Field(default_factory=list)
    avoid_ids: List[str] = Field(default_factory=list)
    offset: int = 0
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    row_id: Optional[str] = None
    force_refresh: bool = False
    recent_track_ids: List[str] = Field(default_factory=list)
    top_track_ids: List[str] = Field(default_factory=list)
    recent_queries: List[str] = Field(default_factory=list)
    playlist_names: List[str] = Field(default_factory=list)
    library_track_ids: List[str] = Field(default_factory=list)
    offline_track_ids: List[str] = Field(default_factory=list)
    recent_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    top_track_snapshots: List[Dict[str, Any]] = Field(default_factory=list)
    last_played_tracks: List[Dict[str, Any]] = Field(default_factory=list)


class RecommendationInteractionEventRequest(BaseModel):
    user_scope_id: str = "guest"
    track_id: str
    event_type: str = "play"
    artist_name: Optional[str] = None
    source: str = "app"
    occurred_at: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecommendationSearchEventRequest(BaseModel):
    user_scope_id: str = "guest"
    query: str
    result_count: int = 0
    source: str = "app"
    occurred_at: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecommendationModelTrainRequest(BaseModel):
    force_sync: bool = False


def _cache_lookup(cache_root, lock: Lock, namespace: str, key: str):
    now = time.time()
    with lock:
        namespace_cache = cache_root.get(namespace) or {}
        cached = namespace_cache.get(key)
        if cached and cached.get("expires_at", 0) > now:
            return cached.get("value")
        if cached:
            namespace_cache.pop(key, None)
    return None


def _cache_store(cache_root, lock: Lock, namespace: str, key: str, value, ttl_seconds: int):
    with lock:
        namespace_cache = cache_root.setdefault(namespace, {})
        namespace_cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
        }


def _search_cache_key(query: str, limit: int) -> str:
    normalized_query = _normalize_text(query)
    return f"{normalized_query}|{max(int(limit or 0), 0)}"


def _recommended_artists_cache_key(req: SearchRequest) -> str:
    payload = {
        "user_scope_id": _recommendation_trim_text(req.user_scope_id or "guest"),
        "limit": max(int(req.limit or 0), 0),
        "artist_hints": _recommendation_unique_strings(req.artist_hints, 12),
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
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

class DownloadRequest(BaseModel):
    video_id: str
    title: str = ""

class WarmStreamRequest(BaseModel):
    video_ids: List[str] = Field(default_factory=list)
    current_video_id: Optional[str] = None
    active_queue: bool = False
    lookahead: int = 0

class AssistantConversationMessage(BaseModel):
    role: str
    content: str

class AssistantPlaylistSummary(BaseModel):
    id: str
    name: str
    track_count: int = 0

class AssistantLibraryTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    album: Optional[str] = None

class AssistantContextTrack(BaseModel):
    id: Optional[str] = None
    title: str = ""
    artist: str = ""
    channel: Optional[str] = None
    album: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: int = 0
    reason: Optional[str] = None

class AssistantChatRequest(BaseModel):
    message: str
    user_scope_id: str = "guest"
    session_id: Optional[str] = None
    thinking_mode: bool = True
    force_refresh: bool = False
    conversation: List[AssistantConversationMessage] = Field(default_factory=list)
    last_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    last_playlist_draft_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    recent_assistant_tracks: List[AssistantContextTrack] = Field(default_factory=list)
    playlist_summaries: List[AssistantPlaylistSummary] = Field(default_factory=list)
    recent_track_ids: List[str] = Field(default_factory=list)
    recent_queries: List[str] = Field(default_factory=list)
    library_tracks: List[AssistantLibraryTrack] = Field(default_factory=list)
    limit: int = 10


class AssistantSessionCreateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None


class AssistantSessionUpdateRequest(BaseModel):
    user_scope_id: str = "guest"
    title: Optional[str] = None
    archived: Optional[bool] = None
    pinned: Optional[bool] = None

def _assistant_db_connection():
    connection = sqlite3.connect(ASSISTANT_MEMORY_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection

def _assistant_pgvector_enabled():
    return (
        ASSISTANT_VECTOR_BACKEND == "pgvector"
        and psycopg is not None
        and bool(ASSISTANT_PGVECTOR_DSN)
    )

def _assistant_pgvector_connection():
    if not _assistant_pgvector_enabled():
        return None
    connection = psycopg.connect(ASSISTANT_PGVECTOR_DSN)
    connection.autocommit = True
    return connection

def _assistant_vector_literal(embedding: Optional[List[float]]):
    if not embedding:
        return None
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"

def _assistant_init_memory_db():
    with assistant_memory_lock:
        if _assistant_pgvector_enabled():
            connection = _assistant_pgvector_connection()
            if connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        cursor.execute(
                            f"""
                            CREATE TABLE IF NOT EXISTS assistant_memory (
                                id TEXT PRIMARY KEY,
                                scope_id TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                content TEXT NOT NULL,
                                metadata_json JSONB,
                                embedding VECTOR({ASSISTANT_EMBED_DIM}),
                                created_at DOUBLE PRECISION NOT NULL
                            )
                            """
                        )
                        cursor.execute(
                            "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                            "ON assistant_memory(scope_id, created_at DESC)"
                        )
                    return
                except Exception:
                    pass
                finally:
                    connection.close()

        connection = _assistant_db_connection()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_memory (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    embedding_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                "ON assistant_memory(scope_id, created_at DESC)"
            )
            connection.commit()
        finally:
            connection.close()

def _assistant_safe_scope_id(scope_id: Optional[str]):
    cleaned = (scope_id or "guest").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", cleaned)
    return cleaned or "guest"


def _assistant_now_timestamp():
    return time.time()


def _assistant_default_session_title(message: Optional[str]):
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return "New chat"
    return text[:72].rstrip(" .,!?:;") or "New chat"


def _assistant_preview_text(text: Optional[str], limit: int = 180):
    preview = re.sub(r"\s+", " ", (text or "").strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "…"


def _assistant_init_session_db():
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_message_preview TEXT,
                last_mode TEXT,
                archived_at REAL,
                pinned_at REAL
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assistant_sessions)").fetchall()
        }
        if "archived_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN archived_at REAL")
        if "pinned_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN pinned_at REAL")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user_updated "
            "ON assistant_sessions(user_id, updated_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                payload_json TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_messages_session_time "
            "ON assistant_messages(session_id, created_at ASC)"
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_session_summary_from_row(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"] or "New chat",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_message_preview": row["last_message_preview"] or "",
        "last_mode": row["last_mode"] or "",
        "archived_at": row["archived_at"],
        "pinned_at": row["pinned_at"],
    }


def _assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        query = (
            "SELECT id, user_id, title, created_at, updated_at, "
            "last_message_preview, last_mode, archived_at, pinned_at "
            "FROM assistant_sessions WHERE user_id = ?"
        )
        params: List[Any] = [scope_id]
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY pinned_at IS NULL ASC, pinned_at DESC, updated_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [_assistant_session_summary_from_row(row) for row in rows]
    finally:
        connection.close()


def _assistant_get_session(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        row = connection.execute(
            """
            SELECT id, user_id, title, created_at, updated_at,
                   last_message_preview, last_mode, archived_at, pinned_at
            FROM assistant_sessions
            WHERE id = ? AND user_id = ?
            """,
            [session_id, scope_id],
        ).fetchone()
        return _assistant_session_summary_from_row(row)
    finally:
        connection.close()


def _assistant_create_session(
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    seed_message: Optional[str] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    session_id = str(uuid.uuid4())
    now = _assistant_now_timestamp()
    resolved_title = (title or "").strip() or _assistant_default_session_title(seed_message)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_sessions (
                id, user_id, title, created_at, updated_at,
                last_message_preview, last_mode, archived_at, pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                session_id,
                scope_id,
                resolved_title,
                now,
                now,
                _assistant_preview_text(seed_message),
                "",
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return _assistant_get_session(session_id, scope_id)


def _assistant_touch_session(
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    last_message_preview: Optional[str] = None,
    last_mode: Optional[str] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [_assistant_now_timestamp()]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if last_message_preview is not None:
        updates.append("last_message_preview = ?")
        params.append(_assistant_preview_text(last_message_preview))
    if last_mode is not None:
        updates.append("last_mode = ?")
        params.append((last_mode or "").strip())
    params.extend([session_id, scope_id])

    connection = _assistant_db_connection()
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_store_session_message(
    session_id: str,
    user_scope_id: str,
    *,
    role: str,
    content: str,
    payload: Optional[Dict[str, Any]] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_messages (
                id, session_id, user_id, role, content, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                session_id,
                scope_id,
                role,
                (content or "").strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                _assistant_now_timestamp(),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _assistant_get_session_messages(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, session_id, user_id, role, content, payload_json, created_at
            FROM assistant_messages
            WHERE session_id = ? AND user_id = ?
            ORDER BY created_at ASC
            """,
            [session_id, scope_id],
        ).fetchall()
    finally:
        connection.close()

    messages = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        messages.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return messages


def _assistant_get_session_detail(session_id: str, user_scope_id: str):
    session = _assistant_get_session(session_id, user_scope_id)
    if session is None:
        return None
    return {
        "session": session,
        "messages": _assistant_get_session_messages(session_id, user_scope_id),
    }


def _assistant_update_session(
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
    pinned: Optional[bool] = None,
):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [_assistant_now_timestamp()]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if archived is not None:
        updates.append("archived_at = ?")
        params.append(_assistant_now_timestamp() if archived else None)
    if pinned is not None:
        updates.append("pinned_at = ?")
        params.append(_assistant_now_timestamp() if pinned else None)
    params.extend([session_id, scope_id])

    connection = _assistant_db_connection()
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()
    return _assistant_get_session(session_id, scope_id)


def _assistant_delete_session(session_id: str, user_scope_id: str):
    scope_id = _assistant_safe_scope_id(user_scope_id)
    connection = _assistant_db_connection()
    try:
        connection.execute(
            "DELETE FROM assistant_messages WHERE session_id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        cursor = connection.execute(
            "DELETE FROM assistant_sessions WHERE id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()

def _assistant_embed_texts(texts: List[str]):
    payload_texts = [text.strip() for text in texts if text and text.strip()]
    if not payload_texts:
        return []

    def local_embed(value: str):
        vector = [0.0] * ASSISTANT_EMBED_DIM
        tokens = _query_tokens(value)
        if not tokens:
            tokens = [token for token in re.split(r"[^a-z0-9]+", _normalize_text(value)) if token]
        if not tokens:
            return vector
        for index, token in enumerate(tokens):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % ASSISTANT_EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.15 if index < 6 else 1.0
            vector[slot] += sign * weight
        norm = math.sqrt(sum(item * item for item in vector))
        if norm > 0:
            vector = [item / norm for item in vector]
        return vector

    if ASSISTANT_EMBED_BACKEND == "ollama":
        try:
            response = ollama_http.post(
                f"{OLLAMA_BASE_URL}/embed",
                headers=_ollama_headers(),
                json={
                    "model": OLLAMA_EMBED_MODEL,
                    "input": payload_texts,
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                return embeddings
            embedding = data.get("embedding")
            if isinstance(embedding, list) and embedding:
                return [embedding]
        except Exception:
            pass
    return [local_embed(text) for text in payload_texts]

def _assistant_cosine_similarity(a: List[float], b: List[float]):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


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


def _recommendation_track_text(track: Optional[Dict[str, Any]]) -> str:
    if not isinstance(track, dict):
        return ""
    title = _recommendation_trim_text(track.get("title")) or "Unknown Track"
    artist = _recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    ) or "Unknown Artist"
    album = _recommendation_trim_text(track.get("album") or track.get("album_title"))
    parts = [f"track {title}", f"artist {artist}"]
    if album:
        parts.append(f"album {album}")
    return ". ".join(parts)


def _recommendation_artist_text(artist: Optional[Dict[str, Any]]) -> str:
    if not isinstance(artist, dict):
        return ""
    name = _recommendation_trim_text(artist.get("name")) or "Unknown Artist"
    description = _recommendation_trim_text(
        artist.get("description")
        or artist.get("subtitle")
        or artist.get("type")
    )
    if description:
        return f"artist {name}. {description}"
    return f"artist {name}"


def _recommendation_album_text(album: Optional[Dict[str, Any]]) -> str:
    if not isinstance(album, dict):
        return ""
    title = _recommendation_trim_text(album.get("title")) or "Unknown Album"
    artist = _recommendation_trim_text(album.get("artist")) or "Unknown Artist"
    year = _recommendation_trim_text(str(album.get("year") or ""))
    parts = [f"album {title}", f"artist {artist}"]
    if year:
        parts.append(f"year {year}")
    return ". ".join(parts)


def _recommendation_artist_embedding_key(artist: Optional[Dict[str, Any]]) -> str:
    if not isinstance(artist, dict):
        return ""
    artist_id = _recommendation_trim_text(artist.get("id") or artist.get("browseId"))
    if artist_id:
        return f"artist:{artist_id}"
    name = _recommendation_trim_text(artist.get("name"))
    if not name:
        return ""
    return f"artist:{_normalize_text(name)}"


def _recommendation_album_embedding_key(album: Optional[Dict[str, Any]]) -> str:
    if not isinstance(album, dict):
        return ""
    album_id = _recommendation_trim_text(album.get("id") or album.get("browseId"))
    if album_id:
        return f"album:{album_id}"
    title = _recommendation_trim_text(album.get("title"))
    artist = _recommendation_trim_text(album.get("artist"))
    if not title and not artist:
        return ""
    return f"album:{_normalize_text(title)}|{_normalize_text(artist)}"


def _recommendation_track_embedding_key(track: Optional[Dict[str, Any]]) -> str:
    if not isinstance(track, dict):
        return ""
    track_id = _recommendation_trim_text(track.get("id") or track.get("videoId"))
    if track_id:
        return f"track:{track_id}"
    title = _recommendation_trim_text(track.get("title"))
    artist = _recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    album = _recommendation_trim_text(track.get("album") or track.get("album_title"))
    if not any([title, artist, album]):
        return ""
    return f"track:{_normalize_text(title)}|{_normalize_text(artist)}|{_normalize_text(album)}"


def _recommendation_text_embedding_key(label: str, value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    return f"{label}:{hashlib.sha1(normalized.encode('utf-8')).hexdigest()}"


def _recommendation_cached_embedding(namespace: str, key: str):
    if not namespace or not key:
        return None
    return _cache_lookup(
        recommendation_embedding_cache,
        recommendation_embedding_lock,
        namespace,
        key,
    )


def _recommendation_store_embedding(namespace: str, key: str, value: List[float]):
    if not namespace or not key or not value:
        return
    _cache_store(
        recommendation_embedding_cache,
        recommendation_embedding_lock,
        namespace,
        key,
        list(value),
        RECOMMENDATION_EMBED_CACHE_TTL_SECONDS,
    )


def _recommendation_embed_entries(namespace: str, keyed_texts):
    results = {}
    pending = []
    for key, text in keyed_texts:
        if not key or not text:
            continue
        cached = _recommendation_cached_embedding(namespace, key)
        if cached is not None:
            results[key] = list(cached)
            continue
        pending.append((key, text))

    if pending:
        embeddings = _assistant_embed_texts([text for _, text in pending])
        for (key, _text), embedding in zip(pending, embeddings):
            normalized = _vector_normalize(embedding)
            if normalized:
                _recommendation_store_embedding(namespace, key, normalized)
                results[key] = normalized

    return results


def _recommendation_track_embeddings(tracks):
    keyed_texts = []
    key_order = []
    for track in tracks or []:
        key = _recommendation_track_embedding_key(track)
        text = _recommendation_track_text(track)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, track))
    embeddings = _recommendation_embed_entries("track", keyed_texts)
    resolved = {}
    for key, track in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved


def _recommendation_artist_embeddings(artists):
    keyed_texts = []
    key_order = []
    for artist in artists or []:
        key = _recommendation_artist_embedding_key(artist)
        text = _recommendation_artist_text(artist)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, artist))
    embeddings = _recommendation_embed_entries("artist", keyed_texts)
    resolved = {}
    for key, artist in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved


def _recommendation_album_embeddings(albums):
    keyed_texts = []
    key_order = []
    for album in albums or []:
        key = _recommendation_album_embedding_key(album)
        text = _recommendation_album_text(album)
        if not key or not text:
            continue
        keyed_texts.append((key, text))
        key_order.append((key, album))
    embeddings = _recommendation_embed_entries("album", keyed_texts)
    resolved = {}
    for key, album in key_order:
        resolved[key] = embeddings.get(key) or []
    return resolved

def _assistant_store_memory(scope_id: str, kind: str, content: str, metadata: Optional[Dict[str, Any]] = None):
    text = (content or "").strip()
    if not text:
        return

    embeddings = _assistant_embed_texts([text])
    embedding = embeddings[0] if embeddings else None
    if _assistant_pgvector_enabled():
        connection = _assistant_pgvector_connection()
        if connection is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding, created_at)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            _assistant_safe_scope_id(scope_id),
                            kind,
                            text,
                            json.dumps(metadata or {}, ensure_ascii=False),
                            _assistant_vector_literal(embedding),
                            time.time(),
                        ),
                    )
                return
            except Exception:
                pass
            finally:
                connection.close()

    connection = _assistant_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                _assistant_safe_scope_id(scope_id),
                kind,
                text,
                json.dumps(metadata or {}, ensure_ascii=False),
                json.dumps(embedding) if embedding is not None else None,
                time.time(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

def _assistant_query_memory(scope_id: str, queries: List[str], limit: int = 6):
    cleaned_queries = [query.strip() for query in queries if query and query.strip()]
    if not cleaned_queries:
        return []

    query_embeddings = _assistant_embed_texts(cleaned_queries[:3])
    if not query_embeddings:
        return []

    if _assistant_pgvector_enabled():
        connection = _assistant_pgvector_connection()
        if connection is not None:
            try:
                rows = []
                with connection.cursor() as cursor:
                    for query_embedding in query_embeddings[:3]:
                        cursor.execute(
                            """
                            SELECT id, kind, content, metadata_json::text, created_at,
                                   (1 - (embedding <=> %s::vector)) AS score
                            FROM assistant_memory
                            WHERE scope_id = %s
                            ORDER BY embedding <=> %s::vector ASC, created_at DESC
                            LIMIT %s
                            """,
                            (
                                _assistant_vector_literal(query_embedding),
                                _assistant_safe_scope_id(scope_id),
                                _assistant_vector_literal(query_embedding),
                                max(limit * 3, 12),
                            ),
                        )
                        rows.extend(cursor.fetchall())
                deduped = {}
                for row in rows:
                    row_id = row[0]
                    current = deduped.get(row_id)
                    candidate = {
                        "id": row[0],
                        "kind": row[1],
                        "content": row[2],
                        "metadata": json.loads(row[3] or "{}"),
                        "score": float(row[5] or 0),
                        "created_at": row[4],
                    }
                    if current is None or candidate["score"] > current["score"]:
                        deduped[row_id] = candidate
                ranked = sorted(
                    deduped.values(),
                    key=lambda item: (item["score"], item["created_at"]),
                    reverse=True,
                )
                return ranked[:limit]
            except Exception:
                pass
            finally:
                connection.close()

    connection = _assistant_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, kind, content, metadata_json, embedding_json, created_at
            FROM assistant_memory
            WHERE scope_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (_assistant_safe_scope_id(scope_id),),
        ).fetchall()
    finally:
        connection.close()

    scored = []
    for row in rows:
        raw_embedding = row["embedding_json"]
        if not raw_embedding:
            continue
        try:
            embedding = json.loads(raw_embedding)
        except Exception:
            continue
        best_score = 0.0
        for query_embedding in query_embeddings:
            best_score = max(best_score, _assistant_cosine_similarity(query_embedding, embedding))
        scored.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "score": best_score,
                "created_at": row["created_at"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["created_at"]), reverse=True)
    return scored[:limit]

def _assistant_track_from_context(track):
    if isinstance(track, BaseModel):
        track = track.model_dump() if hasattr(track, "model_dump") else track.dict()
    raw = dict(track or {})
    track_id = (raw.get("id") or "").strip()
    if not track_id:
        return None
    artist = raw.get("artist") or raw.get("channel") or ""
    return {
        "id": track_id,
        "title": raw.get("title") or "Unknown Track",
        "channel": artist,
        "artist": artist,
        "album": raw.get("album"),
        "thumbnail": raw.get("thumbnail"),
        "duration": parse_duration_seconds(raw.get("duration")),
        "reason": raw.get("reason"),
    }

def _assistant_all_context_tracks(req: AssistantChatRequest):
    groups = {
        "last_assistant_tracks": req.last_assistant_tracks,
        "last_playlist_draft_tracks": req.last_playlist_draft_tracks,
        "recent_assistant_tracks": req.recent_assistant_tracks,
    }
    normalized = {}
    for key, tracks in groups.items():
        normalized[key] = [
            track
            for track in (_assistant_track_from_context(entry) for entry in tracks)
            if track is not None
        ]
    return normalized

def _assistant_tool_search_tracks(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    return _search_tracks_blended(query, limit)


def _assistant_tool_search_albums(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    return _search_albums_blended(query, limit)


def _assistant_tool_search_artists_direct(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    cache_key = _search_cache_key(query, limit)
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "artists_direct",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached]
    try:
        raw_results = ytmusic.search(query, filter="artists", limit=limit)
    except Exception:
        raw_results = []
    artists = normalize_artist_results(raw_results)
    if not artists:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []
        artists = normalize_artist_results(fallback_results)
    normalized_query = _normalize_text(query)
    tokens = _query_tokens(query)

    def artist_score(item):
        name = _normalize_text(item.get("name"))
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
            -len(_normalize_text(item.get("name"))),
        ),
        reverse=True,
    )
    results = artists[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "artists_direct",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in results]


def _artist_names_from_track_query(query: str, limit: int):
    normalized_query = _normalize_text(query)
    tokens = _query_tokens(query)
    if not normalized_query and not tokens:
        return []

    candidates = []
    raw_tracks = _ytmusic_song_search(query, max(limit * 3, 12))
    for index, track in enumerate(raw_tracks):
        title_text = _normalize_text(track.get("title"))
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
        artist_names = extract_artist_names(track)
        for artist_name in artist_names:
            candidates.append((artist_name, max(match_score - (index * 0.18), 0.6)))

    weighted = {}
    for artist_name, score in candidates:
        weighted[artist_name] = max(weighted.get(artist_name, 0.0), score)
    ranked = sorted(weighted.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def _assistant_tool_search_artists(query: str, limit: int):
    query = (query or "").strip()
    if not query:
        return []
    cache_key = _search_cache_key(query, limit)
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "artists",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached]

    combined = {}

    def upsert_artist(item, source_score: float):
        artist_id = (item.get("id") or "").strip()
        artist_name = (item.get("name") or "").strip()
        normalized_name = _normalize_text(artist_name)
        if not artist_id or not artist_name or not normalized_name:
            return
        current = combined.get(artist_id)
        score = source_score
        if current is None or score > current.get("score", 0):
            combined[artist_id] = {
                **item,
                "score": score,
            }

    direct_artists = _assistant_tool_search_artists_direct(query, max(limit * 2, 6))
    normalized_query = _normalize_text(query)
    tokens = _query_tokens(query)
    for artist in direct_artists:
        name = _normalize_text(artist.get("name"))
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

    for artist_name, seed_score in _artist_names_from_track_query(query, max(limit, 4)):
        for artist in _assistant_tool_search_artists_direct(artist_name, 2):
            upsert_artist(artist, seed_score + 1.6)

    artists = list(combined.values())
    artists.sort(
        key=lambda item: (
            item.get("score", 0),
            -len(_normalize_text(item.get("name") or "")),
        ),
        reverse=True,
    )
    results = artists[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "artists",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in results]


def _build_track_details_payload(video_id: str):
    cache_key = _recommendation_trim_text(video_id)
    if cache_key:
        cached = _cache_lookup(
            detail_result_cache,
            detail_result_cache_lock,
            "track",
            cache_key,
        )
        if cached is not None:
            return dict(cached)

    release_date = ""
    artist = ""
    album_title = ""
    album_id = None

    try:
        song = ytmusic.get_song(video_id)
        if "microformat" in song and "microformatDataRenderer" in song["microformat"]:
            release_date = song["microformat"]["microformatDataRenderer"].get("publishDate", "")
        vd = song.get("videoDetails", {})
        artist = vd.get("author", "")
        song_album = extract_album_info(song) or extract_album_info(vd)
        if song_album:
            album_title = song_album.get("title") or ""
            album_id = song_album.get("id")
    except Exception:
        pass

    watch = ytmusic.get_watch_playlist(videoId=video_id)
    video_details = watch.get("videoDetails", {})
    track_title = video_details.get("title") or ""
    if not artist:
        artist = extract_artist(video_details)
    if not album_title:
        looked_up_album = lookup_album_for_song(video_id, track_title, artist)
        if looked_up_album:
            album_title = looked_up_album.get("title") or ""
            album_id = looked_up_album.get("id")

    similar_tracks = []
    for track in watch.get("tracks", []):
        if track.get("videoId") == video_id or not track.get("videoId"):
            continue
        similar_tracks.append({
            "id": track["videoId"],
            "title": track.get("title"),
            "duration": track.get("length") or track.get("duration_seconds") or 0,
            "thumbnail": extract_thumbnail(track),
            "channel": extract_artist(track),
            "album": (extract_album_info(track) or {}).get("title"),
            "album_id": (extract_album_info(track) or {}).get("id"),
        })
    payload = {
        "status": "success",
        "video_id": video_id,
        "title": track_title,
        "author": artist,
        "thumbnail": extract_thumbnail(video_details),
        "duration": video_details.get("lengthSeconds"),
        "release_date": release_date,
        "album": album_title,
        "album_title": album_title,
        "album_id": album_id,
        "lyrics_available": bool(watch.get("lyrics")),
        "similar_tracks": similar_tracks,
    }
    if cache_key:
        _cache_store(
            detail_result_cache,
            detail_result_cache_lock,
            "track",
            cache_key,
            payload,
            DETAIL_RESULT_CACHE_TTL_SECONDS,
        )
    return payload


def _build_album_details_payload(album_id: str):
    cache_key = _recommendation_trim_text(album_id)
    if cache_key:
        cached = _cache_lookup(
            detail_result_cache,
            detail_result_cache_lock,
            "album",
            cache_key,
        )
        if cached is not None:
            return dict(cached)

    album = ytmusic.get_album(album_id)
    album_thumbnail = extract_thumbnail(album)
    album_artist = extract_artist(album)
    tracks = []

    for entry in album.get("tracks", []):
        video_id = entry.get("videoId")
        if not video_id:
            continue
        tracks.append({
            "id": video_id,
            "title": entry.get("title"),
            "duration": parse_duration_seconds(
                entry.get("duration_seconds")
                or entry.get("duration")
                or entry.get("length")
            ),
            "thumbnail": extract_thumbnail(entry) or album_thumbnail,
            "channel": extract_artist(entry) or album_artist,
            "album": album.get("title"),
            "album_title": album.get("title"),
            "album_id": album_id,
        })

    payload = {
        "status": "success",
        "id": album_id,
        "title": album.get("title"),
        "artist": album_artist,
        "thumbnail": album_thumbnail,
        "year": album.get("year") or "",
        "track_count": len(tracks),
        "tracks": tracks,
    }
    if cache_key:
        _cache_store(
            detail_result_cache,
            detail_result_cache_lock,
            "album",
            cache_key,
            payload,
            DETAIL_RESULT_CACHE_TTL_SECONDS,
        )
    return payload


def _assistant_tool_get_track_details(video_id: str):
    video_id = (video_id or "").strip()
    if not video_id:
        return {}
    try:
        return _build_track_details_payload(video_id)
    except Exception:
        return {}


def _assistant_tool_get_album_details(album_id: str):
    album_id = (album_id or "").strip()
    if not album_id:
        return {}
    try:
        return _build_album_details_payload(album_id)
    except Exception:
        return {}


def _assistant_tool_get_similar_tracks(video_id: str, limit: int):
    details = _assistant_tool_get_track_details(video_id)
    similar = details.get("similar_tracks")
    if not isinstance(similar, list):
        return []
    return similar[: max(1, min(limit, 12))]


def _track_metadata_incomplete(track: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(track, dict):
        return True
    title = _normalize_text(track.get("title"))
    artist = _normalize_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    return not title or title == "unknown track" or not artist or artist == "unknown artist"


def _merge_track_metadata(primary: Dict[str, Any], fallback: Optional[Dict[str, Any]]):
    if fallback is None:
        return dict(primary)
    merged = dict(primary)
    for key in ("title", "thumbnail", "album", "album_id"):
        if not _recommendation_trim_text(merged.get(key)):
            if _recommendation_trim_text(fallback.get(key)):
                merged[key] = fallback.get(key)
    current_artist = _recommendation_trim_text(
        merged.get("channel") or merged.get("author") or merged.get("artist")
    )
    fallback_artist = _recommendation_trim_text(
        fallback.get("channel") or fallback.get("author") or fallback.get("artist")
    )
    if not current_artist or _normalize_text(current_artist) == "unknown artist":
        if fallback_artist:
            merged["channel"] = fallback_artist
            merged["author"] = fallback_artist
            merged["artist"] = fallback_artist
    if parse_duration_seconds(merged.get("duration")) <= 0:
        fallback_duration = parse_duration_seconds(fallback.get("duration"))
        if fallback_duration > 0:
            merged["duration"] = fallback_duration
    return normalize_recommendation_track(merged) or merged


def _recommendation_enrich_track_metadata(track: Dict[str, Any]):
    normalized = normalize_recommendation_track(track) or dict(track)
    track_id = _recommendation_trim_text(normalized.get("id"))
    if not track_id or not _track_metadata_incomplete(normalized):
        return normalized
    details_track = _recommendation_fetch_track_for_id(track_id)
    if details_track is None:
        return normalized
    enriched = _merge_track_metadata(normalized, details_track)
    _recommendation_store_cached_track(track_id, enriched)
    return enriched


def _search_artist_seed_tracks(query: str, limit: int):
    artists = _assistant_tool_search_artists(query, 2)
    if not artists:
        return []
    normalized_query = _normalize_text(query)
    query_tokens = _query_tokens(query)
    tracks = []
    seen = set()
    for artist in artists:
        artist_name = _normalize_text(artist.get("name"))
        if normalized_query and artist_name:
            if normalized_query not in artist_name and not any(
                token in artist_name for token in query_tokens
            ):
                continue
        artist_id = _recommendation_trim_text(artist.get("id"))
        if not artist_id:
            continue
        try:
            payload = _build_artist_details_payload(artist_id)
        except Exception:
            payload = {}
        for track in payload.get("top_songs") or []:
            normalized = normalize_recommendation_track(track)
            track_id = _recommendation_trim_text((normalized or {}).get("id"))
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            tracks.append(normalized)
            if len(tracks) >= limit:
                return tracks
    return tracks


def _search_albums_for_artist_name(artist_name: str):
    normalized_artist = _recommendation_trim_text(artist_name)
    if not normalized_artist:
        return {}
    direct_artists = _assistant_tool_search_artists_direct(normalized_artist, 1)
    if not direct_artists:
        return {}
    artist_id = _recommendation_trim_text(direct_artists[0].get("id"))
    if not artist_id:
        return {}
    try:
        return _build_artist_details_payload(artist_id)
    except Exception:
        return {}


def _search_albums_blended(query: str, limit: int):
    query = (query or "").strip()
    limit = max(1, min(limit, 18))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit)
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "albums",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached]

    results = []
    seen = set()

    def add_albums(albums, max_to_add: Optional[int] = None):
        added = 0
        for raw_album in albums or []:
            if not isinstance(raw_album, dict):
                continue
            album_id = _recommendation_trim_text(raw_album.get("id"))
            title = _recommendation_trim_text(raw_album.get("title"))
            artist = _recommendation_trim_text(raw_album.get("artist"))
            key = album_id or f"{_normalize_text(title)}|{_normalize_text(artist)}"
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(raw_album)
            added += 1
            if len(results) >= limit:
                break
            if max_to_add is not None and added >= max_to_add:
                break

    try:
        direct_results = ytmusic.search(query, filter="albums", limit=max(limit, 8))
    except Exception:
        direct_results = []
    direct_albums = normalize_album_results(direct_results)
    add_albums(direct_albums, max_to_add=min(max(4, limit // 2), 8))

    track_anchor_future = recommendation_executor.submit(
        _ytmusic_song_search,
        query,
        max(6, limit),
    )
    query_artist_future = recommendation_executor.submit(
        _assistant_tool_search_artists,
        query,
        2,
    )

    try:
        track_anchor_results = track_anchor_future.result(timeout=8)
    except Exception:
        track_anchor_results = []

    anchor_artist_payload_futures = {}
    for index, track in enumerate(track_anchor_results[:4]):
        artist_name = _recommendation_trim_text(track.get("channel"))
        if not artist_name:
            continue
        anchor_artist_payload_futures[index] = recommendation_executor.submit(
            _search_albums_for_artist_name,
            artist_name,
        )

    for index, track in enumerate(track_anchor_results[:4]):
        album_title = _recommendation_trim_text(track.get("album"))
        album_id = _recommendation_trim_text(track.get("album_id"))
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
            artist_payload = anchor_artist_payload_futures[index].result(timeout=8)
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
            direct_artists = query_artist_future.result(timeout=8)
        except Exception:
            direct_artists = []
        for artist in direct_artists:
            artist_id = _recommendation_trim_text(artist.get("id"))
            if not artist_id:
                continue
            try:
                artist_payload = _build_artist_details_payload(artist_id)
            except Exception:
                artist_payload = {}
            add_albums(artist_payload.get("albums") or [], max_to_add=4)
            if len(results) >= limit:
                break

    if len(results) < limit:
        try:
            fallback_results = ytmusic.search(query, limit=max(limit * 3, 12))
        except Exception:
            fallback_results = []
        add_albums(normalize_album_results(fallback_results))

    final_results = results[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "albums",
        cache_key,
        final_results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in final_results]


def _search_tracks_blended(query: str, limit: int):
    query = (query or "").strip()
    limit = max(1, min(limit, 30))
    if not query:
        return []
    cache_key = _search_cache_key(query, limit)
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "tracks",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached]

    direct_pool = _ytmusic_song_search(query, max(limit, 14))
    if not direct_pool:
        direct_pool = _ytdlp_song_search(query, max(limit, 14))

    results = []
    seen = set()

    def add_tracks(tracks, max_to_add: Optional[int] = None):
        added = 0
        for raw_track in tracks or []:
            normalized = normalize_recommendation_track(raw_track)
            if normalized is None:
                continue
            track_id = _recommendation_trim_text(normalized.get("id"))
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
    query_tokens = _query_tokens(query)
    similar_tracks_future = None
    if anchor_track is not None and anchor_track.get("id"):
        anchor_text = _normalize_text(
            f"{anchor_track.get('title') or ''} {anchor_track.get('channel') or ''}"
        )
        if not query_tokens or any(token in anchor_text for token in query_tokens):
            similar_tracks_future = recommendation_executor.submit(
                _assistant_tool_get_similar_tracks,
                anchor_track["id"],
                max(6, min(10, limit // 2)),
            )
    artist_seed_future = recommendation_executor.submit(
        _search_artist_seed_tracks,
        query,
        max(4, min(8, limit // 3)),
    )

    if similar_tracks_future is not None:
        try:
            similar_tracks = similar_tracks_future.result(timeout=8)
        except Exception:
            similar_tracks = []
        add_tracks(
            similar_tracks,
            max_to_add=max(6, min(10, limit // 2)),
        )

    if len(results) < limit:
        try:
            artist_seed_tracks = artist_seed_future.result(timeout=8)
        except Exception:
            artist_seed_tracks = []
        add_tracks(
            artist_seed_tracks,
            max_to_add=max(4, min(8, limit // 3)),
        )

    if len(results) < limit:
        add_tracks(direct_pool)

    if len(results) < limit:
        try:
            broad_results = ytmusic.search(query, limit=max(limit * 4, 24))
        except Exception:
            broad_results = []
        filtered = []
        for entry in broad_results:
            result_type = (entry.get("resultType") or entry.get("type") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue
            filtered.append(entry)
        add_tracks(filtered)

    if len(results) < limit:
        add_tracks(_ytdlp_song_search(query, limit))

    final_results = results[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "tracks",
        cache_key,
        final_results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in final_results]


def _semantic_search_cache_key(req: SearchRequest, namespace: str) -> str:
    payload = {
        "namespace": namespace,
        "limit": max(int(req.limit or 0), 0),
        "profile_key": _recommendation_profile_key(req),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _semantic_search_lexical_score(query: str, *texts) -> float:
    normalized_query = _normalize_text(query)
    tokens = _query_tokens(query)
    normalized_texts = [
        _normalize_text(text)
        for text in texts
        if _normalize_text(text)
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


def _semantic_search_vectors(req: SearchRequest, profile):
    query_text = _recommendation_trim_text(req.query)
    query_vector = []
    if query_text:
        query_key = _recommendation_text_embedding_key("semantic_search_query", query_text)
        if query_key:
            text_embeddings = _recommendation_embed_entries(
                "text",
                [(query_key, query_text)],
            )
            query_vector = text_embeddings.get(query_key) or []

    vectors = profile.get("vectors") or {}
    semantic_query_vector = _vector_weighted_average(
        [
            (query_vector, 2.2),
            (vectors.get("query_vector") or [], 0.95),
            (vectors.get("artist_vector") or [], 0.35),
        ]
    )
    semantic_context_vector = _vector_weighted_average(
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


def _semantic_search_vector_similarities(candidate_vector, search_vectors, profile):
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
        "query": _assistant_cosine_similarity(
            candidate_vector,
            search_vectors.get("current_query_vector") or [],
        ),
        "semantic_query": _assistant_cosine_similarity(
            candidate_vector,
            search_vectors.get("semantic_query_vector") or [],
        ),
        "context": _assistant_cosine_similarity(
            candidate_vector,
            search_vectors.get("semantic_context_vector") or [],
        ),
        "taste": _assistant_cosine_similarity(
            candidate_vector,
            vectors.get("taste_vector") or [],
        ),
        "artist": _assistant_cosine_similarity(
            candidate_vector,
            vectors.get("artist_vector") or [],
        ),
        "short": _assistant_cosine_similarity(
            candidate_vector,
            vectors.get("short_term_vector") or [],
        ),
        "long": _assistant_cosine_similarity(
            candidate_vector,
            vectors.get("long_term_vector") or [],
        ),
    }


def _semantic_suggestion_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", _recommendation_trim_text(value))


def _semantic_track_suggestion_text(track: Optional[Dict[str, Any]]) -> str:
    if not isinstance(track, dict):
        return ""
    title = _semantic_suggestion_text(track.get("title"))
    artist = _semantic_suggestion_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    if not title:
        return ""
    if artist and _normalize_text(artist) not in _normalize_text(title):
        return f"{title} - {artist}"
    return title


def _semantic_album_suggestion_text(album: Optional[Dict[str, Any]]) -> str:
    if not isinstance(album, dict):
        return ""
    title = _semantic_suggestion_text(album.get("title"))
    artist = _semantic_suggestion_text(album.get("artist"))
    if not title:
        return ""
    if artist and _normalize_text(artist) not in _normalize_text(title):
        return f"{title} - {artist}"
    return title


def _semantic_search_suggestions(req: SearchRequest):
    query = _recommendation_trim_text(req.query)
    limit = max(1, min(req.limit or 5, 8))
    if not query:
        return []

    cache_key = _semantic_search_cache_key(req, "suggestions")
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "suggestions",
        cache_key,
    )
    if cached is not None:
        return list(cached[:limit])

    profile = _recommendation_build_profile(req)
    search_vectors = _semantic_search_vectors(req, profile)
    normalized_query = _normalize_text(query)
    user_scope_id = _assistant_safe_scope_id(profile.get("user_scope_id") or "guest")
    collaborative_model = (profile.get("collaborative") or {}).get("model") or {}
    candidates = {}

    def add_candidate(
        raw_text: Optional[str],
        source_score: float,
        source_name: str,
        suggestion_type: str,
    ) -> None:
        text = _semantic_suggestion_text(raw_text)
        normalized = _normalize_text(text)
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

    try:
        upstream_suggestions = ytmusic.get_search_suggestions(query)
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

    for index, artist in enumerate(_assistant_tool_search_artists_direct(query, 4)):
        add_candidate(
            artist.get("name"),
            max(3.7 - (index * 0.16), 1.0),
            "direct_artist_search",
            "artist",
        )

    for index, track in enumerate(_ytmusic_song_search(query, 6)):
        add_candidate(
            _semantic_track_suggestion_text(track),
            max(3.2 - (index * 0.15), 0.9),
            "direct_song_search",
            "track",
        )

    try:
        direct_album_results = ytmusic.search(query, filter="albums", limit=4)
    except Exception:
        direct_album_results = []
    for index, album in enumerate(normalize_album_results(direct_album_results)):
        add_candidate(
            _semantic_album_suggestion_text(album),
            max(2.8 - (index * 0.14), 0.8),
            "direct_album_search",
            "album",
        )

    for index, recent_query in enumerate(profile.get("recent_queries") or []):
        recent_query_text = _semantic_suggestion_text(recent_query)
        if not recent_query_text:
            continue
        if normalized_query and normalized_query not in _normalize_text(recent_query_text):
            continue
        add_candidate(
            recent_query_text,
            max(2.2 - (index * 0.12), 0.6),
            "recent_query_history",
            "query",
        )

    user_query_profile = (
        (collaborative_model.get("user_query_profiles") or {}).get(user_scope_id) or []
    )
    for index, entry in enumerate(user_query_profile[:6]):
        query_text = _semantic_suggestion_text(entry.get("query"))
        if not query_text:
            continue
        add_candidate(
            query_text,
            max(float(entry.get("weight") or 0.0) * 0.28, 0.45) + max(1.0 - (index * 0.1), 0.28),
            "collaborative_query_profile",
            "query",
        )

    if not candidates:
        return []

    suggestion_entries = list(candidates.values())
    suggestion_embeddings = _recommendation_embed_entries(
        "text",
        [
            (_recommendation_text_embedding_key("semantic_suggestion", item["text"]), item["text"])
            for item in suggestion_entries
            if item.get("text")
        ],
    )

    ranked = []
    for item in suggestion_entries:
        suggestion_text = item["text"]
        suggestion_key = _recommendation_text_embedding_key(
            "semantic_suggestion",
            suggestion_text,
        )
        suggestion_vector = suggestion_embeddings.get(suggestion_key) or []
        similarities = _semantic_search_vector_similarities(
            suggestion_vector,
            search_vectors,
            profile,
        )
        lexical_score = _semantic_search_lexical_score(query, suggestion_text)
        normalized_text = _normalize_text(suggestion_text)
        ranking_score = (
            (float(item.get("source_score") or 0.0) * 0.44)
            + lexical_score
            + (similarities["query"] * 8.1)
            + (similarities["semantic_query"] * 4.7)
            + (similarities["context"] * 1.35)
            + (similarities["taste"] * 0.55)
            + (similarities["artist"] * 0.5)
        )
        if normalized_query and normalized_text.startswith(normalized_query):
            ranking_score += 1.25
        elif normalized_query and normalized_query in normalized_text:
            ranking_score += 0.5
        if item.get("suggestion_type") == "artist":
            ranking_score += similarities["artist"] * 0.65
        elif item.get("suggestion_type") == "track":
            ranking_score += similarities["short"] * 0.35
        elif item.get("suggestion_type") == "query":
            ranking_score += similarities["context"] * 0.3

        ranked.append(
            {
                **item,
                "score": round(ranking_score, 3),
                "ml_similarities": {
                    "query": round(similarities["query"], 4),
                    "semantic_query": round(similarities["semantic_query"], 4),
                    "context": round(similarities["context"], 4),
                    "taste": round(similarities["taste"], 4),
                    "artist": round(similarities["artist"], 4),
                    "lexical": round(lexical_score, 4),
                },
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

    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "suggestions",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return list(results)


def _semantic_search_track_results(req: SearchRequest):
    query = _recommendation_trim_text(req.query)
    limit = max(1, min(req.limit or 24, 30))
    if not query:
        return []

    cache_key = _semantic_search_cache_key(req, "tracks")
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "tracks",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached[:limit]]

    profile = _recommendation_build_profile(req)
    search_vectors = _semantic_search_vectors(req, profile)
    candidate_limit = min(max(limit * 4, 24), 72)
    combined = {}

    def add_track_candidate(raw_track, source_score: float, source_name: str) -> None:
        normalized = normalize_recommendation_track(raw_track)
        if normalized is None:
            return
        if _track_metadata_incomplete(normalized):
            normalized = _recommendation_enrich_track_metadata(normalized)
        track_id = _recommendation_trim_text(normalized.get("id"))
        if not track_id:
            return
        current = combined.get(track_id)
        if current is None:
            combined[track_id] = {
                "track": normalized,
                "source_score": float(source_score),
                "source_name": source_name,
            }
            return
        if float(source_score) > float(current.get("source_score") or 0.0):
            current["source_score"] = float(source_score)
            current["source_name"] = source_name
        if _track_metadata_incomplete(current.get("track")):
            current["track"] = _merge_track_metadata(current["track"], normalized)

    direct_pool = _ytmusic_song_search(query, max(limit * 2, 18))
    if not direct_pool:
        direct_pool = _ytdlp_song_search(query, max(limit * 2, 18))
    for index, track in enumerate(direct_pool[:candidate_limit]):
        add_track_candidate(
            track,
            max(4.8 - (index * 0.14), 1.6),
            "direct_song_search",
        )

    blended_pool = _search_tracks_blended(query, min(candidate_limit, 42))
    for index, track in enumerate(blended_pool):
        add_track_candidate(
            track,
            max(3.6 - (index * 0.06), 1.1),
            "semantic_candidate_pool",
        )

    collaborative_track_ids = (
        (profile.get("collaborative") or {}).get("candidate_track_ids") or []
    )
    if collaborative_track_ids:
        collaborative_tracks = _recommendation_fetch_tracks_for_ids(
            collaborative_track_ids,
            limit=min(max(limit, 10), 16),
        )
        for index, track in enumerate(collaborative_tracks):
            add_track_candidate(
                track,
                max(2.2 - (index * 0.08), 0.55),
                "collaborative_query_match",
            )

    if not combined:
        return []

    candidate_embeddings = _recommendation_track_embeddings(
        [entry.get("track") for entry in combined.values()]
    )
    ranked = []
    for entry in combined.values():
        track = dict(entry.get("track") or {})
        track_id = _recommendation_trim_text(track.get("id"))
        track_key = _recommendation_track_embedding_key(track)
        track_vector = candidate_embeddings.get(track_key) or []
        similarities = _semantic_search_vector_similarities(
            track_vector,
            search_vectors,
            profile,
        )
        collaborative_scores = _recommendation_collaborative_track_scores(track, profile)
        lexical_score = _semantic_search_lexical_score(
            query,
            track.get("title"),
            track.get("channel"),
            track.get("album"),
        )
        title_lexical_score = _semantic_search_lexical_score(
            query,
            track.get("title"),
        )
        ranking_score = (
            (float(entry.get("source_score") or 0.0) * 0.46)
            + (title_lexical_score * 1.1)
            + lexical_score
            + (similarities["query"] * 8.6)
            + (similarities["semantic_query"] * 5.4)
            + (similarities["context"] * 2.0)
            + (similarities["taste"] * 1.2)
            + (similarities["artist"] * 1.4)
            + (similarities["short"] * 0.7)
            + (similarities["long"] * 0.45)
            + (collaborative_scores["latent"] * 4.6)
            + (min(collaborative_scores["neighbor"], 5.0) * 0.72)
            + (min(collaborative_scores["artist"], 6.0) * 0.14)
        )
        if (
            lexical_score < 0.75
            and similarities["query"] < 0.12
            and entry.get("source_name") == "collaborative_query_match"
        ):
            ranking_score -= 1.4
        if track_id in (profile.get("recent_track_ids") or []) and title_lexical_score < 1.8:
            ranking_score -= 0.25

        track["score"] = round(ranking_score, 3)
        track["search_source"] = entry.get("source_name") or ""
        track["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "short": round(similarities["short"], 4),
            "long": round(similarities["long"], 4),
            "lexical": round(lexical_score + title_lexical_score, 4),
            "collab_latent": round(collaborative_scores["latent"], 4),
            "collab_neighbor": round(collaborative_scores["neighbor"], 4),
            "collab_artist": round(collaborative_scores["artist"], 4),
        }
        ranked.append(track)

    ranked.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )

    results = []
    artist_counts = {}
    for track in ranked:
        artist_key = _normalize_text(track.get("channel") or track.get("artist") or "")
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(track)
        if len(results) >= limit:
            break

    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "tracks",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in results]


def _semantic_search_album_results(req: SearchRequest):
    query = _recommendation_trim_text(req.query)
    limit = max(1, min(req.limit or 12, 18))
    if not query:
        return []

    cache_key = _semantic_search_cache_key(req, "albums")
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "albums",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached[:limit]]

    profile = _recommendation_build_profile(req)
    search_vectors = _semantic_search_vectors(req, profile)
    candidates = _search_albums_blended(query, min(max(limit * 3, 18), 24))
    if not candidates:
        return []

    collaborative_artist_scores = (
        (profile.get("collaborative") or {}).get("artist_scores") or {}
    )
    album_embeddings = _recommendation_album_embeddings(candidates)
    ranked = []
    for index, album in enumerate(candidates):
        album_copy = dict(album)
        album_key = _recommendation_album_embedding_key(album_copy)
        album_vector = album_embeddings.get(album_key) or []
        similarities = _semantic_search_vector_similarities(
            album_vector,
            search_vectors,
            profile,
        )
        lexical_score = _semantic_search_lexical_score(
            query,
            album_copy.get("title"),
            album_copy.get("artist"),
        )
        collaborative_artist_score = float(
            collaborative_artist_scores.get(
                _normalize_text(album_copy.get("artist") or "")
            ) or 0.0
        )
        ranking_score = (
            (max(2.9 - (index * 0.08), 0.65) * 0.42)
            + lexical_score
            + (similarities["query"] * 8.0)
            + (similarities["semantic_query"] * 4.4)
            + (similarities["context"] * 1.85)
            + (similarities["taste"] * 1.35)
            + (similarities["artist"] * 1.55)
            + (min(collaborative_artist_score, 6.0) * 0.18)
        )
        album_copy["score"] = round(ranking_score, 3)
        album_copy["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "lexical": round(lexical_score, 4),
            "collab_artist": round(collaborative_artist_score, 4),
        }
        ranked.append(album_copy)

    ranked.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("title") or "",
        ),
        reverse=True,
    )

    results = []
    artist_counts = {}
    for album in ranked:
        artist_key = _normalize_text(album.get("artist") or "")
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(album)
        if len(results) >= limit:
            break

    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "albums",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in results]


def _semantic_search_artist_results(req: SearchRequest):
    query = _recommendation_trim_text(req.query)
    limit = max(1, min(req.limit or 12, 18))
    if not query:
        return []

    cache_key = _semantic_search_cache_key(req, "artists")
    cached = _cache_lookup(
        search_result_cache,
        search_result_cache_lock,
        "artists",
        cache_key,
    )
    if cached is not None:
        return [dict(item) for item in cached[:limit]]

    profile = _recommendation_build_profile(req)
    search_vectors = _semantic_search_vectors(req, profile)
    combined = {}

    def add_artist_candidate(raw_artist, source_score: float, source_name: str) -> None:
        if not isinstance(raw_artist, dict):
            return
        artist_id = _recommendation_trim_text(raw_artist.get("id"))
        artist_name = _recommendation_trim_text(raw_artist.get("name"))
        normalized_name = _normalize_text(artist_name)
        if not artist_id or not artist_name or not normalized_name:
            return
        current = combined.get(artist_id)
        if current is None:
            combined[artist_id] = {
                "artist": dict(raw_artist),
                "source_score": float(source_score),
                "source_name": source_name,
            }
            return
        if float(source_score) > float(current.get("source_score") or 0.0):
            current["source_score"] = float(source_score)
            current["source_name"] = source_name
        if not current["artist"].get("thumbnail") and raw_artist.get("thumbnail"):
            current["artist"]["thumbnail"] = raw_artist.get("thumbnail")

    direct_artists = _assistant_tool_search_artists_direct(query, max(limit * 2, 12))
    for index, artist in enumerate(direct_artists):
        add_artist_candidate(
            artist,
            max(4.9 - (index * 0.15), 1.6),
            "direct_artist_search",
        )

    semantic_artists = _assistant_tool_search_artists(query, min(max(limit * 3, 18), 24))
    for index, artist in enumerate(semantic_artists):
        add_artist_candidate(
            artist,
            max(3.9 - (index * 0.08), 1.0),
            "semantic_artist_pool",
        )

    collaborative_artist_scores = (
        (profile.get("collaborative") or {}).get("artist_scores") or {}
    )
    collaborative_seed_names = sorted(
        collaborative_artist_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:6]
    for artist_key, seed_score in collaborative_seed_names:
        for index, artist in enumerate(_assistant_tool_search_artists_direct(artist_key, 1)):
            add_artist_candidate(
                artist,
                max(float(seed_score) * 0.12, 0.35) + max(1.0 - (index * 0.14), 0.3),
                "collaborative_query_match",
            )

    if not combined:
        return []

    artist_embeddings = _recommendation_artist_embeddings(
        [entry.get("artist") for entry in combined.values()]
    )
    ranked = []
    for entry in combined.values():
        artist = dict(entry.get("artist") or {})
        artist_key = _recommendation_artist_embedding_key(artist)
        artist_vector = artist_embeddings.get(artist_key) or []
        similarities = _semantic_search_vector_similarities(
            artist_vector,
            search_vectors,
            profile,
        )
        lexical_score = _semantic_search_lexical_score(
            query,
            artist.get("name"),
            artist.get("description"),
        )
        collaborative_artist_score = float(
            collaborative_artist_scores.get(
                _normalize_text(artist.get("name") or "")
            ) or 0.0
        )
        penalty = _artist_related_name_penalty(query, artist.get("name") or "") * 0.25
        ranking_score = (
            (float(entry.get("source_score") or 0.0) * 0.5)
            + lexical_score
            + (similarities["query"] * 8.4)
            + (similarities["semantic_query"] * 4.9)
            + (similarities["context"] * 1.65)
            + (similarities["taste"] * 1.15)
            + (similarities["artist"] * 1.85)
            + (min(collaborative_artist_score, 6.0) * 0.4)
            - penalty
        )
        if (
            lexical_score < 0.65
            and similarities["query"] < 0.1
            and entry.get("source_name") == "collaborative_query_match"
        ):
            ranking_score -= 1.1
        artist["score"] = round(ranking_score, 3)
        artist["search_source"] = entry.get("source_name") or ""
        artist["ml_similarities"] = {
            "query": round(similarities["query"], 4),
            "semantic_query": round(similarities["semantic_query"], 4),
            "context": round(similarities["context"], 4),
            "taste": round(similarities["taste"], 4),
            "artist": round(similarities["artist"], 4),
            "lexical": round(lexical_score, 4),
            "collab_artist": round(collaborative_artist_score, 4),
        }
        ranked.append(artist)

    ranked.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("name") or "",
        ),
        reverse=True,
    )
    results = ranked[:limit]
    _cache_store(
        search_result_cache,
        search_result_cache_lock,
        "artists",
        cache_key,
        results,
        SEARCH_RESULT_CACHE_TTL_SECONDS,
    )
    return [dict(item) for item in results]


def _assistant_tool_get_user_taste_profile(req: AssistantChatRequest):
    artist_counts = {}
    for entry in list(req.last_assistant_tracks) + list(req.recent_assistant_tracks):
        artist = (entry.artist or "").strip()
        if not artist:
            continue
        artist_counts[artist] = artist_counts.get(artist, 0) + 1

    top_artists = [
        artist
        for artist, _ in sorted(
            artist_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
    ]
    return {
        "recent_queries": list(req.recent_queries[:5]),
        "recent_track_ids": list(req.recent_track_ids[:8]),
        "playlist_names": [playlist.name for playlist in req.playlist_summaries[:6]],
        "library_track_count": len(req.library_tracks),
        "top_recent_artists": top_artists,
    }

def _assistant_tool_use_context_tracks(
    req: AssistantChatRequest,
    source: str,
    count: int,
    artist_filter: Optional[str] = None,
):
    groups = _assistant_all_context_tracks(req)
    selected = groups.get(source) or []
    if not selected and source != "recent_assistant_tracks":
        selected = groups.get("recent_assistant_tracks") or []
    if artist_filter:
        normalized_artist = _normalize_text(artist_filter)
        filtered = [
            track
            for track in selected
            if normalized_artist in _normalize_text(track.get("channel") or track.get("artist"))
        ]
        if filtered:
            selected = filtered
    count = max(1, min(count, 12))
    return selected[:count]

def _assistant_tool_list_playlists(req: AssistantChatRequest):
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in req.playlist_summaries[:20]
    ]

def _assistant_attach_reasons_runtime(tracks, reasons):
    reason_map = {}
    for row in reasons or []:
        if not isinstance(row, dict):
            continue
        track_id = row.get("id")
        reason = (row.get("reason") or "").strip()
        if track_id and reason:
            reason_map[track_id] = reason
    enriched = []
    for track in tracks:
        copy = dict(track)
        track_id = copy.get("id")
        if track_id in reason_map:
            copy["reason"] = reason_map[track_id]
        enriched.append(copy)
    return enriched

def _assistant_store_turn_memory(
    req: AssistantChatRequest,
    response_payload: Dict[str, Any],
    selected_tracks: Optional[List[Dict[str, Any]]] = None,
    target_playlist: Optional[Dict[str, Any]] = None,
):
    scope_id = _assistant_safe_scope_id(req.user_scope_id)
    _assistant_store_memory(
        scope_id,
        "user_message",
        req.message,
        {
            "conversation_length": len(req.conversation),
        },
    )
    reply = (response_payload.get("reply") or "").strip()
    if reply:
        _assistant_store_memory(
            scope_id,
            "assistant_reply",
            reply,
            {
                "action_type": response_payload.get("action_type"),
                "selected_track_ids": response_payload.get("selected_track_ids") or [],
            },
        )
    if selected_tracks:
        summary = "; ".join(
            f"{track.get('title') or 'Unknown Track'} - {track.get('channel') or track.get('artist') or 'Unknown Artist'}"
            for track in selected_tracks[:8]
        )
        _assistant_store_memory(
            scope_id,
            "assistant_tracks",
            summary,
            {
                "track_ids": [
                    track.get("id")
                    for track in selected_tracks
                    if track.get("id")
                ],
                "action_type": response_payload.get("action_type"),
            },
        )
    playlist_name = (response_payload.get("playlist_name") or "").strip()
    if playlist_name:
        _assistant_store_memory(
            scope_id,
            "playlist_intent",
            playlist_name,
            {
                "summary": response_payload.get("playlist_summary"),
                "target_playlist": target_playlist or {},
            },
        )

def _assistant_initial_memory_queries(req: AssistantChatRequest):
    queries = [req.message]
    for entry in req.conversation[-4:]:
        if entry.content and entry.content.strip():
            queries.append(entry.content.strip())
    for query in req.recent_queries[:3]:
        if query and query.strip():
            queries.append(query.strip())
    deduped = []
    seen = set()
    for query in queries:
        normalized = _normalize_text(query)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(query)
    return deduped[:6]

def _assistant_merge_memory_hits(*groups):
    merged = []
    seen = set()
    for group in groups:
        for hit in group or []:
            hit_id = hit.get("id")
            if not hit_id or hit_id in seen:
                continue
            seen.add(hit_id)
            merged.append(hit)
    merged.sort(key=lambda item: (item.get("score", 0.0), item.get("created_at", 0.0)), reverse=True)
    return merged[:8]

def _assistant_playlist_options(req: AssistantChatRequest, preferred_names: Optional[List[str]] = None):
    available = list(req.playlist_summaries[:20])
    if not preferred_names:
        return [
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
            for playlist in available[:12]
        ]

    resolved = []
    seen_ids = set()
    for name in preferred_names:
        playlist = _playlist_lookup_by_name(available, name)
        if playlist is None or playlist.id in seen_ids:
            continue
        seen_ids.add(playlist.id)
        resolved.append(
            {
                "id": playlist.id,
                "name": playlist.name,
                "track_count": playlist.track_count,
            }
        )
    return resolved or [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
        }
        for playlist in available[:12]
    ]

def _assistant_model_for_request(req: AssistantChatRequest):
    return OLLAMA_THINKING_MODEL if req.thinking_mode else OLLAMA_FAST_MODEL

def _assistant_langgraph_deps(req: AssistantChatRequest):
    selected_model = _assistant_model_for_request(req)
    return {
        "initial_memory_queries": _assistant_initial_memory_queries,
        "query_memory": _assistant_query_memory,
        "merge_memory_hits": _assistant_merge_memory_hits,
        "call_planner_structured": lambda messages, **kwargs: _call_ollama_structured(
            messages,
            model_override=OLLAMA_PLANNER_MODEL,
            **kwargs,
        ),
        "call_response_structured": lambda messages, **kwargs: _call_ollama_structured(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "call_structured": lambda messages, **kwargs: _call_ollama_structured(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "call_chat": lambda messages, **kwargs: _call_ollama_chat(
            messages,
            model_override=selected_model,
            **kwargs,
        ),
        "tool_search_tracks": _assistant_tool_search_tracks,
        "tool_search_albums": _assistant_tool_search_albums,
        "tool_search_artists": _assistant_tool_search_artists,
        "tool_get_track_details": _assistant_tool_get_track_details,
        "tool_get_album_details": _assistant_tool_get_album_details,
        "tool_get_similar_tracks": _assistant_tool_get_similar_tracks,
        "tool_get_user_taste_profile": _assistant_tool_get_user_taste_profile,
        "tool_use_context_tracks": _assistant_tool_use_context_tracks,
        "tool_list_playlists": _assistant_tool_list_playlists,
        "all_context_tracks": _assistant_all_context_tracks,
        "attach_reasons": _assistant_attach_reasons_runtime,
        "playlist_lookup_by_name": _playlist_lookup_by_name,
        "playlist_options": _assistant_playlist_options,
        "store_turn_memory": _assistant_store_turn_memory,
        "fallback_chat_reply": lambda request: _assistant_fallback_chat_reply(
            request,
            model_override=selected_model,
        ),
        "model_name": selected_model,
        "planner_model_name": OLLAMA_PLANNER_MODEL,
    }

def _assistant_fallback_chat_reply(req: AssistantChatRequest, model_override=None):
    messages = [
        {
            "role": "system",
            "content": (
                "You are EBB, a warm conversational assistant. "
                "Reply naturally to the user's latest message. "
                "If they want comfort or conversation, be present and human. "
                "Do not force music suggestions unless they explicitly ask for music. "
                "Respond in 2 to 5 sentences."
            ),
        }
    ]
    for entry in req.conversation[-8:]:
        messages.append(
            {
                "role": "assistant" if entry.role == "assistant" else "user",
                "content": entry.content,
            }
        )
    messages.append({"role": "user", "content": req.message})
    reply = _call_ollama_chat(
        messages,
        temperature=0.6,
        model_override=model_override,
    )
    reply = (reply or "").strip()
    if reply:
        return reply
    return "I'm here with you. Tell me a little more and I'll stay with you."

_assistant_init_memory_db()
_assistant_init_session_db()

def _extract_stream_info(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    headers = {}
    for key, value in (info.get("http_headers") or {}).items():
        if key and value:
            headers[str(key)] = str(value)

    return {
        "url": info["url"],
        "headers": headers,
        "mime_type": info.get("ext") or info.get("acodec") or "audio/mp4",
        "duration": info.get("duration") or 0,
    }

def _is_stream_info_cached(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        return bool(cached and cached["expires_at"] > now)

def _extract_total_length(headers):
    content_range = headers.get("content-range") or headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.split("/")[-1].strip()
        if total.isdigit():
            return int(total)

    content_length = headers.get("content-length") or headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None

def _get_cached_stream_chunk(video_id: str, min_bytes: int = 0):
    now = time.time()
    with stream_chunk_lock:
        cached = stream_chunk_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            payload = cached["payload"]
            if len(payload.get("bytes") or b"") >= min_bytes:
                return payload
            return None
        if cached:
            stream_chunk_cache.pop(video_id, None)
    return None

def _store_stream_chunk(video_id: str, payload):
    with stream_chunk_lock:
        stream_chunk_cache[video_id] = {
            "payload": payload,
            "expires_at": time.time() + STREAM_CHUNK_TTL_SECONDS,
        }

def _chunk_target_bytes(position: int, active_queue: bool):
    if active_queue:
        if position <= 0:
            return 2097152
        if position == 1:
            return 1572864
        if position == 2:
            return 1048576
        return STREAM_WARM_CHUNK_BYTES
    if position <= 0:
        return 1048576
    if position == 1:
        return 786432
    return STREAM_WARM_CHUNK_BYTES

def _parse_byte_range(range_header: Optional[str], total_length: Optional[int]):
    if not range_header:
        return None

    value = range_header.strip().lower()
    if not value.startswith("bytes="):
        return None

    spec = value.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None

    start_text, end_text = spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()

    if not start_text:
        if total_length is None or not end_text.isdigit():
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        start = max(total_length - suffix_length, 0)
        end = total_length - 1
        return start, end

    if not start_text.isdigit():
        return None

    start = int(start_text)
    end = None
    if end_text:
        if not end_text.isdigit():
            return None
        end = int(end_text)

    if total_length is not None:
        if start >= total_length:
            return None
        if end is None or end >= total_length:
            end = total_length - 1

    if end is not None and end < start:
        return None

    return start, end

def _record_prepare_metric(video_id: str, metrics: dict):
    with prepare_metrics_lock:
        prepare_metrics.append({
            "video_id": video_id,
            "resolve_ms": metrics.get("resolve_ms") or 0,
            "chunk_ms": metrics.get("chunk_ms") or 0,
            "server_ms": metrics.get("server_ms") or 0,
            "cached_prefix_bytes": metrics.get("cached_prefix_bytes") or 0,
            "resolve_cache_hit": bool(metrics.get("resolve_cache_hit")),
            "chunk_cache_hit": bool(metrics.get("chunk_cache_hit")),
            "active_queue": bool(metrics.get("active_queue")),
            "created_at": time.time(),
        })

def _summarize_prepare_metrics():
    with prepare_metrics_lock:
        items = list(prepare_metrics)

    if not items:
        return {
            "sample_count": 0,
            "avg_resolve_ms": 0,
            "avg_chunk_ms": 0,
            "avg_server_ms": 0,
            "avg_cached_prefix_bytes": 0,
            "resolve_cache_hit_rate": 0.0,
            "chunk_cache_hit_rate": 0.0,
        }

    count = len(items)
    return {
        "sample_count": count,
        "avg_resolve_ms": int(sum(item["resolve_ms"] for item in items) / count),
        "avg_chunk_ms": int(sum(item["chunk_ms"] for item in items) / count),
        "avg_server_ms": int(sum(item["server_ms"] for item in items) / count),
        "avg_cached_prefix_bytes": int(
            sum(item["cached_prefix_bytes"] for item in items) / count
        ),
        "resolve_cache_hit_rate": round(
            sum(1 for item in items if item["resolve_cache_hit"]) / count, 3
        ),
        "chunk_cache_hit_rate": round(
            sum(1 for item in items if item["chunk_cache_hit"]) / count, 3
        ),
    }

def _warm_initial_stream_chunk(video_id: str, stream_info: dict, target_bytes: int):
    target_bytes = max(target_bytes, STREAM_WARM_CHUNK_BYTES)
    while True:
        cached = _get_cached_stream_chunk(video_id, min_bytes=target_bytes)
        if cached is not None:
            return cached

        with stream_chunk_inflight_lock:
            pending = stream_chunk_inflight.get(video_id)
            if pending is None:
                pending = Future()
                stream_chunk_inflight[video_id] = pending
                should_fetch = True
            else:
                should_fetch = False

        if not should_fetch:
            try:
                pending.result(timeout=25)
            except Exception:
                pass
            continue

        try:
            existing = _get_cached_stream_chunk(video_id)
            existing_bytes = existing.get("bytes") if existing else b""
            total_length = existing.get("total_length") if existing else None
            if total_length is not None and len(existing_bytes) >= total_length:
                pending.set_result(existing)
                return existing

            headers = dict(stream_info["headers"])
            start_offset = len(existing_bytes)
            headers["range"] = f"bytes={start_offset}-{max(target_bytes - 1, start_offset)}"
            req = upstream_http.get(
                stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(5, 20),
            )
            try:
                req.raise_for_status()
                chunks = [existing_bytes] if existing_bytes else []
                total_bytes = len(existing_bytes)
                for chunk in req.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    remaining = target_bytes - total_bytes
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= target_bytes:
                        break

                payload = {
                    "bytes": b"".join(chunks),
                    "content_type": req.headers.get(
                        "content-type",
                        stream_info.get("mime_type") or "audio/mp4",
                    ),
                    "total_length": _extract_total_length(req.headers) or total_length,
                }
                _store_stream_chunk(video_id, payload)
                pending.set_result(payload)
                return payload
            finally:
                req.close()
        except Exception as exc:
            pending.set_exception(exc)
            raise
        finally:
            with stream_chunk_inflight_lock:
                if stream_chunk_inflight.get(video_id) is pending:
                    stream_chunk_inflight.pop(video_id, None)

def _prepare_stream_track(video_id: str, target_chunk_bytes: int, active_queue: bool):
    total_start = time.perf_counter()
    resolve_start = time.perf_counter()
    resolve_cache_hit = _is_stream_info_cached(video_id)
    stream_info = get_stream_info(video_id)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    chunk_cache_hit = _get_cached_stream_chunk(video_id, min_bytes=target_chunk_bytes) is not None
    chunk_start = time.perf_counter()
    chunk_payload = _warm_initial_stream_chunk(video_id, stream_info, target_chunk_bytes)
    chunk_ms = int((time.perf_counter() - chunk_start) * 1000)

    metrics = {
        "prepared": True,
        "playback_path": f"/proxy_stream/{video_id}",
        "resolve_cache_hit": resolve_cache_hit,
        "chunk_cache_hit": chunk_cache_hit,
        "resolve_ms": resolve_ms,
        "chunk_ms": chunk_ms,
        "target_chunk_bytes": target_chunk_bytes,
        "cached_prefix_bytes": len(chunk_payload.get("bytes") or b""),
        "duration": stream_info.get("duration") or 0,
        "active_queue": active_queue,
        "server_ms": int((time.perf_counter() - total_start) * 1000),
    }
    _record_prepare_metric(video_id, metrics)
    return metrics

def _prepare_stream_track_safely(video_id: str, target_chunk_bytes: int, active_queue: bool):
    try:
        return _prepare_stream_track(video_id, target_chunk_bytes, active_queue)
    except Exception:
        return None


def _classify_stream_failure(exc: Exception):
    message = str(exc or "").strip()
    lowered = message.lower()
    if "video unavailable" in lowered or "this video is not available" in lowered:
        return {
            "code": "video_unavailable",
            "message": message or "Video unavailable",
            "status_code": 410,
        }
    if "requested format is not available" in lowered:
        return {
            "code": "format_unavailable",
            "message": message or "Requested format is not available",
            "status_code": 410,
        }
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return {
            "code": "source_blocked",
            "message": message or "Upstream source requires verification",
            "status_code": 502,
        }
    return {
        "code": "stream_failed",
        "message": message or exc.__class__.__name__,
        "status_code": 500,
    }


def _prepare_streams_with_failures(
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared = {}
    failed = {}
    deduped_ids = []
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        deduped_ids.append(video_id)
        if len(deduped_ids) >= limit:
            break

    if not deduped_ids:
        return prepared, failed

    if current_video_id and current_video_id in deduped_ids:
        deduped_ids.remove(current_video_id)
        deduped_ids.insert(0, current_video_id)

    targets = {
        video_id: _chunk_target_bytes(index, active_queue)
        for index, video_id in enumerate(deduped_ids)
    }

    max_workers = min(PREPARE_SESSION_WORKERS, len(deduped_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _prepare_stream_track,
                video_id,
                targets[video_id],
                active_queue,
            ): video_id
            for video_id in deduped_ids
        }
        for future, video_id in future_map.items():
            try:
                prepared[video_id] = future.result(timeout=25)
            except Exception as exc:
                failed[video_id] = _classify_stream_failure(exc)
    return prepared, failed

def _prepare_streams(
    video_ids: List[str],
    limit: int = 18,
    current_video_id: Optional[str] = None,
    active_queue: bool = False,
):
    prepared, _ = _prepare_streams_with_failures(
        video_ids,
        limit=limit,
        current_video_id=current_video_id,
        active_queue=active_queue,
    )
    return prepared

def get_stream_info(video_id: str):
    now = time.time()
    with stream_info_lock:
        cached = stream_info_cache.get(video_id)
        if cached and cached["expires_at"] > now:
            return cached["payload"]

        pending = stream_info_inflight.get(video_id)
        if pending is None:
            pending = Future()
            stream_info_inflight[video_id] = pending
            should_extract = True
        else:
            should_extract = False

    if not should_extract:
        return pending.result(timeout=25)

    try:
        payload = _extract_stream_info(video_id)
        with stream_info_lock:
            stream_info_cache[video_id] = {
                "payload": payload,
                "expires_at": now + STREAM_INFO_TTL_SECONDS,
            }
        pending.set_result(payload)
        return payload
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with stream_info_lock:
            if stream_info_inflight.get(video_id) is pending:
                stream_info_inflight.pop(video_id, None)

def refresh_stream_info(video_id: str):
    with stream_info_lock:
        stream_info_cache.pop(video_id, None)
    return get_stream_info(video_id)

def _should_refresh_stream_info(exc: Exception):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {401, 403, 404, 410, 416}

def _iter_upstream_stream(video_id: str, stream_info: dict, start: int = 0, end: Optional[int] = None):
    current_start = max(start, 0)
    current_stream_info = stream_info
    attempts = 0
    refreshed = False

    while True:
        req = None
        try:
            headers = dict(current_stream_info["headers"])
            if current_start > 0 or end is not None:
                if end is None:
                    headers["range"] = f"bytes={current_start}-"
                else:
                    headers["range"] = f"bytes={current_start}-{end}"

            req = upstream_http.get(
                current_stream_info["url"],
                headers=headers,
                stream=True,
                timeout=(8, 90),
            )
            req.raise_for_status()

            for chunk in req.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                yield chunk
                current_start += len(chunk)
                if end is not None and current_start > end:
                    return
            return
        except Exception as exc:
            if attempts >= 2:
                raise
            attempts += 1
            if _should_refresh_stream_info(exc) and not refreshed:
                refreshed = True
                try:
                    current_stream_info = refresh_stream_info(video_id)
                except Exception:
                    raise exc
            time.sleep(0.15)
        finally:
            if req is not None:
                req.close()

def _warm_stream_safely(video_id: str):
    _prepare_stream_track_safely(
        video_id,
        _chunk_target_bytes(4, False),
        False,
    )

def queue_stream_warmup(video_ids: List[str], limit: int = 18):
    seen = set()
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        stream_warm_executor.submit(_warm_stream_safely, video_id)
        if len(seen) >= limit:
            break

@app.post("/prepare_session")
def prepare_session(req: WarmStreamRequest):
    start_time = time.perf_counter()
    limit = min(max(req.lookahead or len(req.video_ids), 1), PREPARE_SESSION_MAX_LOOKAHEAD)
    prepared, failed = _prepare_streams_with_failures(
        req.video_ids,
        limit=limit,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "prepared": prepared,
        "prepared_ids": list(prepared.keys()),
        "failed": failed,
        "failed_ids": list(failed.keys()),
        "summary": _summarize_prepare_metrics(),
        "server_ms": int((time.perf_counter() - start_time) * 1000),
    }

@app.get("/latency_summary")
def latency_summary():
    return {
        "status": "success",
        "summary": _summarize_prepare_metrics(),
        "stream_info_cache_size": len(stream_info_cache),
        "stream_chunk_cache_size": len(stream_chunk_cache),
    }

@app.get("/")
def health_check():
    return {"status": "Auralis Python Proxy is running"}

@app.post("/track_details")
def get_track_details(req: DownloadRequest):
    try:
        return _build_track_details_payload(req.video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/lyrics/{video_id}")
def get_track_lyrics(video_id: str):
    try:
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        lyrics_browse_id = watch.get("lyrics")
        if not lyrics_browse_id:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

        lyrics_payload = ytmusic.get_lyrics(lyrics_browse_id, timestamps=True)
        if not lyrics_payload:
            return {
                "status": "success",
                "video_id": video_id,
                "has_lyrics": False,
                "has_timestamps": False,
                "source": None,
                "lines": [],
            }

        lines = []
        if lyrics_payload.get("hasTimestamps"):
            for index, line in enumerate(lyrics_payload.get("lyrics", [])):
                text = getattr(line, "text", "").strip()
                if not text:
                    continue
                lines.append({
                    "index": index,
                    "text": text,
                    "start_time_ms": getattr(line, "start_time", None),
                    "end_time_ms": getattr(line, "end_time", None),
                })
        else:
            raw_text = (lyrics_payload.get("lyrics") or "").splitlines()
            for index, line in enumerate(raw_text):
                text = line.strip()
                if not text:
                    continue
                lines.append({
                    "index": index,
                    "text": text,
                    "start_time_ms": None,
                    "end_time_ms": None,
                })

        return {
            "status": "success",
            "video_id": video_id,
            "has_lyrics": bool(lines),
            "has_timestamps": bool(lyrics_payload.get("hasTimestamps")),
            "source": lyrics_payload.get("source"),
            "lines": lines,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
def search_youtube(req: SearchRequest):
    query = (req.query or "").strip()
    limit = max(18, min(req.limit or 24, 30))
    results = []

    # Check if the query is a direct YouTube URL
    url_match = re.search(r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})", query)
    if url_match:
        video_id = url_match.group(1)
        try:
            watch = ytmusic.get_watch_playlist(videoId=video_id)
            vd = watch.get("videoDetails", {})
            results.append({
                "id": video_id,
                "title": vd.get("title") or "Unknown URL Track",
                "duration": vd.get("lengthSeconds") or 0,
                "thumbnail": extract_thumbnail(vd),
                "channel": extract_artist(vd)
            })
            return {"status": "success", "results": results}
        except Exception:
            pass # Fallback to normal search if extraction fails

    results = _semantic_search_track_results(req)
    return {
        "status": "success",
        "results": results[:limit],
        "diagnostics": {"ranking_backend": "semantic_search_profile"},
    }

@app.post("/search_albums")
def search_albums(req: SearchRequest):
    try:
        albums = _semantic_search_album_results(req)
        return {
            "status": "success",
            "albums": albums[: max(1, min(req.limit, 12))],
            "diagnostics": {"ranking_backend": "semantic_search_profile"},
        }
    except Exception as e:
        return {"status": "success", "albums": []}


@app.post("/search_artists")
def search_artists(req: SearchRequest):
    try:
        artists = _semantic_search_artist_results(req)
        return {
            "status": "success",
            "artists": artists[: max(1, min(req.limit, 12))],
            "diagnostics": {"ranking_backend": "semantic_search_profile"},
        }
    except Exception:
        return {"status": "success", "artists": []}


@app.post("/recommended_artists")
def recommended_artists(req: SearchRequest):
    try:
        return _recommended_artists_payload(req)
    except Exception:
        return {"status": "success", "artists": []}


@app.post("/interaction_event")
def recommendation_interaction_event(req: RecommendationInteractionEventRequest):
    try:
        stored = _recommendation_store_interaction_event(req)
        return {"status": "success", "stored": bool(stored)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/search_interaction")
def recommendation_search_interaction(req: RecommendationSearchEventRequest):
    try:
        stored = _recommendation_store_search_event(req)
        return {"status": "success", "stored": bool(stored)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/recommendation_model")
def recommendation_model_status():
    try:
        model = _recommendation_get_collaborative_model()
        sync_signature = _recommendation_model_source_signature()
        try:
            sync_payload = json.loads(sync_signature)
        except Exception:
            sync_payload = {}
        return {
            "status": "success",
            "model": {
                "ready": bool((model or {}).get("ready")),
                "model_id": (model or {}).get("model_id") or "",
                "model_type": (model or {}).get("model_type") or "",
                "event_count": int((model or {}).get("event_count") or 0),
                "search_event_count": int((model or {}).get("search_event_count") or 0),
                "user_count": int((model or {}).get("user_count") or 0),
                "item_count": int((model or {}).get("item_count") or 0),
                "factor_dim": int((model or {}).get("factor_dim") or 0),
                "trained_at": (model or {}).get("trained_at"),
                "source_signature": (model or {}).get("source_signature") or "",
                "evaluation_metrics": (model or {}).get("evaluation_metrics") or {},
                "sync_state": {
                    "dsn_configured": bool(RECOMMENDATION_SYNC_DATABASE_DSN),
                    "scheduler_enabled": RECOMMENDATION_ENABLE_SCHEDULER,
                    "event_count": int(sync_payload.get("event_count") or 0),
                    "search_event_count": int(sync_payload.get("search_event_count") or 0),
                    "user_count": int(sync_payload.get("user_count") or 0),
                    "item_count": int(sync_payload.get("item_count") or 0),
                },
                "runtime": _recommendation_runtime_snapshot(),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/recommendation_model/versions")
def recommendation_model_versions():
    try:
        return {
            "status": "success",
            "runtime": _recommendation_runtime_snapshot(version_limit=12),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/recommendation_experiments")
def recommendation_experiments(window_hours: int = RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS):
    try:
        return {
            "status": "success",
            "experiments": _recommendation_experiment_dashboard(window_hours=window_hours),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/recommendation_experiments/evaluate")
def recommendation_experiments_evaluate(
    force_promote: bool = False,
    window_hours: int = RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS,
):
    try:
        return {
            "status": "success",
            "result": _recommendation_evaluate_experiment(
                force_promote=force_promote,
                window_hours=window_hours,
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/recommendation_model/train")
def recommendation_model_train(req: RecommendationModelTrainRequest):
    try:
        if req.force_sync:
            _recommendation_sync_external_events(force=True)
        model = _recommendation_get_collaborative_model(force_refresh=True)
        return {
            "status": "success",
            "model": {
                "ready": bool((model or {}).get("ready")),
                "model_id": (model or {}).get("model_id") or "",
                "model_type": (model or {}).get("model_type") or "",
                "event_count": int((model or {}).get("event_count") or 0),
                "search_event_count": int((model or {}).get("search_event_count") or 0),
                "user_count": int((model or {}).get("user_count") or 0),
                "item_count": int((model or {}).get("item_count") or 0),
                "factor_dim": int((model or {}).get("factor_dim") or 0),
                "trained_at": (model or {}).get("trained_at"),
                "source_signature": (model or {}).get("source_signature") or "",
                "evaluation_metrics": (model or {}).get("evaluation_metrics") or {},
                "runtime": _recommendation_runtime_snapshot(),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/album/{album_id}")
def get_album_details(album_id: str):
    try:
        return _build_album_details_payload(album_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/artist/{artist_id}")
def get_artist_details(artist_id: str):
    try:
        return _build_artist_details_payload(artist_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/suggest")
def get_suggestions(req: SearchRequest):
    try:
        results = _semantic_search_suggestions(req)
        return {
            "status": "success",
            "results": results[: max(1, min(req.limit or 5, 8))],
            "diagnostics": {"ranking_backend": "semantic_search_profile"},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _fallback_home_candidates(limit: int):
    now = time.time()
    with home_candidates_lock:
        cached = home_candidates_cache.get("results") or []
        if home_candidates_cache.get("expires_at", 0) > now and len(cached) >= max(5, limit):
            return cached[: limit + 10]

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
        with home_candidates_lock:
            home_candidates_cache["results"] = results[: limit + 10]
            home_candidates_cache["expires_at"] = now + RECOMMENDATION_CACHE_TTL_SECONDS
    return results

def _recommendation_cache_key(req: SearchRequest):
    payload = {
        "query": (req.query or "").strip().lower(),
        "limit": int(req.limit),
        "offset": max(int(req.offset or 0), 0),
        "seed_id": (req.seed_id or "").strip(),
        "seed_ids": [item for item in (req.seed_ids or []) if item][:6],
        "artist_hints": [item.strip().lower() for item in (req.artist_hints or []) if item][:6],
        "taste_queries": [item.strip().lower() for item in (req.taste_queries or []) if item][:8],
        "avoid_ids": [item for item in (req.avoid_ids or []) if item][:40],
    }
    return json.dumps(payload, sort_keys=True)

def _get_cached_recommendations(cache_key: str):
    now = time.time()
    with recommendation_cache_lock:
        cached = recommendation_cache.get(cache_key)
        if cached and cached["expires_at"] > now:
            return cached["results"]
        if cached:
            recommendation_cache.pop(cache_key, None)
    return None

def _set_cached_recommendations(cache_key: str, results):
    with recommendation_cache_lock:
        recommendation_cache[cache_key] = {
            "results": results,
            "expires_at": time.time() + RECOMMENDATION_CACHE_TTL_SECONDS,
        }


def _recommendation_candidate_window(req: SearchRequest) -> int:
    limit = max(int(req.limit or 0), 1)
    offset = max(int(req.offset or 0), 0)
    avoid_count = len([item for item in (req.avoid_ids or []) if item])
    bias = max(offset, avoid_count)
    return min(max(limit + bias + 8, limit + 8), 72)

def _rank_recommendation_candidates(
    candidates,
    *,
    limit: int,
    avoid_ids,
    seed_ids,
    artist_hints,
    taste_queries,
):
    ranked = {}
    blocked_ids = {item for item in avoid_ids if item}.union(
        {item for item in seed_ids if item}
    )
    normalized_artist_hints = [
        _normalize_text(item) for item in artist_hints if _normalize_text(item)
    ]
    query_token_groups = [
        tokens[:4]
        for tokens in (_query_tokens(item) for item in taste_queries)
        if tokens
    ]

    for track, base_score in candidates:
        track_id = track.get("id")
        if not track_id or track_id in blocked_ids:
            continue
        title_text = _normalize_text(track.get("title"))
        artist_text = _normalize_text(track.get("channel"))
        album_text = _normalize_text(track.get("album"))
        score = float(base_score)

        for index, artist_hint in enumerate(normalized_artist_hints):
            if artist_hint and artist_hint in artist_text:
                score += max(3.2 - (index * 0.35), 0.8)

        for index, tokens in enumerate(query_token_groups):
            hits = sum(
                1
                for token in tokens
                if token in title_text or token in artist_text or token in album_text
            )
            if hits:
                score += min(hits, 2) * max(2.1 - (index * 0.18), 0.45)

        existing = ranked.get(track_id)
        if existing is None or score > existing["score"]:
            ranked[track_id] = {
                "track": track,
                "score": score,
                "artist_key": artist_text,
            }

    ordered = sorted(
        ranked.values(),
        key=lambda item: (item["score"], item["track"].get("title") or ""),
        reverse=True,
    )

    results = []
    artist_counts = {}
    for item in ordered:
        artist_key = item["artist_key"]
        if artist_key:
            artist_count = artist_counts.get(artist_key, 0)
            if artist_count >= 2 and len(results) + 1 < limit:
                continue
            artist_counts[artist_key] = artist_count + 1
        results.append(item["track"])
        if len(results) >= limit:
            break
    return results


def _recommendation_trim_text(value: Optional[str]) -> str:
    return (value or "").strip()


def _recommendation_unique_strings(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        value = _recommendation_trim_text(raw)
        normalized = _normalize_text(value)
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(value)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


def _recommendation_unique_track_ids(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        value = _recommendation_trim_text(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


def _recommendation_unique_snapshot_tracks(values, limit: Optional[int] = None):
    ordered = []
    seen = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_recommendation_track(raw)
        if normalized is None:
            continue
        track_id = _recommendation_trim_text(normalized.get("id"))
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        ordered.append(normalized)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


def _recommendation_track_from_details(payload: Dict[str, Any]):
    if not isinstance(payload, dict):
        return None
    video_id = (
        payload.get("video_id")
        or payload.get("id")
        or payload.get("videoId")
    )
    if not video_id:
        return None
    return {
        "id": str(video_id),
        "title": payload.get("title") or "Unknown Track",
        "duration": parse_duration_seconds(payload.get("duration")),
        "thumbnail": payload.get("thumbnail"),
        "channel": payload.get("author") or payload.get("artist") or "Unknown Artist",
        "album": payload.get("album") or payload.get("album_title"),
        "album_id": payload.get("album_id"),
    }


def _recommendation_cached_track(track_id: str):
    normalized_id = _recommendation_trim_text(track_id)
    if not normalized_id:
        return None
    now = time.time()
    with recommendation_track_details_lock:
        cached = recommendation_track_details_cache.get(normalized_id)
        if cached and cached["expires_at"] > now:
            return dict(cached["track"])
        if cached:
            recommendation_track_details_cache.pop(normalized_id, None)
    return None


def _recommendation_store_cached_track(track_id: str, track: Dict[str, Any]):
    normalized_id = _recommendation_trim_text(track_id)
    if not normalized_id:
        return
    with recommendation_track_details_lock:
        recommendation_track_details_cache[normalized_id] = {
            "track": dict(track),
            "expires_at": time.time() + RECOMMENDATION_TRACK_CACHE_TTL_SECONDS,
        }


def _recommendation_fetch_track_for_id(track_id: str):
    cached = _recommendation_cached_track(track_id)
    if cached is not None:
        return cached
    try:
        details = _assistant_tool_get_track_details(track_id)
    except Exception:
        details = {}
    track = _recommendation_track_from_details(details)
    if track is not None:
        _recommendation_store_cached_track(track_id, track)
    return track


def _recommendation_store_connection():
    connection = sqlite3.connect(RECOMMENDATION_STORE_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _recommendation_init_store_db():
    with recommendation_store_lock:
        connection = _recommendation_store_connection()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_events (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    artist_name TEXT,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    weight REAL NOT NULL,
                    metadata_json TEXT,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_events_user_time "
                "ON recommendation_events(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_events_track_time "
                "ON recommendation_events(track_id, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_models (
                    id TEXT PRIMARY KEY,
                    source_signature TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            existing_model_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recommendation_models)"
                ).fetchall()
            }
            if "metrics_json" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN metrics_json TEXT"
                )
            if "is_active" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0"
                )
            if "created_at" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN created_at REAL"
                )
            if "model_kind" not in existing_model_columns:
                connection.execute(
                    "ALTER TABLE recommendation_models ADD COLUMN model_kind TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_model_versions (
                    id TEXT PRIMARY KEY,
                    source_signature TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    metrics_json TEXT,
                    created_at REAL NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_search_events (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    result_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    metadata_json TEXT,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_search_events_user_time "
                "ON recommendation_search_events(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_search_events_query_time "
                "ON recommendation_search_events(query, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_feature_store (
                    namespace TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    model_id TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(namespace, entity_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_experiment_assignments (
                    user_scope_id TEXT NOT NULL,
                    experiment_key TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    assigned_at REAL NOT NULL,
                    PRIMARY KEY(user_scope_id, experiment_key)
                )
                """
            )
            existing_assignment_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recommendation_experiment_assignments)"
                ).fetchall()
            }
            if "assignment_source" not in existing_assignment_columns:
                connection.execute(
                    "ALTER TABLE recommendation_experiment_assignments "
                    "ADD COLUMN assignment_source TEXT NOT NULL DEFAULT 'hash_bucket'"
                )
            if "updated_at" not in existing_assignment_columns:
                connection.execute(
                    "ALTER TABLE recommendation_experiment_assignments "
                    "ADD COLUMN updated_at REAL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_impressions (
                    id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    model_id TEXT,
                    experiment_key TEXT,
                    experiment_variant TEXT,
                    row_id TEXT,
                    track_id TEXT NOT NULL,
                    rank_index INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendation_impressions_user_time "
                "ON recommendation_impressions(user_scope_id, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_attributed_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    impression_id TEXT,
                    user_scope_id TEXT NOT NULL,
                    session_id TEXT,
                    model_id TEXT,
                    experiment_key TEXT,
                    experiment_variant TEXT,
                    row_id TEXT,
                    track_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    weight REAL NOT NULL,
                    rank_index INTEGER,
                    occurred_at REAL NOT NULL,
                    payload_json TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_attr_interactions_experiment_time "
                "ON recommendation_attributed_interactions(experiment_key, experiment_variant, occurred_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_attr_interactions_user_time "
                "ON recommendation_attributed_interactions(user_scope_id, occurred_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_experiment_promotions (
                    id TEXT PRIMARY KEY,
                    experiment_key TEXT NOT NULL,
                    promoted_variant TEXT NOT NULL,
                    baseline_variant TEXT,
                    score REAL NOT NULL,
                    score_margin REAL NOT NULL,
                    impression_count INTEGER NOT NULL,
                    evaluation_window_hours INTEGER NOT NULL,
                    payload_json TEXT,
                    created_at REAL NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reco_experiment_promotions_active "
                "ON recommendation_experiment_promotions(experiment_key, is_active, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_sync_state (
                    name TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()


def _recommendation_external_pg_connection():
    if psycopg is None or not RECOMMENDATION_SYNC_DATABASE_DSN:
        return None
    connection = psycopg.connect(RECOMMENDATION_SYNC_DATABASE_DSN)
    connection.autocommit = True
    return connection


def _recommendation_event_weight(event_type: Optional[str]) -> float:
    normalized = (event_type or "").strip().lower()
    if normalized == "complete":
        return 3.4
    if normalized == "download":
        return 3.0
    if normalized == "library":
        return 3.2
    if normalized == "skip":
        return -2.0
    return 1.0


def _recommendation_sync_state_get(name: str, default: str = "0") -> str:
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            "SELECT value FROM recommendation_sync_state WHERE name = ?",
            [name],
        ).fetchone()
        if row is None:
            return default
        value = (row["value"] or "").strip()
        return value or default
    finally:
        connection.close()


def _recommendation_sync_state_set(name: str, value: str):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT INTO recommendation_sync_state(name, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            [name, value, time.time()],
        )
        connection.commit()
    finally:
        connection.close()


def _recommendation_sync_state_float(name: str, default: float = 0.0) -> float:
    try:
        return float(_recommendation_sync_state_get(name, str(default)) or default)
    except Exception:
        return default


def _recommendation_active_promotion():
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT id, promoted_variant, baseline_variant, score, score_margin,
                   impression_count, evaluation_window_hours, payload_json, created_at
            FROM recommendation_experiment_promotions
            WHERE experiment_key = ? AND is_active = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [RECOMMENDATION_EXPERIMENT_KEY],
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        promoted_variant = _recommendation_trim_text(
            _recommendation_sync_state_get("experiment_promoted_variant", "")
        )
        if not promoted_variant:
            return None
        return {
            "promotion_id": "",
            "promoted_variant": promoted_variant,
            "baseline_variant": _recommendation_sync_state_get(
                "experiment_promotion_baseline",
                "",
            ),
            "score": _recommendation_sync_state_float("experiment_promotion_score", 0.0),
            "score_margin": _recommendation_sync_state_float(
                "experiment_promotion_score_margin",
                0.0,
            ),
            "impression_count": int(
                _recommendation_sync_state_float(
                    "experiment_promotion_impression_count",
                    0.0,
                )
            ),
            "evaluation_window_hours": int(
                _recommendation_sync_state_float(
                    "experiment_promotion_window_hours",
                    float(RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
                )
            ),
            "payload": {},
            "created_at": _recommendation_sync_state_float(
                "experiment_promoted_at",
                0.0,
            ),
        }
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    return {
        "promotion_id": row["id"] or "",
        "promoted_variant": row["promoted_variant"] or "",
        "baseline_variant": row["baseline_variant"] or "",
        "score": float(row["score"] or 0.0),
        "score_margin": float(row["score_margin"] or 0.0),
        "impression_count": int(row["impression_count"] or 0),
        "evaluation_window_hours": int(row["evaluation_window_hours"] or 0),
        "payload": payload,
        "created_at": float(row["created_at"] or 0.0),
    }


def _recommendation_find_recent_impression(
    user_scope_id: str,
    track_id: str,
    occurred_at: float,
):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        return connection.execute(
            """
            SELECT id, session_id, model_id, experiment_key, experiment_variant,
                   row_id, rank_index, created_at
            FROM recommendation_impressions
            WHERE user_scope_id = ? AND track_id = ?
              AND created_at <= ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [
                user_scope_id,
                track_id,
                occurred_at,
                occurred_at - RECOMMENDATION_ATTRIBUTION_WINDOW_SECONDS,
            ],
        ).fetchone()
    finally:
        connection.close()


def _recommendation_attribute_interaction_event(
    interaction_id: str,
    *,
    user_scope_id: str,
    track_id: str,
    event_type: str,
    weight: float,
    occurred_at: float,
    payload: Optional[Dict[str, Any]] = None,
):
    impression = _recommendation_find_recent_impression(
        user_scope_id,
        track_id,
        occurred_at,
    )
    if impression is None:
        return False
    payload_json = json.dumps(
        {
            **(payload or {}),
            "impression_created_at": float(impression["created_at"] or 0.0),
            "attribution_latency_seconds": max(
                0.0,
                float(occurred_at) - float(impression["created_at"] or 0.0),
            ),
        },
        ensure_ascii=False,
    )
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_attributed_interactions(
                interaction_id, impression_id, user_scope_id, session_id, model_id,
                experiment_key, experiment_variant, row_id, track_id, event_type,
                weight, rank_index, occurred_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                interaction_id,
                impression["id"] or None,
                user_scope_id,
                impression["session_id"] or None,
                impression["model_id"] or None,
                impression["experiment_key"] or None,
                impression["experiment_variant"] or None,
                impression["row_id"] or None,
                track_id,
                event_type,
                float(weight),
                int(impression["rank_index"] or 0),
                float(occurred_at),
                payload_json,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return True


def _recommendation_experiment_dashboard(window_hours: int = None):
    evaluation_window_hours = max(
        1,
        int(window_hours or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
    )
    cutoff = time.time() - (evaluation_window_hours * 3600)
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    variants = {}
    model_breakdown = []
    try:
        impression_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS impression_count,
                   COUNT(DISTINCT user_scope_id) AS user_count,
                   COUNT(DISTINCT session_id) AS session_count
            FROM recommendation_impressions
            WHERE experiment_key = ? AND created_at >= ?
            GROUP BY experiment_variant
            ORDER BY impression_count DESC
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in impression_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            variants[variant] = {
                "variant": variant,
                "impression_count": int(row["impression_count"] or 0),
                "user_count": int(row["user_count"] or 0),
                "session_count": int(row["session_count"] or 0),
                "interaction_counts": {},
                "engaged_impression_count": 0,
                "weighted_outcome": 0.0,
            }

        interaction_rows = connection.execute(
            """
            SELECT experiment_variant, event_type,
                   COUNT(*) AS event_count,
                   COUNT(DISTINCT impression_id) AS engaged_impression_count,
                   COALESCE(SUM(weight), 0) AS weighted_outcome
            FROM recommendation_attributed_interactions
            WHERE experiment_key = ? AND occurred_at >= ?
            GROUP BY experiment_variant, event_type
            ORDER BY experiment_variant ASC, event_type ASC
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in interaction_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            variant_entry = variants.setdefault(
                variant,
                {
                    "variant": variant,
                    "impression_count": 0,
                    "user_count": 0,
                    "session_count": 0,
                    "interaction_counts": {},
                    "engaged_impression_count": 0,
                    "weighted_outcome": 0.0,
                },
            )
            event_type = _recommendation_trim_text(row["event_type"] or "unknown")
            variant_entry["interaction_counts"][event_type] = int(row["event_count"] or 0)
            if event_type in {"play", "complete", "library", "download"}:
                variant_entry["engaged_impression_count"] += int(
                    row["engaged_impression_count"] or 0
                )
            variant_entry["weighted_outcome"] += float(row["weighted_outcome"] or 0.0)

        model_rows = connection.execute(
            """
            SELECT experiment_variant, COALESCE(model_id, '') AS model_id,
                   COUNT(*) AS impression_count
            FROM recommendation_impressions
            WHERE experiment_key = ? AND created_at >= ?
            GROUP BY experiment_variant, COALESCE(model_id, '')
            ORDER BY impression_count DESC
            LIMIT 16
            """,
            [RECOMMENDATION_EXPERIMENT_KEY, cutoff],
        ).fetchall()
        for row in model_rows:
            model_breakdown.append(
                {
                    "variant": _recommendation_trim_text(
                        row["experiment_variant"] or "unknown"
                    ),
                    "model_id": _recommendation_trim_text(row["model_id"]),
                    "impression_count": int(row["impression_count"] or 0),
                }
            )
    finally:
        connection.close()

    ranked_variants = []
    for variant in variants.values():
        impressions = max(int(variant.get("impression_count") or 0), 0)
        interactions = variant.get("interaction_counts") or {}
        engaged_impressions = min(
            impressions,
            max(int(variant.get("engaged_impression_count") or 0), 0),
        )
        weighted_outcome = float(variant.get("weighted_outcome") or 0.0)
        play_count = int(interactions.get("play") or 0)
        complete_count = int(interactions.get("complete") or 0)
        library_count = int(interactions.get("library") or 0)
        download_count = int(interactions.get("download") or 0)
        skip_count = int(interactions.get("skip") or 0)
        variant["engagement_rate"] = round(
            (engaged_impressions / impressions) if impressions else 0.0,
            4,
        )
        variant["play_rate"] = round(
            (play_count / impressions) if impressions else 0.0,
            4,
        )
        variant["completion_rate"] = round(
            (complete_count / impressions) if impressions else 0.0,
            4,
        )
        variant["save_rate"] = round(
            ((library_count + download_count) / impressions) if impressions else 0.0,
            4,
        )
        variant["skip_rate"] = round(
            (skip_count / impressions) if impressions else 0.0,
            4,
        )
        variant["score_per_impression"] = round(
            (weighted_outcome / impressions) if impressions else 0.0,
            4,
        )
        variant["weighted_outcome"] = round(weighted_outcome, 4)
        ranked_variants.append(variant)

    ranked_variants.sort(
        key=lambda item: (
            float(item.get("score_per_impression") or 0.0),
            float(item.get("engagement_rate") or 0.0),
            int(item.get("impression_count") or 0),
        ),
        reverse=True,
    )
    active_promotion = _recommendation_active_promotion()
    return {
        "experiment_key": RECOMMENDATION_EXPERIMENT_KEY,
        "evaluation_window_hours": evaluation_window_hours,
        "promotion_enabled": RECOMMENDATION_PROMOTE_WINNER,
        "minimum_impressions": RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS,
        "minimum_margin": RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN,
        "active_promotion": active_promotion,
        "variants": ranked_variants,
        "model_breakdown": model_breakdown,
    }


def _recommendation_evaluate_experiment(*, force_promote: bool = False, window_hours: int = None):
    dashboard = _recommendation_experiment_dashboard(window_hours=window_hours)
    variants = list(dashboard.get("variants") or [])
    eligible_variants = [
        variant
        for variant in variants
        if int(variant.get("impression_count") or 0) >= RECOMMENDATION_EXPERIMENT_MIN_IMPRESSIONS
    ]
    if len(eligible_variants) < 2:
        return {
            "evaluated": False,
            "promoted": False,
            "reason": "insufficient_impressions",
            "dashboard": dashboard,
        }

    winner = eligible_variants[0]
    runner_up = eligible_variants[1]
    score_margin = round(
        float(winner.get("score_per_impression") or 0.0)
        - float(runner_up.get("score_per_impression") or 0.0),
        4,
    )
    if not force_promote and score_margin < RECOMMENDATION_EXPERIMENT_MIN_SCORE_MARGIN:
        return {
            "evaluated": True,
            "promoted": False,
            "reason": "margin_too_small",
            "score_margin": score_margin,
            "winner": winner,
            "runner_up": runner_up,
            "dashboard": dashboard,
        }

    active_promotion = dashboard.get("active_promotion") or {}
    promoted_variant = _recommendation_trim_text(winner.get("variant"))
    if (
        promoted_variant
        and promoted_variant == _recommendation_trim_text(active_promotion.get("promoted_variant"))
    ):
        return {
            "evaluated": True,
            "promoted": False,
            "reason": "winner_already_active",
            "winner": winner,
            "runner_up": runner_up,
            "score_margin": score_margin,
            "dashboard": dashboard,
        }

    promotion_id = str(uuid.uuid4())
    created_at = time.time()
    payload = {
        "winner": winner,
        "runner_up": runner_up,
        "dashboard": dashboard,
    }
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            UPDATE recommendation_experiment_promotions
            SET is_active = 0
            WHERE experiment_key = ?
            """,
            [RECOMMENDATION_EXPERIMENT_KEY],
        )
        connection.execute(
            """
            INSERT INTO recommendation_experiment_promotions(
                id, experiment_key, promoted_variant, baseline_variant, score,
                score_margin, impression_count, evaluation_window_hours,
                payload_json, created_at, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                promotion_id,
                RECOMMENDATION_EXPERIMENT_KEY,
                promoted_variant,
                _recommendation_trim_text(runner_up.get("variant")) or None,
                float(winner.get("score_per_impression") or 0.0),
                score_margin,
                int(winner.get("impression_count") or 0),
                int(dashboard.get("evaluation_window_hours") or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS),
                json.dumps(payload, ensure_ascii=False),
                created_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()

    _recommendation_sync_state_set("experiment_promoted_variant", promoted_variant)
    _recommendation_sync_state_set(
        "experiment_promotion_baseline",
        _recommendation_trim_text(runner_up.get("variant")),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_score",
        str(float(winner.get("score_per_impression") or 0.0)),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_score_margin",
        str(score_margin),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_impression_count",
        str(int(winner.get("impression_count") or 0)),
    )
    _recommendation_sync_state_set(
        "experiment_promotion_window_hours",
        str(int(dashboard.get("evaluation_window_hours") or RECOMMENDATION_EXPERIMENT_EVAL_WINDOW_HOURS)),
    )
    _recommendation_sync_state_set("experiment_promoted_at", str(created_at))
    return {
        "evaluated": True,
        "promoted": True,
        "winner": winner,
        "runner_up": runner_up,
        "score_margin": score_margin,
        "promotion_id": promotion_id,
        "dashboard": _recommendation_experiment_dashboard(window_hours=window_hours),
    }


def _recommendation_runtime_snapshot(version_limit: int = 5):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    feature_store_counts = {}
    recent_versions = []
    impressions_by_variant = {}
    attributed_interactions_by_variant = {}
    model_version_count = 0
    try:
        feature_rows = connection.execute(
            """
            SELECT namespace, COUNT(*) AS row_count
            FROM recommendation_feature_store
            GROUP BY namespace
            ORDER BY namespace ASC
            """
        ).fetchall()
        for row in feature_rows:
            namespace = _recommendation_trim_text(row["namespace"])
            if not namespace:
                continue
            feature_store_counts[namespace] = int(row["row_count"] or 0)

        version_count_row = connection.execute(
            "SELECT COUNT(*) AS version_count FROM recommendation_model_versions"
        ).fetchone()
        model_version_count = int((version_count_row["version_count"] or 0) if version_count_row else 0)
        version_rows = connection.execute(
            """
            SELECT id, model_kind, created_at, is_active, metrics_json
            FROM recommendation_model_versions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [max(1, min(int(version_limit or 5), 24))],
        ).fetchall()
        for row in version_rows:
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except Exception:
                metrics = {}
            recent_versions.append(
                {
                    "model_id": row["id"] or "",
                    "model_kind": row["model_kind"] or "",
                    "created_at": float(row["created_at"] or 0),
                    "is_active": bool(row["is_active"]),
                    "metrics": metrics,
                }
            )

        variant_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS impression_count
            FROM recommendation_impressions
            GROUP BY experiment_variant
            ORDER BY impression_count DESC
            """
        ).fetchall()
        for row in variant_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            impressions_by_variant[variant or "unknown"] = int(
                row["impression_count"] or 0
            )

        attributed_rows = connection.execute(
            """
            SELECT experiment_variant, COUNT(*) AS interaction_count
            FROM recommendation_attributed_interactions
            GROUP BY experiment_variant
            ORDER BY interaction_count DESC
            """
        ).fetchall()
        for row in attributed_rows:
            variant = _recommendation_trim_text(row["experiment_variant"] or "unknown")
            attributed_interactions_by_variant[variant or "unknown"] = int(
                row["interaction_count"] or 0
            )
    finally:
        connection.close()

    active_promotion = _recommendation_active_promotion()

    return {
        "feature_store_counts": feature_store_counts,
        "model_version_count": model_version_count,
        "recent_versions": recent_versions,
        "impressions_by_variant": impressions_by_variant,
        "attributed_interactions_by_variant": attributed_interactions_by_variant,
        "external_worker_expected": RECOMMENDATION_EXTERNAL_WORKER,
        "last_external_sync_at": _recommendation_sync_state_float("external_last_sync_at", 0.0),
        "last_external_synced_count": int(
            _recommendation_sync_state_float("external_last_synced_count", 0.0)
        ),
        "last_external_sync_error": _recommendation_sync_state_get(
            "external_last_sync_error",
            "",
        ),
        "last_scheduler_sync_at": _recommendation_sync_state_float(
            "scheduler_last_sync_at",
            0.0,
        ),
        "last_scheduler_train_at": _recommendation_sync_state_float(
            "scheduler_last_train_at",
            0.0,
        ),
        "last_scheduler_error": _recommendation_sync_state_get(
            "scheduler_last_error",
            "",
        ),
        "worker_mode": _recommendation_sync_state_get("worker_mode", ""),
        "worker_status": _recommendation_sync_state_get("worker_status", ""),
        "worker_process_id": _recommendation_sync_state_get("worker_process_id", ""),
        "worker_started_at": _recommendation_sync_state_float("worker_started_at", 0.0),
        "worker_last_heartbeat_at": _recommendation_sync_state_float(
            "worker_last_heartbeat_at",
            0.0,
        ),
        "last_trained_signature": _recommendation_sync_state_get(
            "scheduler_last_trained_signature",
            "",
        ),
        "active_promotion": active_promotion,
        "export_dir": RECOMMENDATION_MODEL_EXPORT_DIR,
    }


def _recommendation_store_search_event(req: RecommendationSearchEventRequest):
    _recommendation_init_store_db()
    query = _recommendation_trim_text(req.query)
    if not query:
        return False
    user_scope_id = _assistant_safe_scope_id(req.user_scope_id or "guest")
    occurred_at = float(req.occurred_at or time.time())
    payload = dict(req.metadata or {})
    event_id = hashlib.sha1(
        json.dumps(
            {
                "user_scope_id": user_scope_id,
                "query": query,
                "occurred_at": round(occurred_at, 3),
                "source": req.source or "app",
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_search_events(
                id, user_scope_id, query, result_count, source, metadata_json, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                user_scope_id,
                query,
                max(int(req.result_count or 0), 0),
                _recommendation_trim_text(req.source or "app") or "app",
                json.dumps(payload, ensure_ascii=False),
                occurred_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _recommendation_invalidate_collaborative_cache()
    return True


def _recommendation_feature_store_upsert_many(rows):
    if not rows:
        return
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        connection.executemany(
            """
            INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _recommendation_assignment_for_user(user_scope_id: str):
    _recommendation_init_store_db()
    normalized_user_scope_id = _assistant_safe_scope_id(user_scope_id or "guest")
    active_promotion = _recommendation_active_promotion() or {}
    promoted_variant = _recommendation_trim_text(active_promotion.get("promoted_variant"))
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT variant, assignment_source
            FROM recommendation_experiment_assignments
            WHERE user_scope_id = ? AND experiment_key = ?
            """,
            [normalized_user_scope_id, RECOMMENDATION_EXPERIMENT_KEY],
        ).fetchone()
        if promoted_variant:
            if (
                row is None
                or _recommendation_trim_text(row["variant"]) != promoted_variant
                or _recommendation_trim_text(row["assignment_source"]) != "promotion"
            ):
                connection.execute(
                    """
                    INSERT INTO recommendation_experiment_assignments(
                        user_scope_id, experiment_key, variant, assigned_at, assignment_source, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_scope_id, experiment_key) DO UPDATE SET
                        variant = excluded.variant,
                        assignment_source = excluded.assignment_source,
                        updated_at = excluded.updated_at
                    """,
                    [
                        normalized_user_scope_id,
                        RECOMMENDATION_EXPERIMENT_KEY,
                        promoted_variant,
                        time.time(),
                        "promotion",
                        time.time(),
                    ],
                )
                connection.commit()
            return promoted_variant

        if row is not None:
            existing_source = _recommendation_trim_text(row["assignment_source"])
            if existing_source != "promotion":
                return (row["variant"] or "control").strip() or "control"

        digest = hashlib.sha1(
            f"{RECOMMENDATION_EXPERIMENT_KEY}:{normalized_user_scope_id}".encode("utf-8")
        ).hexdigest()
        bucket = int(digest[:8], 16) % 100
        variant = "collab_heavy" if bucket >= 50 else "control"
        connection.execute(
            """
            INSERT INTO recommendation_experiment_assignments(
                user_scope_id, experiment_key, variant, assigned_at, assignment_source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_scope_id, experiment_key) DO UPDATE SET
                variant = excluded.variant,
                assignment_source = excluded.assignment_source,
                updated_at = excluded.updated_at
            """,
            [
                normalized_user_scope_id,
                RECOMMENDATION_EXPERIMENT_KEY,
                variant,
                time.time(),
                "hash_bucket",
                time.time(),
            ],
        )
        connection.commit()
        return variant
    finally:
        connection.close()


def _recommendation_record_impressions(session, rows):
    if not isinstance(session, dict):
        return
    if not rows:
        return
    _recommendation_init_store_db()
    session_id = _recommendation_trim_text(session.get("session_id"))
    user_scope_id = _assistant_safe_scope_id(session.get("user_scope_id") or "guest")
    diagnostics = session.get("diagnostics") or {}
    model_id = _recommendation_trim_text(diagnostics.get("collaborative_model_id"))
    variant = _recommendation_trim_text(diagnostics.get("experiment_variant") or "control")
    created_at = time.time()
    payload_rows = []
    for row in rows:
        row_id = _recommendation_trim_text(row.get("id"))
        for index, item in enumerate(row.get("items") or []):
            track_id = _recommendation_trim_text(item.get("id"))
            if not track_id:
                continue
            impression_id = hashlib.sha1(
                f"{session_id}:{row_id}:{track_id}:{index}".encode("utf-8")
            ).hexdigest()
            payload_rows.append(
                [
                    impression_id,
                    user_scope_id,
                    session_id,
                    model_id or None,
                    RECOMMENDATION_EXPERIMENT_KEY,
                    variant,
                    row_id or None,
                    track_id,
                    index,
                    created_at,
                    json.dumps(
                        {
                            "generator_score": item.get("generator_score"),
                            "ml_similarities": item.get("ml_similarities") or {},
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
    if not payload_rows:
        return
    connection = _recommendation_store_connection()
    try:
        connection.executemany(
            """
            INSERT OR IGNORE INTO recommendation_impressions(
                id, user_scope_id, session_id, model_id, experiment_key,
                experiment_variant, row_id, track_id, rank_index, created_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload_rows,
        )
        connection.commit()
    finally:
        connection.close()


def _recommendation_invalidate_collaborative_cache():
    with recommendation_model_lock:
        recommendation_model_cache["artifact"] = None
        recommendation_model_cache["source_signature"] = ""
        recommendation_model_cache["expires_at"] = 0
    with recommendation_profile_lock:
        recommendation_profile_cache.clear()
    with recommendation_feed_lock:
        recommendation_feed_sessions.clear()
        recommendation_feed_index.clear()
    with search_result_cache_lock:
        search_result_cache["recommended_artists"] = {}


def _recommendation_store_interaction_event(req: RecommendationInteractionEventRequest):
    _recommendation_init_store_db()
    track_id = _recommendation_trim_text(req.track_id)
    if not track_id:
        return False
    user_scope_id = _assistant_safe_scope_id(req.user_scope_id or "guest")
    event_type = (req.event_type or "play").strip().lower() or "play"
    artist_name = _recommendation_trim_text(
        req.artist_name
        or (req.metadata or {}).get("channel")
        or (req.metadata or {}).get("artist")
        or (req.metadata or {}).get("author")
    )
    occurred_at = float(req.occurred_at or time.time())
    payload = dict(req.metadata or {})
    payload.setdefault("track_id", track_id)
    event_id = hashlib.sha1(
        json.dumps(
            {
                "user_scope_id": user_scope_id,
                "track_id": track_id,
                "event_type": event_type,
                "occurred_at": round(occurred_at, 3),
                "source": req.source or "app",
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    connection = _recommendation_store_connection()
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_events(
                id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                user_scope_id,
                track_id,
                artist_name or None,
                event_type,
                _recommendation_trim_text(req.source or "app") or "app",
                _recommendation_event_weight(event_type),
                json.dumps(payload, ensure_ascii=False),
                occurred_at,
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _recommendation_attribute_interaction_event(
        event_id,
        user_scope_id=user_scope_id,
        track_id=track_id,
        event_type=event_type,
        weight=_recommendation_event_weight(event_type),
        occurred_at=occurred_at,
        payload=payload,
    )
    _recommendation_invalidate_collaborative_cache()
    return True


def _recommendation_sync_external_events(force: bool = False):
    _recommendation_init_store_db()
    if psycopg is None or not RECOMMENDATION_SYNC_DATABASE_DSN:
        _recommendation_sync_state_set("external_last_sync_error", "")
        return {"synced": 0, "enabled": False}

    connection = _recommendation_external_pg_connection()
    if connection is None:
        _recommendation_sync_state_set(
            "external_last_sync_error",
            "psycopg connection unavailable",
        )
        return {"synced": 0, "enabled": False}

    local_connection = _recommendation_store_connection()
    total_synced = 0
    last_play_ts = float(_recommendation_sync_state_get("external_play_ts", "0") or 0)
    last_library_ts = float(_recommendation_sync_state_get("external_library_ts", "0") or 0)
    last_search_ts = float(_recommendation_sync_state_get("external_search_ts", "0") or 0)

    try:
        with connection.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, track_id, event_type,
                           COALESCE(metadata::text, '{}'),
                           EXTRACT(EPOCH FROM created_at)
                    FROM public.play_events
                    WHERE created_at > to_timestamp(%s)
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    [last_play_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    event_id, user_id, track_id, event_type, metadata_json, created_at = row
                    normalized_track_id = _recommendation_trim_text(track_id)
                    if not normalized_track_id:
                        continue
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_events(
                            id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_play:{event_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_track_id,
                            None,
                            (event_type or "play").strip().lower() or "play",
                            "supabase_pg",
                            _recommendation_event_weight(event_type),
                            metadata_json or "{}",
                            float(created_at or time.time()),
                        ],
                    )
                    last_play_ts = max(last_play_ts, float(created_at or last_play_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break

            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, query, result_count,
                           EXTRACT(EPOCH FROM created_at)
                    FROM public.search_events
                    WHERE created_at > to_timestamp(%s)
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    [last_search_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    search_id, user_id, query, result_count, created_at = row
                    normalized_query = _recommendation_trim_text(query)
                    if not normalized_query:
                        continue
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_search_events(
                            id, user_scope_id, query, result_count, source, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_search:{search_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_query,
                            max(int(result_count or 0), 0),
                            "supabase_pg",
                            "{}",
                            float(created_at or time.time()),
                        ],
                    )
                    last_search_ts = max(last_search_ts, float(created_at or last_search_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break

            while True:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text, track_id,
                           COALESCE(track_data::text, '{}'),
                           EXTRACT(EPOCH FROM COALESCE(updated_at, added_at))
                    FROM public.library_tracks
                    WHERE COALESCE(updated_at, added_at) > to_timestamp(%s)
                    ORDER BY COALESCE(updated_at, added_at) ASC
                    LIMIT %s
                    """,
                    [last_library_ts, RECOMMENDATION_SYNC_BATCH_SIZE],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    library_id, user_id, track_id, metadata_json, updated_at = row
                    normalized_track_id = _recommendation_trim_text(track_id)
                    if not normalized_track_id:
                        continue
                    try:
                        payload = json.loads(metadata_json or "{}")
                    except Exception:
                        payload = {}
                    artist_name = _recommendation_trim_text(
                        payload.get("channel")
                        or payload.get("artist")
                        or payload.get("author")
                    )
                    local_connection.execute(
                        """
                        INSERT OR IGNORE INTO recommendation_events(
                            id, user_scope_id, track_id, artist_name, event_type, source, weight, metadata_json, occurred_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            f"pg_library:{library_id}",
                            _assistant_safe_scope_id(user_id or "guest"),
                            normalized_track_id,
                            artist_name or None,
                            "library",
                            "supabase_pg",
                            _recommendation_event_weight("library"),
                            metadata_json or "{}",
                            float(updated_at or time.time()),
                        ],
                    )
                    last_library_ts = max(last_library_ts, float(updated_at or last_library_ts))
                    total_synced += 1
                local_connection.commit()
                if len(rows) < RECOMMENDATION_SYNC_BATCH_SIZE:
                    break
    except Exception as exc:
        _recommendation_sync_state_set(
            "external_last_sync_error",
            str(exc)[:1000],
        )
    finally:
        local_connection.close()
        connection.close()

    _recommendation_sync_state_set("external_play_ts", str(last_play_ts))
    _recommendation_sync_state_set("external_library_ts", str(last_library_ts))
    _recommendation_sync_state_set("external_search_ts", str(last_search_ts))
    _recommendation_sync_state_set("external_last_sync_at", str(time.time()))
    _recommendation_sync_state_set("external_last_synced_count", str(total_synced))
    if total_synced or force:
        _recommendation_sync_state_set("external_last_sync_error", "")
    if total_synced or force:
        _recommendation_invalidate_collaborative_cache()
    return {
        "synced": total_synced,
        "enabled": True,
    }


def _recommendation_model_source_signature():
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM recommendation_events) AS event_count,
                (SELECT COALESCE(MAX(occurred_at), 0) FROM recommendation_events) AS max_occurred_at,
                (SELECT COUNT(DISTINCT user_scope_id) FROM recommendation_events) AS user_count,
                (SELECT COUNT(DISTINCT track_id) FROM recommendation_events) AS item_count,
                (SELECT COUNT(*) FROM recommendation_search_events) AS search_event_count,
                (SELECT COALESCE(MAX(occurred_at), 0) FROM recommendation_search_events) AS max_search_occurred_at,
                (SELECT COUNT(DISTINCT user_scope_id) FROM recommendation_search_events) AS search_user_count,
                (SELECT COUNT(DISTINCT query) FROM recommendation_search_events) AS distinct_query_count
            """
        ).fetchone()
        payload = {
            "event_count": int(row["event_count"] or 0),
            "max_occurred_at": round(float(row["max_occurred_at"] or 0), 3),
            "user_count": int(row["user_count"] or 0),
            "item_count": int(row["item_count"] or 0),
            "search_event_count": int(row["search_event_count"] or 0),
            "max_search_occurred_at": round(float(row["max_search_occurred_at"] or 0), 3),
            "search_user_count": int(row["search_user_count"] or 0),
            "distinct_query_count": int(row["distinct_query_count"] or 0),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    finally:
        connection.close()


def _recommendation_seeded_vector(namespace: str, key: str, dimension: int):
    values = []
    for index in range(dimension):
        digest = hashlib.sha256(f"{namespace}:{key}:{index}".encode("utf-8")).digest()
        ratio = int.from_bytes(digest[:4], "big") / 4294967295.0
        values.append((ratio - 0.5) * 0.12)
    return values


def _recommendation_vector_dot(left: Optional[List[float]], right: Optional[List[float]]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(float(a) * float(b) for a, b in zip(left, right))


def _recommendation_sigmoid(value: float) -> float:
    bounded = max(min(float(value), 18.0), -18.0)
    return 1.0 / (1.0 + math.exp(-bounded))


def _recommendation_sample_negative_item(
    user_id: str,
    positive_item_id: str,
    epoch: int,
    round_index: int,
    all_items,
    positive_item_ids,
):
    if not all_items:
        return None
    digest = hashlib.sha1(
        f"{user_id}:{positive_item_id}:{epoch}:{round_index}".encode("utf-8")
    ).hexdigest()
    base_index = int(digest[:8], 16) % len(all_items)
    for offset in range(len(all_items)):
        candidate = all_items[(base_index + offset) % len(all_items)]
        if candidate not in positive_item_ids:
            return candidate
    return None


def _recommendation_round_vector(values: Optional[List[float]], digits: int = 6):
    if not values:
        return []
    return [round(float(value), digits) for value in values]


def _recommendation_train_collaborative_model(source_signature: str):
    _recommendation_init_store_db()
    connection = _recommendation_store_connection()
    try:
        event_rows = connection.execute(
            """
            SELECT user_scope_id, track_id, artist_name, event_type, weight, metadata_json, occurred_at
            FROM recommendation_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [RECOMMENDATION_MODEL_MAX_EVENTS],
        ).fetchall()
        search_rows = connection.execute(
            """
            SELECT user_scope_id, query, result_count, metadata_json, occurred_at
            FROM recommendation_search_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [RECOMMENDATION_MODEL_MAX_EVENTS],
        ).fetchall()
    finally:
        connection.close()

    event_count = len(event_rows)
    if event_count < RECOMMENDATION_MODEL_MIN_EVENTS:
        return {
            "ready": False,
            "reason": "insufficient_events",
            "event_count": event_count,
            "trained_at": time.time(),
            "source_signature": source_signature,
        }

    now = time.time()
    user_track_weights = defaultdict(dict)
    user_positive_order = defaultdict(list)
    track_artists = {}
    item_popularity = defaultdict(float)
    user_search_weights = defaultdict(dict)
    query_track_scores = defaultdict(dict)
    query_artist_scores = defaultdict(dict)

    for row in event_rows:
        user_scope_id = _assistant_safe_scope_id(row["user_scope_id"] or "guest")
        track_id = _recommendation_trim_text(row["track_id"])
        if not track_id:
            continue
        event_type = (row["event_type"] or "play").strip().lower() or "play"
        base_weight = float(row["weight"] or _recommendation_event_weight(event_type))
        occurred_at = float(row["occurred_at"] or now)
        age_days = max(0.0, (now - occurred_at) / 86400.0)
        recency_weight = max(0.35, 1.0 / (1.0 + (age_days / 45.0)))
        weighted_value = base_weight * recency_weight

        try:
            payload = json.loads(row["metadata_json"] or "{}")
        except Exception:
            payload = {}
        artist_name = _recommendation_trim_text(
            row["artist_name"]
            or payload.get("channel")
            or payload.get("artist")
            or payload.get("author")
        )
        if artist_name:
            track_artists[track_id] = artist_name

        user_values = user_track_weights[user_scope_id]
        user_values[track_id] = user_values.get(track_id, 0.0) + weighted_value
        if weighted_value > 0:
            item_popularity[track_id] += weighted_value
            positives = user_positive_order[user_scope_id]
            if track_id not in positives:
                positives.append(track_id)

    for row in search_rows:
        user_scope_id = _assistant_safe_scope_id(row["user_scope_id"] or "guest")
        query = _recommendation_trim_text(row["query"])
        if not query:
            continue
        occurred_at = float(row["occurred_at"] or now)
        age_days = max(0.0, (now - occurred_at) / 86400.0)
        recency_weight = max(0.3, 1.0 / (1.0 + (age_days / 30.0)))
        base_weight = 1.0 + min(max(int(row["result_count"] or 0), 0), 12) * 0.04
        normalized_query = _normalize_text(query)
        user_search_weights[user_scope_id][normalized_query] = (
            user_search_weights[user_scope_id].get(normalized_query, 0.0)
            + (base_weight * recency_weight)
        )

    positive_user_items = {}
    holdout_track_by_user = {}
    for user_scope_id, weights in user_track_weights.items():
        positives = {
            track_id: score
            for track_id, score in weights.items()
            if score > 0.2
        }
        positive_order = [
            track_id for track_id in user_positive_order.get(user_scope_id, [])
            if positives.get(track_id, 0.0) > 0.2
        ]
        if len(positive_order) >= 3:
            holdout_track_by_user[user_scope_id] = positive_order[0]
            positives.pop(positive_order[0], None)
        if positives:
            positive_user_items[user_scope_id] = dict(
                sorted(
                    positives.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:80]
            )

    all_items = [
        track_id
        for track_id, _score in sorted(
            item_popularity.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    if len(all_items) < 2 or not positive_user_items:
        return {
            "ready": False,
            "reason": "insufficient_positive_matrix",
            "event_count": event_count,
            "trained_at": time.time(),
            "source_signature": source_signature,
        }

    dimension = RECOMMENDATION_MODEL_FACTOR_DIM
    item_factors = {
        track_id: _recommendation_seeded_vector("item", track_id, dimension)
        for track_id in all_items
    }
    user_factors = {}
    for user_scope_id, positives in positive_user_items.items():
        seeded = _recommendation_seeded_vector("user", user_scope_id, dimension)
        blended = _vector_weighted_average(
            [
                (item_factors[track_id], float(weight))
                for track_id, weight in positives.items()
                if track_id in item_factors
            ]
        )
        user_factors[user_scope_id] = _vector_weighted_average(
            [
                (seeded, 0.35),
                (blended, 1.65),
            ]
        ) or seeded

    for epoch in range(RECOMMENDATION_MODEL_EPOCHS):
        for user_scope_id in sorted(positive_user_items.keys()):
            positives = list(positive_user_items[user_scope_id].items())[:32]
            positive_item_ids = {track_id for track_id, _weight in positives}
            user_vector = user_factors.setdefault(
                user_scope_id,
                _recommendation_seeded_vector("user", user_scope_id, dimension),
            )
            for sample_index, (positive_item_id, interaction_weight) in enumerate(positives):
                positive_vector = item_factors.get(positive_item_id)
                if positive_vector is None:
                    continue
                negative_item_id = _recommendation_sample_negative_item(
                    user_scope_id,
                    positive_item_id,
                    epoch,
                    sample_index,
                    all_items,
                    positive_item_ids,
                )
                if negative_item_id is None:
                    continue
                negative_vector = item_factors.get(negative_item_id)
                if negative_vector is None:
                    continue
                margin = (
                    _recommendation_vector_dot(user_vector, positive_vector)
                    - _recommendation_vector_dot(user_vector, negative_vector)
                )
                gradient = _recommendation_sigmoid(-margin)
                step = 0.04 * min(max(float(interaction_weight), 0.45), 3.5)
                regularization = 0.0015
                for index in range(dimension):
                    user_value = user_vector[index]
                    positive_value = positive_vector[index]
                    negative_value = negative_vector[index]
                    user_vector[index] += step * (
                        ((positive_value - negative_value) * gradient)
                        - (regularization * user_value)
                    )
                    positive_vector[index] += step * (
                        (user_value * gradient)
                        - (regularization * positive_value)
                    )
                    negative_vector[index] += step * (
                        ((-user_value) * gradient)
                        - (regularization * negative_value)
                    )
            user_factors[user_scope_id] = _vector_normalize(user_vector)

    for track_id in list(item_factors.keys()):
        item_factors[track_id] = _vector_normalize(item_factors[track_id])

    item_neighbors = {}
    co_occurrence = defaultdict(dict)
    for positives in positive_user_items.values():
        top_items = list(positives.items())[:48]
        if len(top_items) < 2:
            continue
        normalizer = math.log(2 + len(top_items))
        for index, (left_track_id, left_weight) in enumerate(top_items):
            for right_track_id, right_weight in top_items[index + 1:]:
                boost = math.sqrt(max(left_weight, 0.05) * max(right_weight, 0.05)) / normalizer
                co_occurrence[left_track_id][right_track_id] = (
                    co_occurrence[left_track_id].get(right_track_id, 0.0) + boost
                )
                co_occurrence[right_track_id][left_track_id] = (
                    co_occurrence[right_track_id].get(left_track_id, 0.0) + boost
                )

    for track_id in all_items:
        raw_neighbors = []
        for candidate_id, co_score in (co_occurrence.get(track_id) or {}).items():
            similarity = max(
                0.0,
                _assistant_cosine_similarity(
                    item_factors.get(track_id) or [],
                    item_factors.get(candidate_id) or [],
                ),
            )
            raw_neighbors.append(
                (
                    co_score + (similarity * 1.7),
                    candidate_id,
                )
            )
        raw_neighbors.sort(key=lambda item: item[0], reverse=True)
        item_neighbors[track_id] = [
            {
                "track_id": candidate_id,
                "score": round(score, 4),
            }
            for score, candidate_id in raw_neighbors[:RECOMMENDATION_MODEL_NEIGHBOR_LIMIT]
        ]

    user_query_profiles = {}
    for user_scope_id, query_weights in user_search_weights.items():
        if not query_weights:
            continue
        top_queries = sorted(
            query_weights.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:10]
        user_query_profiles[user_scope_id] = [
            {
                "query": query,
                "weight": round(float(weight), 4),
            }
            for query, weight in top_queries
        ]
        positive_tracks = positive_user_items.get(user_scope_id) or {}
        for query, query_weight in top_queries[:6]:
            for track_id, track_weight in list(positive_tracks.items())[:14]:
                boost = float(query_weight) * math.sqrt(max(float(track_weight), 0.05))
                query_track_scores[query][track_id] = (
                    query_track_scores[query].get(track_id, 0.0) + boost
                )
                artist_key = _normalize_text(track_artists.get(track_id) or "")
                if artist_key:
                    query_artist_scores[query][artist_key] = (
                        query_artist_scores[query].get(artist_key, 0.0) + boost
                    )

    for query, track_scores in list(query_track_scores.items()):
        query_track_scores[query] = {
            track_id: round(float(score), 4)
            for track_id, score in sorted(
                track_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:24]
        }
    for query, artist_scores in list(query_artist_scores.items()):
        query_artist_scores[query] = {
            artist_key: round(float(score), 4)
            for artist_key, score in sorted(
                artist_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:18]
        }

    evaluation_samples = 0
    hit_rate_at_10 = 0.0
    reciprocal_rank_total = 0.0
    for user_scope_id, holdout_track_id in holdout_track_by_user.items():
        user_vector = user_factors.get(user_scope_id) or []
        if not user_vector or holdout_track_id not in item_factors:
            continue
        trained_tracks = set((positive_user_items.get(user_scope_id) or {}).keys())
        ranked_candidates = []
        for candidate_id, candidate_vector in item_factors.items():
            if candidate_id in trained_tracks:
                continue
            score = max(0.0, _assistant_cosine_similarity(user_vector, candidate_vector))
            score += float(item_popularity.get(candidate_id) or 0.0) * 0.02
            ranked_candidates.append((score, candidate_id))
        ranked_candidates.sort(key=lambda item: item[0], reverse=True)
        ranking = [candidate_id for _score, candidate_id in ranked_candidates[:50]]
        if holdout_track_id not in ranking:
            continue
        evaluation_samples += 1
        rank = ranking.index(holdout_track_id) + 1
        reciprocal_rank_total += 1.0 / rank
        if rank <= 10:
            hit_rate_at_10 += 1.0

    evaluation_metrics = {
        "offline_users_evaluated": evaluation_samples,
        "hit_rate_at_10": round(
            (hit_rate_at_10 / evaluation_samples) if evaluation_samples else 0.0,
            4,
        ),
        "mrr": round(
            (reciprocal_rank_total / evaluation_samples) if evaluation_samples else 0.0,
            4,
        ),
    }

    model_id = str(uuid.uuid4())

    return {
        "ready": True,
        "model_id": model_id,
        "model_type": "implicit_bpr_collaborative",
        "trained_at": time.time(),
        "source_signature": source_signature,
        "event_count": event_count,
        "search_event_count": len(search_rows),
        "user_count": len(positive_user_items),
        "item_count": len(all_items),
        "factor_dim": dimension,
        "evaluation_metrics": evaluation_metrics,
        "item_popularity": {
            track_id: round(float(score), 4)
            for track_id, score in item_popularity.items()
        },
        "track_artists": track_artists,
        "item_factors": {
            track_id: _recommendation_round_vector(values)
            for track_id, values in item_factors.items()
        },
        "user_factors": {
            user_scope_id: _recommendation_round_vector(values)
            for user_scope_id, values in user_factors.items()
        },
        "item_neighbors": item_neighbors,
        "user_positive_tracks": {
            user_scope_id: [track_id for track_id, _weight in positives.items()]
            for user_scope_id, positives in positive_user_items.items()
        },
        "user_query_profiles": user_query_profiles,
        "query_track_scores": query_track_scores,
        "query_artist_scores": query_artist_scores,
    }


def _recommendation_materialize_feature_store(artifact):
    if not isinstance(artifact, dict) or not artifact.get("ready"):
        return
    model_id = _recommendation_trim_text(artifact.get("model_id"))
    if not model_id:
        return
    updated_at = time.time()
    rows = []
    for user_scope_id, track_ids in (artifact.get("user_positive_tracks") or {}).items():
        rows.append(
            (
                "user_profile",
                user_scope_id,
                model_id,
                json.dumps(
                    {
                        "top_tracks": list(track_ids[:24]),
                        "top_queries": (artifact.get("user_query_profiles") or {}).get(
                            user_scope_id,
                            [],
                        ),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    for query, track_scores in (artifact.get("query_track_scores") or {}).items():
        rows.append(
            (
                "query_profile",
                query,
                model_id,
                json.dumps(
                    {
                        "top_tracks": track_scores,
                        "top_artists": (artifact.get("query_artist_scores") or {}).get(
                            query,
                            {},
                        ),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    for track_id, popularity in (artifact.get("item_popularity") or {}).items():
        rows.append(
            (
                "track_profile",
                track_id,
                model_id,
                json.dumps(
                    {
                        "artist": (artifact.get("track_artists") or {}).get(track_id) or "",
                        "popularity": popularity,
                        "neighbors": (artifact.get("item_neighbors") or {}).get(track_id, []),
                    },
                    ensure_ascii=False,
                ),
                updated_at,
            )
        )
    rows.append(
        (
            "model_metrics",
            model_id,
            model_id,
            json.dumps(artifact.get("evaluation_metrics") or {}, ensure_ascii=False),
            updated_at,
        )
    )
    _recommendation_feature_store_upsert_many(rows)


def _recommendation_export_model_artifact(artifact):
    if not isinstance(artifact, dict) or not artifact.get("ready"):
        return
    model_id = _recommendation_trim_text(artifact.get("model_id"))
    if not model_id:
        return
    try:
        os.makedirs(RECOMMENDATION_MODEL_EXPORT_DIR, exist_ok=True)
        with open(
            os.path.join(RECOMMENDATION_MODEL_EXPORT_DIR, f"{model_id}.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(artifact, handle, ensure_ascii=False)
    except Exception:
        pass


def _recommendation_store_collaborative_model(artifact):
    _recommendation_init_store_db()
    model_id = _recommendation_trim_text(artifact.get("model_id")) or str(uuid.uuid4())
    artifact["model_id"] = model_id
    metrics_json = json.dumps(artifact.get("evaluation_metrics") or {}, ensure_ascii=False)
    created_at = float(artifact.get("trained_at") or time.time())
    connection = _recommendation_store_connection()
    try:
        connection.execute(
            "UPDATE recommendation_model_versions SET is_active = 0"
        )
        connection.execute(
            """
            INSERT INTO recommendation_model_versions(
                id, source_signature, model_kind, artifact_json, metrics_json, created_at, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                source_signature = excluded.source_signature,
                model_kind = excluded.model_kind,
                artifact_json = excluded.artifact_json,
                metrics_json = excluded.metrics_json,
                created_at = excluded.created_at,
                is_active = excluded.is_active
            """,
            [
                model_id,
                artifact.get("source_signature") or "",
                artifact.get("model_type") or "implicit_bpr_collaborative",
                json.dumps(artifact, ensure_ascii=False),
                metrics_json,
                created_at,
            ],
        )
        connection.execute(
            """
            INSERT INTO recommendation_models(
                id, source_signature, artifact_json, metrics_json,
                is_active, created_at, updated_at, model_kind
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_signature = excluded.source_signature,
                artifact_json = excluded.artifact_json,
                metrics_json = excluded.metrics_json,
                is_active = excluded.is_active,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                model_kind = excluded.model_kind
            """,
            [
                "global",
                artifact.get("source_signature") or "",
                json.dumps(artifact, ensure_ascii=False),
                metrics_json,
                created_at,
                time.time(),
                artifact.get("model_type") or "implicit_bpr_collaborative",
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _recommendation_materialize_feature_store(artifact)
    _recommendation_export_model_artifact(artifact)


def _recommendation_run_maintenance_cycle(
    *,
    force_sync: bool = False,
    force_train: bool = False,
    run_experiment_evaluation: bool = False,
):
    _recommendation_init_store_db()
    cycle_started_at = time.time()
    result = {
        "synced": 0,
        "trained": False,
        "model_ready": False,
        "model_id": "",
        "source_signature": "",
        "experiment_evaluation": None,
    }
    try:
        if force_sync or bool(RECOMMENDATION_SYNC_DATABASE_DSN):
            sync_result = _recommendation_sync_external_events(force=force_sync)
            result["synced"] = int((sync_result or {}).get("synced") or 0)
            _recommendation_sync_state_set("scheduler_last_sync_at", str(time.time()))

        source_signature = _recommendation_model_source_signature()
        result["source_signature"] = source_signature
        last_trained_signature = _recommendation_sync_state_get(
            "scheduler_last_trained_signature",
            "",
        )
        last_train_at = _recommendation_sync_state_float("scheduler_last_train_at", 0.0)
        signature_changed = source_signature != last_trained_signature
        train_due = (
            force_train
            or signature_changed
            or (cycle_started_at - last_train_at) >= RECOMMENDATION_TRAIN_INTERVAL_SECONDS
        )
        if train_due:
            model = _recommendation_get_collaborative_model(
                force_refresh=True,
                force_sync=False,
            )
            result["trained"] = True
            result["model_ready"] = bool((model or {}).get("ready"))
            result["model_id"] = _recommendation_trim_text((model or {}).get("model_id"))
            _recommendation_sync_state_set("scheduler_last_train_at", str(time.time()))
            _recommendation_sync_state_set(
                "scheduler_last_trained_signature",
                source_signature,
            )
            _recommendation_sync_state_set(
                "scheduler_last_model_id",
                result["model_id"],
            )
        if RECOMMENDATION_PROMOTE_WINNER and run_experiment_evaluation:
            evaluation = _recommendation_evaluate_experiment()
            result["experiment_evaluation"] = evaluation
            if evaluation.get("evaluated"):
                _recommendation_sync_state_set(
                    "experiment_last_evaluated_at",
                    str(time.time()),
                )
                _recommendation_sync_state_set(
                    "experiment_last_evaluation_reason",
                    _recommendation_trim_text(evaluation.get("reason", "")),
                )
                if evaluation.get("promoted"):
                    _recommendation_sync_state_set(
                        "experiment_last_promoted_at",
                        str(time.time()),
                    )
        _recommendation_sync_state_set("scheduler_last_error", "")
        _recommendation_sync_state_set(
            "scheduler_last_cycle_at",
            str(time.time()),
        )
    except Exception as exc:
        _recommendation_sync_state_set("scheduler_last_error", str(exc)[:1000])
        traceback.print_exc()
    return result


def _recommendation_bootstrap_once():
    _recommendation_run_maintenance_cycle(
        force_sync=bool(RECOMMENDATION_SYNC_DATABASE_DSN),
        force_train=True,
        run_experiment_evaluation=True,
    )


def _recommendation_worker_heartbeat(worker_mode: str, status: str):
    _recommendation_sync_state_set("worker_mode", worker_mode)
    _recommendation_sync_state_set("worker_status", status)
    _recommendation_sync_state_set("worker_process_id", str(os.getpid()))
    _recommendation_sync_state_set("worker_last_heartbeat_at", str(time.time()))


def _recommendation_scheduler_loop(worker_mode: str = "embedded"):
    next_sync_at = 0.0
    next_train_at = 0.0
    next_eval_at = 0.0
    minimum_sync_interval = max(30, RECOMMENDATION_SYNC_INTERVAL_SECONDS)
    minimum_train_interval = max(60, RECOMMENDATION_TRAIN_INTERVAL_SECONDS)
    minimum_eval_interval = max(60, RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS)
    _recommendation_sync_state_set("worker_started_at", str(time.time()))
    _recommendation_worker_heartbeat(worker_mode, "running")
    while not recommendation_scheduler_stop.is_set():
        now = time.time()
        _recommendation_worker_heartbeat(worker_mode, "running")
        should_sync = now >= next_sync_at
        should_train = now >= next_train_at
        should_evaluate = RECOMMENDATION_PROMOTE_WINNER and now >= next_eval_at
        if should_sync or should_train:
            _recommendation_run_maintenance_cycle(
                force_sync=should_sync,
                force_train=should_train,
                run_experiment_evaluation=should_evaluate,
            )
            completed_at = time.time()
            if should_sync:
                next_sync_at = completed_at + minimum_sync_interval
            if should_train:
                next_train_at = completed_at + minimum_train_interval
            if should_evaluate:
                next_eval_at = completed_at + minimum_eval_interval
        elif should_evaluate:
            evaluation = _recommendation_evaluate_experiment()
            completed_at = time.time()
            next_eval_at = completed_at + minimum_eval_interval
            _recommendation_sync_state_set(
                "experiment_last_evaluated_at",
                str(completed_at),
            )
            _recommendation_sync_state_set(
                "experiment_last_evaluation_reason",
                _recommendation_trim_text(evaluation.get("reason", "")),
            )
            if evaluation.get("promoted"):
                _recommendation_sync_state_set(
                    "experiment_last_promoted_at",
                    str(completed_at),
                )
        sleep_for = max(
            5.0,
            min(
                next_sync_at - time.time(),
                next_train_at - time.time(),
                next_eval_at - time.time() if RECOMMENDATION_PROMOTE_WINNER else 30.0,
                30.0,
            ),
        )
        recommendation_scheduler_stop.wait(sleep_for)
    _recommendation_worker_heartbeat(worker_mode, "stopped")


def _recommendation_start_scheduler():
    global recommendation_scheduler_thread
    if (
        recommendation_scheduler_thread is not None
        and recommendation_scheduler_thread.is_alive()
    ):
        return
    recommendation_scheduler_stop.clear()
    recommendation_scheduler_thread = Thread(
        target=_recommendation_scheduler_loop,
        kwargs={"worker_mode": "embedded"},
        name="recommendation-scheduler",
        daemon=True,
    )
    recommendation_scheduler_thread.start()


def _recommendation_stop_scheduler():
    global recommendation_scheduler_thread
    recommendation_scheduler_stop.set()
    thread = recommendation_scheduler_thread
    recommendation_scheduler_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


def run_recommendation_worker_forever():
    recommendation_scheduler_stop.clear()
    try:
        _recommendation_scheduler_loop(worker_mode="external")
    finally:
        _recommendation_worker_heartbeat("external", "stopped")


def _recommendation_get_collaborative_model(force_refresh: bool = False, force_sync: bool = False):
    _recommendation_init_store_db()
    if force_sync:
        _recommendation_sync_external_events(force=True)
    source_signature = _recommendation_model_source_signature()
    try:
        source_payload = json.loads(source_signature)
    except Exception:
        source_payload = {}
    if (
        not force_sync
        and int(source_payload.get("event_count") or 0) <= 0
        and RECOMMENDATION_SYNC_DATABASE_DSN
    ):
        _recommendation_sync_external_events(force=True)
        source_signature = _recommendation_model_source_signature()
    now = time.time()
    with recommendation_model_lock:
        cached_artifact = recommendation_model_cache.get("artifact")
        cached_signature = recommendation_model_cache.get("source_signature") or ""
        cached_expires_at = float(recommendation_model_cache.get("expires_at") or 0)
        if (
            not force_refresh
            and cached_artifact is not None
            and cached_signature == source_signature
            and cached_expires_at > now
        ):
            return cached_artifact

    connection = _recommendation_store_connection()
    try:
        row = connection.execute(
            """
            SELECT source_signature, artifact_json
            FROM recommendation_models
            WHERE id = ?
            """,
            ["global"],
        ).fetchone()
    finally:
        connection.close()

    if (
        not force_refresh
        and row is not None
        and (row["source_signature"] or "") == source_signature
    ):
        try:
            artifact = json.loads(row["artifact_json"] or "{}")
        except Exception:
            artifact = None
        if isinstance(artifact, dict):
            with recommendation_model_lock:
                recommendation_model_cache["artifact"] = artifact
                recommendation_model_cache["source_signature"] = source_signature
                recommendation_model_cache["expires_at"] = (
                    now + RECOMMENDATION_MODEL_CACHE_TTL_SECONDS
                )
            return artifact

    artifact = _recommendation_train_collaborative_model(source_signature)
    _recommendation_store_collaborative_model(artifact)
    with recommendation_model_lock:
        recommendation_model_cache["artifact"] = artifact
        recommendation_model_cache["source_signature"] = source_signature
        recommendation_model_cache["expires_at"] = (
            now + RECOMMENDATION_MODEL_CACHE_TTL_SECONDS
        )
    return artifact


def _recommendation_build_collaborative_profile(profile, *, force_refresh: bool = False):
    model = _recommendation_get_collaborative_model(
        force_refresh=force_refresh,
        force_sync=force_refresh,
    )
    if not isinstance(model, dict) or not model.get("ready"):
        return {
            "model_ready": False,
            "reason": (model or {}).get("reason") if isinstance(model, dict) else "unavailable",
        }

    item_factors = model.get("item_factors") or {}
    item_neighbors = model.get("item_neighbors") or {}
    user_factors = model.get("user_factors") or {}
    track_artists = model.get("track_artists") or {}
    item_popularity = model.get("item_popularity") or {}
    query_track_scores = model.get("query_track_scores") or {}
    query_artist_scores = model.get("query_artist_scores") or {}
    user_scope_id = _assistant_safe_scope_id(profile.get("user_scope_id") or "guest")
    seed_track_ids = _recommendation_unique_track_ids(
        [
            *(profile.get("recent_track_ids") or []),
            *(profile.get("top_track_ids") or []),
            *(profile.get("library_track_ids") or []),
        ],
        16,
    )
    session_vector = _vector_weighted_average(
        [
            (item_factors.get(track_id) or [], max(1.7 - (index * 0.12), 0.45))
            for index, track_id in enumerate(seed_track_ids[:10])
            if item_factors.get(track_id)
        ]
    )
    stored_user_vector = user_factors.get(user_scope_id) or []
    user_vector = _vector_weighted_average(
        [
            (stored_user_vector, 1.4),
            (session_vector, 1.25),
        ]
    ) or stored_user_vector or session_vector

    known_track_ids = {
        item
        for item in (
            list(profile.get("recent_track_ids") or [])
            + list(profile.get("top_track_ids") or [])
            + list(profile.get("library_track_ids") or [])
            + list(profile.get("offline_track_ids") or [])
            + list((model.get("user_positive_tracks") or {}).get(user_scope_id) or [])
        )
        if item
    }

    candidate_scores = {}
    for index, track_id in enumerate(seed_track_ids[:8]):
        decay = max(1.7 - (index * 0.14), 0.45)
        for neighbor in (item_neighbors.get(track_id) or [])[:RECOMMENDATION_MODEL_NEIGHBOR_LIMIT]:
            candidate_id = _recommendation_trim_text(neighbor.get("track_id"))
            if not candidate_id or candidate_id in known_track_ids:
                continue
            candidate_scores[candidate_id] = candidate_scores.get(candidate_id, 0.0) + (
                float(neighbor.get("score") or 0.0) * decay
            )

    if user_vector:
        for track_id, item_vector in item_factors.items():
            if track_id in known_track_ids:
                continue
            latent_score = max(0.0, _assistant_cosine_similarity(user_vector, item_vector))
            if latent_score <= 0.03 and track_id not in candidate_scores:
                continue
            candidate_scores[track_id] = candidate_scores.get(track_id, 0.0) + (
                latent_score * 4.2
            ) + min(float(item_popularity.get(track_id) or 0.0) * 0.08, 0.8)

    ordered_candidates = sorted(
        candidate_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    artist_scores = {}
    for track_id, score in ordered_candidates[:72]:
        artist_key = _normalize_text(track_artists.get(track_id) or "")
        if not artist_key:
            continue
        artist_scores[artist_key] = artist_scores.get(artist_key, 0.0) + float(score)

    blended_queries = _recommendation_unique_strings(
        [
            *(profile.get("recent_queries") or []),
            *(profile.get("taste_queries") or []),
        ],
        8,
    )
    for query in blended_queries:
        normalized_query = _normalize_text(query)
        for track_id, score in (query_track_scores.get(normalized_query) or {}).items():
            if track_id in known_track_ids:
                continue
            candidate_scores[track_id] = candidate_scores.get(track_id, 0.0) + float(score)
        for artist_key, score in (query_artist_scores.get(normalized_query) or {}).items():
            if not artist_key:
                continue
            artist_scores[artist_key] = artist_scores.get(artist_key, 0.0) + float(score)

    ordered_candidates = sorted(
        candidate_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    return {
        "model_ready": True,
        "model_type": model.get("model_type"),
        "model_id": model.get("model_id"),
        "source_signature": model.get("source_signature"),
        "model": model,
        "user_vector": user_vector,
        "neighbor_scores": {
            track_id: round(float(score), 4)
            for track_id, score in ordered_candidates[:128]
        },
        "candidate_track_ids": [track_id for track_id, _score in ordered_candidates[:72]],
        "artist_scores": {
            artist_key: round(float(score), 4)
            for artist_key, score in sorted(
                artist_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:36]
        },
    }


def _recommendation_collaborative_neighbor_tracks(track_id: str, limit: int = 12):
    normalized_track_id = _recommendation_trim_text(track_id)
    if not normalized_track_id:
        return []
    model = _recommendation_get_collaborative_model()
    if not isinstance(model, dict) or not model.get("ready"):
        return []
    neighbor_ids = [
        _recommendation_trim_text(item.get("track_id"))
        for item in (model.get("item_neighbors") or {}).get(normalized_track_id, [])[:limit]
        if _recommendation_trim_text(item.get("track_id"))
    ]
    return _recommendation_fetch_tracks_for_ids(neighbor_ids, limit=limit)


def _recommendation_collaborative_track_scores(track, profile):
    if not isinstance(track, dict):
        return {
            "latent": 0.0,
            "neighbor": 0.0,
            "artist": 0.0,
        }
    collaborative = profile.get("collaborative") or {}
    track_id = _recommendation_trim_text(track.get("id"))
    artist_key = _normalize_text(
        track.get("channel") or track.get("author") or track.get("artist") or ""
    )
    neighbor_score = float((collaborative.get("neighbor_scores") or {}).get(track_id) or 0.0)
    artist_score = float((collaborative.get("artist_scores") or {}).get(artist_key) or 0.0)
    latent_score = 0.0
    user_vector = collaborative.get("user_vector") or []
    if track_id and user_vector:
        model = collaborative.get("model") or {}
        if isinstance(model, dict) and model.get("ready"):
            item_vector = (model.get("item_factors") or {}).get(track_id) or []
            latent_score = max(0.0, _assistant_cosine_similarity(user_vector, item_vector))
    return {
        "latent": latent_score,
        "neighbor": neighbor_score,
        "artist": artist_score,
    }


def _recommendation_profile_key(req: SearchRequest):
    recent_snapshot_tracks = _recommendation_unique_snapshot_tracks(
        [*(req.last_played_tracks or []), *(req.recent_track_snapshots or [])],
        16,
    )
    top_snapshot_tracks = _recommendation_unique_snapshot_tracks(
        req.top_track_snapshots,
        16,
    )
    payload = {
        "user_scope_id": _recommendation_trim_text(req.user_scope_id or "guest"),
        "seed_id": _recommendation_trim_text(req.seed_id),
        "seed_ids": _recommendation_unique_track_ids(req.seed_ids, 16),
        "recent_track_ids": _recommendation_unique_track_ids(req.recent_track_ids, 16),
        "top_track_ids": _recommendation_unique_track_ids(req.top_track_ids, 16),
        "recent_track_snapshot_ids": [track.get("id") for track in recent_snapshot_tracks],
        "top_track_snapshot_ids": [track.get("id") for track in top_snapshot_tracks],
        "artist_hints": _recommendation_unique_strings(req.artist_hints, 12),
        "album_hints": _recommendation_unique_strings(req.album_hints, 12),
        "taste_queries": _recommendation_unique_strings(req.taste_queries, 12),
        "recent_queries": _recommendation_unique_strings(req.recent_queries, 12),
        "playlist_names": _recommendation_unique_strings(req.playlist_names, 12),
        "library_track_ids": _recommendation_unique_track_ids(req.library_track_ids, 24),
        "offline_track_ids": _recommendation_unique_track_ids(req.offline_track_ids, 24),
        "query": _recommendation_trim_text(req.query),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _recommendation_build_profile(req: SearchRequest):
    key = _recommendation_profile_key(req)
    now = time.time()
    with recommendation_profile_lock:
        cached = recommendation_profile_cache.get(key)
        if cached and cached["expires_at"] > now and not req.force_refresh:
            return cached["profile"]

    recent_track_snapshots = _recommendation_unique_snapshot_tracks(
        [*(req.last_played_tracks or []), *(req.recent_track_snapshots or [])],
        16,
    )
    top_track_snapshots = _recommendation_unique_snapshot_tracks(
        req.top_track_snapshots,
        16,
    )
    last_played_tracks = _recommendation_unique_snapshot_tracks(
        req.last_played_tracks,
        12,
    )

    recent_track_ids = _recommendation_unique_track_ids(
        [
            req.seed_id,
            *(track.get("id") for track in recent_track_snapshots),
            *(req.seed_ids or []),
            *(req.recent_track_ids or []),
        ],
        16,
    )
    top_track_ids = _recommendation_unique_track_ids(
        [
            *(track.get("id") for track in top_track_snapshots),
            *(req.top_track_ids or []),
            *(req.seed_ids or []),
            req.seed_id,
        ],
        16,
    )
    recent_queries = _recommendation_unique_strings(
        [*(req.recent_queries or []), req.query, *(req.taste_queries or [])],
        12,
    )
    snapshot_artist_hints = [
        track.get("channel")
        for track in [*top_track_snapshots, *recent_track_snapshots, *last_played_tracks]
        if track.get("channel")
    ]
    snapshot_album_hints = [
        track.get("album")
        for track in [*top_track_snapshots, *recent_track_snapshots, *last_played_tracks]
        if track.get("album")
    ]
    artist_hints = _recommendation_unique_strings(
        [*(req.artist_hints or []), *snapshot_artist_hints],
        12,
    )
    album_hints = _recommendation_unique_strings(
        [*(req.album_hints or []), *snapshot_album_hints],
        12,
    )
    playlist_names = _recommendation_unique_strings(req.playlist_names, 12)
    library_track_ids = _recommendation_unique_track_ids(req.library_track_ids, 28)
    offline_track_ids = _recommendation_unique_track_ids(req.offline_track_ids, 28)

    artist_weights = {}
    for index, track in enumerate(top_track_snapshots):
        artist_name = _recommendation_trim_text(track.get("channel"))
        if not artist_name:
            continue
        artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
            1.8 - (index * 0.14),
            0.55,
        )
    for index, track in enumerate(recent_track_snapshots):
        artist_name = _recommendation_trim_text(track.get("channel"))
        if not artist_name:
            continue
        artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
            1.25 - (index * 0.1),
            0.35,
        )
    ranked_artist_names = [
        item[0]
        for item in sorted(
            artist_weights.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]

    top_artist_names = (ranked_artist_names + artist_hints)[:6]
    listened_artist_names = _recommendation_unique_strings(
        [*ranked_artist_names, *artist_hints],
        12,
    )
    top_album_names = album_hints[:6]
    repeat_intensity = min(1.0, len(top_track_ids) / 10.0)
    novelty_tolerance = max(
        0.25,
        min(0.75, 0.38 + (len(recent_queries) * 0.04) + (len(artist_hints) * 0.02)),
    )

    profile = {
        "profile_key": key,
        "user_scope_id": _recommendation_trim_text(req.user_scope_id or "guest") or "guest",
        "recent_track_ids": recent_track_ids,
        "top_track_ids": top_track_ids,
        "recent_track_snapshots": recent_track_snapshots,
        "top_track_snapshots": top_track_snapshots,
        "last_played_tracks": last_played_tracks,
        "recent_queries": recent_queries,
        "taste_queries": _recommendation_unique_strings(req.taste_queries, 12),
        "artist_hints": artist_hints,
        "album_hints": album_hints,
        "playlist_names": playlist_names,
        "library_track_ids": library_track_ids,
        "offline_track_ids": offline_track_ids,
        "top_artists": top_artist_names,
        "listened_artists": listened_artist_names,
        "top_albums": top_album_names,
        "repeat_intensity": repeat_intensity,
        "novelty_tolerance": novelty_tolerance,
        "experiment_variant": _recommendation_assignment_for_user(
            _recommendation_trim_text(req.user_scope_id or "guest") or "guest"
        ),
    }
    profile["vectors"] = _recommendation_build_profile_vectors(profile)
    profile["collaborative"] = _recommendation_build_collaborative_profile(
        profile,
        force_refresh=req.force_refresh,
    )
    with recommendation_profile_lock:
        recommendation_profile_cache[key] = {
            "profile": profile,
            "expires_at": now + RECOMMENDATION_PROFILE_CACHE_TTL_SECONDS,
        }
    return profile


def _recommendation_fetch_tracks_for_ids(track_ids, limit: int = 12):
    ordered_ids = []
    seen = set()
    for raw_id in track_ids or []:
        normalized_id = _recommendation_trim_text(raw_id)
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)
        ordered_ids.append(normalized_id)
        if len(ordered_ids) >= max(limit * 2, limit):
            break

    if not ordered_ids:
        return []

    futures = {}
    resolved = {}
    for track_id in ordered_ids:
        cached = _recommendation_cached_track(track_id)
        if cached is not None:
            resolved[track_id] = cached
            continue
        futures[track_id] = recommendation_executor.submit(
            _recommendation_fetch_track_for_id,
            track_id,
        )

    tracks = []
    for track_id in ordered_ids:
        track = resolved.get(track_id)
        if track is None:
            future = futures.get(track_id)
            if future is not None:
                try:
                    track = future.result(timeout=8)
                except Exception:
                    track = None
        if track is None:
            continue
        tracks.append(track)
        if len(tracks) >= limit:
            break
    return tracks


def _recommendation_build_profile_vectors(profile):
    short_term_tracks = [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("recent_track_snapshots") or [])[:8],
    ]
    long_term_tracks = [
        *(profile.get("top_track_snapshots") or [])[:10],
    ]

    track_items = []
    for index, track in enumerate(short_term_tracks):
        track_items.append(("short", index, track))
    for index, track in enumerate(long_term_tracks):
        track_items.append(("long", index, track))

    track_embeddings = _recommendation_track_embeddings(
        [track for _, _, track in track_items]
    )

    query_entries = []
    for index, query in enumerate(profile.get("recent_queries") or []):
        text = (query or "").strip()
        if not text:
            continue
        key = _recommendation_text_embedding_key("query", text)
        query_entries.append((key, text, max(1.6 - (index * 0.12), 0.5)))
    for index, query in enumerate(profile.get("taste_queries") or []):
        text = (query or "").strip()
        if not text:
            continue
        key = _recommendation_text_embedding_key("taste", text)
        query_entries.append((key, text, max(1.45 - (index * 0.1), 0.45)))
    for index, artist_hint in enumerate(profile.get("artist_hints") or []):
        artist_value = (artist_hint or "").strip()
        if not artist_value:
            continue
        text = f"artist {artist_value}"
        key = _recommendation_text_embedding_key("artist_hint", text)
        query_entries.append((key, text, max(1.3 - (index * 0.08), 0.4)))
    for index, album_hint in enumerate(profile.get("album_hints") or []):
        album_value = (album_hint or "").strip()
        if not album_value:
            continue
        text = f"album {album_value}"
        key = _recommendation_text_embedding_key("album_hint", text)
        query_entries.append((key, text, max(1.15 - (index * 0.08), 0.35)))
    for index, playlist_name in enumerate(profile.get("playlist_names") or []):
        playlist_value = (playlist_name or "").strip()
        if not playlist_value:
            continue
        text = f"playlist {playlist_value}"
        key = _recommendation_text_embedding_key("playlist", text)
        query_entries.append((key, text, max(0.9 - (index * 0.06), 0.25)))

    text_embeddings = _recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in query_entries],
    )

    short_term_vectors = []
    long_term_vectors = []
    for scope, index, track in track_items:
        key = _recommendation_track_embedding_key(track)
        vector = track_embeddings.get(key) or []
        if not vector:
            continue
        if scope == "short":
            short_term_vectors.append((vector, max(2.2 - (index * 0.18), 0.7)))
        else:
            long_term_vectors.append((vector, max(1.7 - (index * 0.1), 0.55)))

    query_vectors = []
    for key, _text, weight in query_entries:
        vector = text_embeddings.get(key) or []
        if not vector:
            continue
        query_vectors.append((vector, weight))

    artist_entries = []
    for index, artist_name in enumerate(profile.get("listened_artists") or []):
        artist_value = (artist_name or "").strip()
        if not artist_value:
            continue
        text = f"artist {artist_value}"
        key = _recommendation_text_embedding_key("profile_artist", text)
        artist_entries.append((key, text, max(1.8 - (index * 0.12), 0.55)))
    artist_embeddings = _recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in artist_entries],
    )
    artist_vectors = []
    for key, _text, weight in artist_entries:
        vector = artist_embeddings.get(key) or []
        if not vector:
            continue
        artist_vectors.append((vector, weight))

    short_term_vector = _vector_weighted_average(short_term_vectors)
    long_term_vector = _vector_weighted_average(long_term_vectors)
    query_vector = _vector_weighted_average(query_vectors)
    artist_vector = _vector_weighted_average(artist_vectors)
    anchor_track = (
        (profile.get("last_played_tracks") or [None])[0]
        or (profile.get("recent_track_snapshots") or [None])[0]
        or (profile.get("top_track_snapshots") or [None])[0]
    )
    anchor_vector = []
    anchor_key = _recommendation_track_embedding_key(anchor_track)
    if anchor_key:
        anchor_vector = track_embeddings.get(anchor_key) or []
    taste_vector = _vector_weighted_average(
        [
            (short_term_vector, 1.65),
            (long_term_vector, 1.2),
            (query_vector, 0.9),
            (artist_vector, 1.15),
        ]
    )

    return {
        "short_term_vector": short_term_vector,
        "long_term_vector": long_term_vector,
        "query_vector": query_vector,
        "artist_vector": artist_vector,
        "anchor_vector": anchor_vector,
        "taste_vector": taste_vector,
    }


def _recommendation_candidate(track, generator_name: str, generator_score: float, reason: str):
    normalized = normalize_recommendation_track(track)
    if normalized is None:
        return None
    enriched = dict(normalized)
    enriched["generator_name"] = generator_name
    enriched["recommendation_reason"] = reason
    enriched["generator_score"] = float(generator_score)
    return {
        "track": enriched,
        "generator_name": generator_name,
        "generator_score": float(generator_score),
        "reason": reason,
    }


def _recommendation_vector_similarities(candidate_vector, profile):
    vectors = profile.get("vectors") or {}
    if not candidate_vector:
        return {
            "taste": 0.0,
            "short": 0.0,
            "long": 0.0,
            "query": 0.0,
            "artist": 0.0,
            "anchor": 0.0,
        }
    return {
        "taste": _assistant_cosine_similarity(candidate_vector, vectors.get("taste_vector") or []),
        "short": _assistant_cosine_similarity(candidate_vector, vectors.get("short_term_vector") or []),
        "long": _assistant_cosine_similarity(candidate_vector, vectors.get("long_term_vector") or []),
        "query": _assistant_cosine_similarity(candidate_vector, vectors.get("query_vector") or []),
        "artist": _assistant_cosine_similarity(candidate_vector, vectors.get("artist_vector") or []),
        "anchor": _assistant_cosine_similarity(candidate_vector, vectors.get("anchor_vector") or []),
    }


def _recommendation_track_score(candidate, profile, row_kind: str, candidate_vector=None):
    track = candidate["track"]
    track_id = track.get("id") or ""
    artist_text = _normalize_text(track.get("channel"))
    album_text = _normalize_text(track.get("album"))
    title_text = _normalize_text(track.get("title"))
    similarities = _recommendation_vector_similarities(candidate_vector, profile)
    collaborative_scores = _recommendation_collaborative_track_scores(track, profile)
    experiment_variant = _recommendation_trim_text(profile.get("experiment_variant")) or "control"
    collaborative_multiplier = 1.25 if experiment_variant == "collab_heavy" else 0.85
    score = float(candidate.get("generator_score") or 0.0) * 0.35
    score += (similarities["taste"] * 5.8)
    score += (collaborative_scores["latent"] * 6.0 * collaborative_multiplier)
    score += min(collaborative_scores["neighbor"], 5.0) * 1.2 * collaborative_multiplier
    score += min(collaborative_scores["artist"], 6.0) * 0.22 * collaborative_multiplier

    if track_id in profile["offline_track_ids"]:
        score += 1.5
    if track_id in profile["library_track_ids"]:
        score += 0.8
    if track_id in profile["top_track_ids"]:
        score += 2.4
    if track_id in profile["recent_track_ids"]:
        score += 1.0

    for index, artist_hint in enumerate(profile["artist_hints"][:6]):
        normalized_hint = _normalize_text(artist_hint)
        if normalized_hint and normalized_hint in artist_text:
            score += max(1.6 - (index * 0.18), 0.55)

    for index, album_hint in enumerate(profile["album_hints"][:6]):
        normalized_hint = _normalize_text(album_hint)
        if normalized_hint and normalized_hint in album_text:
            score += max(1.2 - (index * 0.16), 0.4)

    for index, query in enumerate(profile["recent_queries"][:4]):
        hits = sum(
            1
            for token in _query_tokens(query)
            if token in title_text or token in artist_text or token in album_text
        )
        if hits:
            score += min(hits, 2) * max(0.45 - (index * 0.06), 0.12)

    if row_kind == "frequently_listened":
        score += (similarities["long"] * 4.8) + (similarities["short"] * 1.6)
        score += 2.1 if track_id in profile["top_track_ids"] else -1.8
    elif row_kind == "continue_listening":
        score += (similarities["anchor"] * 4.8) + (similarities["short"] * 4.2) + (similarities["query"] * 1.6)
        if track_id in profile["recent_track_ids"]:
            score -= 0.6
        score += 0.7
    elif row_kind == "because_you_played":
        score += (similarities["anchor"] * 6.2) + (similarities["short"] * 2.4) + (similarities["artist"] * 1.6)
        if track_id in profile["recent_track_ids"]:
            score -= 1.4
        score += 0.4
    elif row_kind == "rediscover":
        score += (similarities["long"] * 5.6) + (similarities["artist"] * 1.8) - (similarities["short"] * 2.8)
        if track_id in profile["top_track_ids"]:
            score += 1.0
        if track_id in profile["recent_track_ids"]:
            score -= 2.4
    elif row_kind == "deep_cuts":
        score += (similarities["long"] * 4.8) + (similarities["artist"] * 3.2) - (similarities["short"] * 1.4)
        if track_id in profile["top_track_ids"]:
            score -= 1.8
        score += 0.9
    elif row_kind == "offline_ready":
        score += (similarities["taste"] * 1.8) + (similarities["long"] * 1.4)
        if track_id in profile["offline_track_ids"]:
            score += 3.0
        elif track_id in profile["library_track_ids"]:
            score += 1.0
        else:
            score -= 2.5
    elif row_kind == "search_rebound":
        score += (similarities["query"] * 6.0) + (similarities["taste"] * 1.8) + 0.8
    elif row_kind == "quiet_picks":
        score += (similarities["query"] * 5.2) + (similarities["taste"] * 1.9) + (similarities["short"] * 0.8)
        if any(
            keyword in title_text or keyword in album_text
            for keyword in ["quiet", "acoustic", "night", "calm", "soft", "ambient"]
        ):
            score += 1.2
    elif row_kind == "trending_for_you":
        score += (similarities["taste"] * 2.5) + (similarities["query"] * 1.0) + 0.4

    candidate["_ml_similarities"] = {
        name: round(value, 4) for name, value in similarities.items()
    }
    candidate["_ml_similarities"].update(
        {
            "collab_latent": round(collaborative_scores["latent"], 4),
            "collab_neighbor": round(collaborative_scores["neighbor"], 4),
            "collab_artist": round(collaborative_scores["artist"], 4),
        }
    )

    return score


def _recommendation_quality_floor(row_kind: str) -> float:
    if row_kind in {"continue_listening", "because_you_played", "quiet_picks", "trending_for_you", "listeners_like_you"}:
        return 1.0
    if row_kind in {"rediscover", "deep_cuts", "search_rebound"}:
        return 1.15
    if row_kind in {"offline_ready", "frequently_listened"}:
        return 1.35
    return 1.5


def _recommendation_min_items(row_kind: str) -> int:
    if row_kind in {"continue_listening", "because_you_played", "quiet_picks", "trending_for_you", "listeners_like_you"}:
        return 2
    if row_kind in {"rediscover", "deep_cuts", "search_rebound"}:
        return 2
    return 3


def _recommendation_finalize_row_items(
    row_kind: str,
    title: str,
    candidates,
    profile,
    used_track_ids,
    *,
    max_items: int = 18,
):
    if not candidates:
        return None

    candidate_embeddings = _recommendation_track_embeddings(
        [
            candidate.get("track")
            for candidate in candidates
            if isinstance(candidate, dict)
        ]
    )
    ranked = []
    for candidate in candidates:
        candidate_key = _recommendation_track_embedding_key(candidate.get("track"))
        candidate_vector = candidate_embeddings.get(candidate_key) or []
        candidate_score = _recommendation_track_score(
            candidate,
            profile,
            row_kind,
            candidate_vector,
        )
        ranked.append((candidate_score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = []
    artist_counts = {}
    quality_floor = _recommendation_quality_floor(row_kind)
    max_same_artist = 3 if row_kind in {"frequently_listened", "offline_ready", "continue_listening"} else 2
    min_items = _recommendation_min_items(row_kind)

    for candidate_score, candidate in ranked:
        if candidate_score < quality_floor:
            continue
        track = dict(candidate["track"])
        track_id = track.get("id")
        if not track_id or track_id in used_track_ids:
            continue
        artist_key = _normalize_text(track.get("channel"))
        if artist_key:
            current_count = artist_counts.get(artist_key, 0)
            if current_count >= max_same_artist and len(selected) + 1 < max_items:
                continue
            artist_counts[artist_key] = current_count + 1
        track["generator_score"] = round(candidate_score, 3)
        track["ml_similarities"] = candidate.get("_ml_similarities") or {}
        selected.append(track)
        used_track_ids.add(track_id)
        if len(selected) >= max_items:
            break

    if len(selected) < min_items:
        return None

    incomplete_indexes = [
        index
        for index, track in enumerate(selected)
        if _track_metadata_incomplete(track)
    ]
    if incomplete_indexes:
        futures = {
            index: recommendation_executor.submit(
                _recommendation_enrich_track_metadata,
                selected[index],
            )
            for index in incomplete_indexes
        }
        for index, future in futures.items():
            try:
                enriched = future.result(timeout=8)
            except Exception:
                enriched = None
            if enriched is not None:
                selected[index] = enriched

    return {
        "id": row_kind,
        "kind": row_kind,
        "title": title,
        "items": selected,
    }


def _recommendation_candidates_from_tracks(tracks, generator_name: str, base_score: float, reason: str):
    candidates = []
    for track in tracks or []:
        candidate = _recommendation_candidate(track, generator_name, base_score, reason)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _recommendation_quiet_pick_queries(profile, base_query: str, cycle: int):
    base = _recommendation_trim_text(base_query)
    if not base:
        return []
    quiet_modifiers = [
        "acoustic",
        "ambient",
        "instrumental",
        "late night",
        "soft vocals",
        "sleep",
        "focus",
        "piano",
        "lofi",
        "calm evening",
        "rainy night",
        "soft rock",
        "soft pop",
    ]
    modifier = quiet_modifiers[cycle % len(quiet_modifiers)]
    secondary_modifier = quiet_modifiers[(cycle + 5) % len(quiet_modifiers)]
    quiet_queries = _recommendation_unique_strings(
        [
            query
            for query in [
                *(profile.get("recent_queries") or []),
                *(profile.get("taste_queries") or []),
            ]
            if any(
                keyword in _normalize_text(query)
                for keyword in [
                    "quiet",
                    "calm",
                    "soft",
                    "night",
                    "sleep",
                    "focus",
                    "ambient",
                    "chill",
                ]
            )
        ],
        6,
    )
    artist_hints = _recommendation_unique_strings(profile.get("artist_hints") or [], 6)
    album_hints = _recommendation_unique_strings(profile.get("album_hints") or [], 4)
    query_candidates = [
        base,
        f"{base} {modifier}",
        f"{base} {secondary_modifier}",
    ]
    if quiet_queries:
        query_candidates.append(quiet_queries[cycle % len(quiet_queries)])
    if artist_hints:
        artist_hint = artist_hints[cycle % len(artist_hints)]
        query_candidates.append(f"{artist_hint} {modifier}")
        query_candidates.append(f"{artist_hint} calm songs")
    if album_hints:
        album_hint = album_hints[cycle % len(album_hints)]
        query_candidates.append(f"{album_hint} {modifier}")
    return _recommendation_unique_strings(query_candidates, 6)


def _recommendation_should_extend_row(row, offset: int, page_size: int) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("kind") != "quiet_picks" or not row.get("can_extend"):
        return False
    total_items = len(row.get("items") or [])
    if total_items <= 0:
        return False
    requested_end = max(int(offset or 0), 0) + max(1, min(page_size, 12))
    return requested_end >= max(total_items - page_size, 0)


def _recommendation_extend_quiet_picks_row(row, profile, *, page_size: int = 10):
    if not isinstance(row, dict):
        return row
    extended_row = dict(row)
    if extended_row.get("kind") != "quiet_picks":
        return extended_row
    base_query = _recommendation_trim_text(extended_row.get("base_query"))
    if not base_query:
        extended_row["can_extend"] = False
        return extended_row

    existing_items = list(extended_row.get("items") or [])
    existing_ids = {
        _recommendation_trim_text(track.get("id"))
        for track in existing_items
        if isinstance(track, dict)
    }
    existing_ids.discard("")

    extension_cycle = max(int(extended_row.get("extension_cycle") or 0), 0)
    target_new_items = max(page_size * 2, 12)
    collected_new_items = []
    max_cycles = extension_cycle + 4
    current_cycle = extension_cycle

    while len(collected_new_items) < target_new_items and current_cycle < max_cycles:
        quiet_queries = _recommendation_quiet_pick_queries(profile, base_query, current_cycle)
        candidate_pool = []
        for query_index, quiet_query in enumerate(quiet_queries):
            try:
                tracks = _assistant_tool_search_tracks(quiet_query, 24)
            except Exception:
                tracks = []
            if not tracks:
                continue
            candidate_pool.extend(
                _recommendation_candidates_from_tracks(
                    tracks,
                    "quiet_picks_extend",
                    max(3.1 - (query_index * 0.12), 2.35),
                    f"Extended around {quiet_query}.",
                )
            )
        if candidate_pool:
            finalized = _recommendation_finalize_row_items(
                "quiet_picks",
                extended_row.get("title") or "Quiet picks",
                candidate_pool,
                profile,
                set(existing_ids),
                max_items=max(page_size + 4, 12),
            )
            for track in (finalized or {}).get("items") or []:
                track_id = _recommendation_trim_text(track.get("id"))
                if not track_id or track_id in existing_ids:
                    continue
                existing_ids.add(track_id)
                collected_new_items.append(track)
                if len(collected_new_items) >= target_new_items:
                    break
        current_cycle += 1

    extended_row["extension_cycle"] = current_cycle
    if collected_new_items:
        extended_row["items"] = existing_items + collected_new_items
        extended_row["can_extend"] = True
    else:
        extended_row["can_extend"] = False
    return extended_row


def _recommendation_anchor_query(track: Optional[Dict[str, Any]], *, include_album: bool = False) -> str:
    if not isinstance(track, dict):
        return ""
    parts = [
        _recommendation_trim_text(track.get("title")),
        _recommendation_trim_text(track.get("channel") or track.get("author") or track.get("artist")),
    ]
    if include_album:
        parts.append(_recommendation_trim_text(track.get("album")))
    return " ".join([part for part in parts if part]).strip()


def _recommendation_album_candidates_for_track(track: Optional[Dict[str, Any]], *, limit: int = 2):
    if not isinstance(track, dict):
        return []

    albums = []
    seen = set()

    def add_album(raw_album: Optional[Dict[str, Any]]):
        if not isinstance(raw_album, dict):
            return
        album_id = _recommendation_trim_text(raw_album.get("id"))
        title = _recommendation_trim_text(raw_album.get("title"))
        artist = _recommendation_trim_text(raw_album.get("artist"))
        key = album_id or f"{_normalize_text(title)}|{_normalize_text(artist)}"
        if not key or key in seen:
            return
        seen.add(key)
        albums.append(raw_album)

    album_id = _recommendation_trim_text(track.get("album_id"))
    album_title = _recommendation_trim_text(track.get("album"))
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

    search_query = _recommendation_anchor_query(track, include_album=True)
    if search_query:
        for album in _assistant_tool_search_albums(search_query, max(limit * 2, 4)):
            add_album(album)
            if len(albums) >= limit:
                break

    return albums[:limit]


def _recommendation_candidate_sources_for_track(track: Optional[Dict[str, Any]]):
    if not isinstance(track, dict):
        return []

    track_id = _recommendation_trim_text(track.get("id"))
    anchor_query = _recommendation_anchor_query(track)
    artist_name = _recommendation_trim_text(
        track.get("channel") or track.get("author") or track.get("artist")
    )
    futures = {}

    if track_id:
        futures["similar"] = recommendation_executor.submit(
            _assistant_tool_get_similar_tracks,
            track.get("id"),
            12,
        )
        futures["collaborative"] = recommendation_executor.submit(
            _recommendation_collaborative_neighbor_tracks,
            track_id,
            10,
        )

    if artist_name:
        futures["artist_seed"] = recommendation_executor.submit(
            _search_artist_seed_tracks,
            artist_name,
            8,
        )

    if anchor_query:
        futures["search_context"] = recommendation_executor.submit(
            _assistant_tool_search_tracks,
            anchor_query,
            10,
        )

    def fetch_album_context():
        album_tracks = []
        for album in _recommendation_album_candidates_for_track(track, limit=2):
            album_id = _recommendation_trim_text(album.get("id"))
            if not album_id:
                continue
            album_details = _assistant_tool_get_album_details(album_id)
            album_tracks.extend(album_details.get("tracks") or [])
        return album_tracks

    futures["album_context"] = recommendation_executor.submit(fetch_album_context)

    source_results = {
        "similar": [],
        "collaborative": [],
        "artist_seed": [],
        "search_context": [],
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
        ("search_context", source_results["search_context"], 3.2),
    ]


def _recommendation_continue_listening_row(profile):
    anchor_tracks = []
    seen_anchor_ids = set()
    for track in [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("recent_track_snapshots") or []),
    ]:
        track_id = _recommendation_trim_text(track.get("id"))
        if not track_id or track_id in seen_anchor_ids:
            continue
        seen_anchor_ids.add(track_id)
        anchor_tracks.append(track)
        if len(anchor_tracks) >= 3:
            break

    if not anchor_tracks:
        return None

    candidates = []
    for index, anchor_track in enumerate(anchor_tracks):
        anchor_title = anchor_track.get("title") or "that song"
        for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
            candidates.extend(
                _recommendation_candidates_from_tracks(
                    source_tracks,
                    f"continue_listening:{source_name}",
                    base_score - (index * 0.22),
                    f"Continues from {anchor_title}.",
                )
            )

    if not candidates:
        return None

    return {
        "title": "Continue the vibe",
        "kind": "continue_listening",
        "candidates": candidates,
    }


def _recommendation_because_you_played_row(profile):
    anchor_track = (
        (profile.get("last_played_tracks") or [None])[0]
        or (profile.get("recent_track_snapshots") or [None])[0]
    )
    anchor_ids = profile["recent_track_ids"][:2] or profile["top_track_ids"][:2]
    if not anchor_track and not anchor_ids:
        return None
    anchor_id = (anchor_track or {}).get("id") or anchor_ids[0]
    if anchor_track:
        anchor_title = anchor_track.get("title") or "that song"
        anchor_artist = anchor_track.get("channel") or ""
    else:
        fetched_anchor = _recommendation_fetch_tracks_for_ids([anchor_id], limit=1)
        anchor_title = fetched_anchor[0]["title"] if fetched_anchor else "that song"
        anchor_artist = fetched_anchor[0]["channel"] if fetched_anchor else ""
    title = (
        f"Because you played {anchor_artist}"
        if anchor_artist
        else f"Because you played {anchor_title}"
    )
    candidates = []
    anchor_track_payload = anchor_track or {
        "id": anchor_id,
        "title": anchor_title,
        "channel": anchor_artist,
        "author": anchor_artist,
        "artist": anchor_artist,
    }
    for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track_payload):
        candidates.extend(
            _recommendation_candidates_from_tracks(
                source_tracks,
                f"because_you_played:{source_name}",
                base_score,
                f"Shaped from {anchor_title}.",
            )
        )
    return {
        "title": title,
        "kind": "because_you_played",
        "candidates": candidates,
    }


def _recommendation_listeners_like_you_row(profile):
    collaborative = profile.get("collaborative") or {}
    candidate_track_ids = collaborative.get("candidate_track_ids") or []
    if not candidate_track_ids:
        return None
    tracks = _recommendation_fetch_tracks_for_ids(candidate_track_ids, limit=18)
    if not tracks:
        return None
    return {
        "title": "Listeners like you also played",
        "kind": "listeners_like_you",
        "candidates": _recommendation_candidates_from_tracks(
            tracks,
            "collaborative",
            5.0,
            "Learned from collaborative listening patterns and your recent taste.",
        ),
    }


def _recommendation_frequently_listened_row(profile):
    tracks = [dict(track) for track in (profile.get("top_track_snapshots") or [])]
    seen_ids = {
        track.get("id")
        for track in tracks
        if track.get("id")
    }
    missing_ids = [
        track_id
        for track_id in profile["top_track_ids"]
        if track_id not in seen_ids
    ]
    if len(tracks) < 18 and missing_ids:
        tracks.extend(
            _recommendation_fetch_tracks_for_ids(
                missing_ids,
                limit=max(0, 18 - len(tracks)),
            )
        )
    if not tracks:
        return None
    return {
        "title": "Frequently listened",
        "kind": "frequently_listened",
        "candidates": _recommendation_candidates_from_tracks(
            tracks,
            "frequently_listened",
            5.2,
            "You keep coming back to this one.",
        ),
    }


def _recommendation_rediscover_row(profile):
    older_ids = [
        track_id
        for track_id in profile["top_track_ids"]
        if track_id not in profile["recent_track_ids"]
    ]
    tracks = _recommendation_fetch_tracks_for_ids(older_ids, limit=14)
    candidates = _recommendation_candidates_from_tracks(
        tracks,
        "rediscovery",
        4.1,
        "A favorite worth bringing back.",
    )

    if len(candidates) < 6:
        seen_ids = {
            _recommendation_trim_text((candidate.get("track") or {}).get("id"))
            for candidate in candidates
        }
        for track in (profile.get("top_track_snapshots") or []) + (profile.get("last_played_tracks") or []):
            track_id = _recommendation_trim_text(track.get("id"))
            if not track_id or track_id in profile["recent_track_ids"]:
                continue
            if track_id not in seen_ids:
                candidates.extend(
                    _recommendation_candidates_from_tracks(
                        [track],
                        "rediscovery:history",
                        3.7,
                        "A favorite worth bringing back.",
                    )
                )
                seen_ids.add(track_id)
            for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(track):
                filtered_tracks = [
                    source_track
                    for source_track in source_tracks
                    if _recommendation_trim_text(source_track.get("id")) not in profile["recent_track_ids"]
                ]
                candidates.extend(
                    _recommendation_candidates_from_tracks(
                        filtered_tracks,
                        f"rediscovery:{source_name}",
                        max(base_score - 0.35, 2.6),
                        "A favorite worth bringing back.",
                    )
                )
            if len(candidates) >= 12:
                break

    if not candidates and profile["top_track_ids"]:
        fallback_tracks = _recommendation_fetch_tracks_for_ids(profile["top_track_ids"], limit=10)
        candidates = _recommendation_candidates_from_tracks(
            fallback_tracks,
            "rediscovery:fallback",
            3.4,
            "A favorite worth bringing back.",
        )
    if not candidates:
        return None
    return {
        "title": "Rediscover these",
        "kind": "rediscover",
        "candidates": candidates,
    }


def _recommendation_deep_cuts_row(profile):
    candidates = []
    for artist_hint in profile["top_artists"][:3]:
        artists = _assistant_tool_search_artists(artist_hint, 2)
        if not artists:
            continue
        artist_id = artists[0].get("id")
        if not artist_id:
            continue
        try:
            artist_payload = _build_artist_details_payload(artist_id)
        except Exception:
            artist_payload = {}
        album_ids = [
            album.get("id")
            for album in artist_payload.get("albums", [])[1:5]
            if album.get("id")
        ]
        deep_cut_tracks = []
        for album_id in album_ids:
            details = _assistant_tool_get_album_details(album_id)
            deep_cut_tracks.extend(details.get("tracks") or [])
        if not deep_cut_tracks:
            deep_cut_tracks = _assistant_tool_search_tracks(f"{artist_hint} album tracks", 14)
        if len(deep_cut_tracks) < 6:
            deep_cut_tracks.extend(_search_artist_seed_tracks(artist_hint, 8))
        candidates.extend(
            _recommendation_candidates_from_tracks(
                deep_cut_tracks,
                "deep_cuts",
                3.6,
                f"Pulled from deeper {artist_hint} territory.",
            )
        )
    if not candidates:
        return None
    return {
        "title": "Deep cuts for you",
        "kind": "deep_cuts",
        "candidates": candidates,
    }


def _recommendation_offline_ready_row(profile):
    track_ids = profile["offline_track_ids"] or profile["library_track_ids"]
    tracks = _recommendation_fetch_tracks_for_ids(track_ids, limit=18)
    if not tracks:
        return None
    return {
        "title": "Ready offline",
        "kind": "offline_ready",
        "candidates": _recommendation_candidates_from_tracks(
            tracks,
            "offline_ready",
            4.8,
            "Ready even when you go offline.",
        ),
    }


def _recommendation_search_rebound_row(profile):
    candidates = []
    for query in profile["recent_queries"][:3]:
        tracks = _assistant_tool_search_tracks(query, 10)
        candidates.extend(
            _recommendation_candidates_from_tracks(
                tracks,
                "search_rebound",
                3.9,
                f"Following up on your search for {query}.",
            )
        )
    if not candidates:
        return None
    return {
        "title": "Search-inspired picks",
        "kind": "search_rebound",
        "candidates": candidates,
    }


def _recommendation_quiet_picks_row(profile):
    quiet_query = next(
        (
            query
            for query in profile["recent_queries"] + profile["taste_queries"]
            if any(keyword in _normalize_text(query) for keyword in ["quiet", "calm", "soft", "night", "sleep", "focus", "ambient", "chill"])
        ),
        None,
    )
    if quiet_query is None:
        return None
    tracks = _assistant_tool_search_tracks(quiet_query, 72)
    if not tracks:
        return None
    return {
        "title": "Quiet picks",
        "kind": "quiet_picks",
        "quiet_query": quiet_query,
        "candidates": _recommendation_candidates_from_tracks(
            tracks,
            "quiet_picks",
            3.4,
            f"Built around {quiet_query}.",
        ),
    }


def _recommendation_recommended_albums_row(profile):
    albums = []
    seen = set()

    def add_album(raw_album, base_score: float):
        if not isinstance(raw_album, dict):
            return
        album_id = _recommendation_trim_text(raw_album.get("id"))
        title = _recommendation_trim_text(raw_album.get("title"))
        artist = _recommendation_trim_text(raw_album.get("artist"))
        key = album_id or f"{_normalize_text(title)}|{_normalize_text(artist)}"
        if not key or key in seen:
            return
        seen.add(key)
        albums.append(
            {
                "id": album_id or None,
                "title": title or "Unknown Album",
                "artist": artist or "Unknown Artist",
                "thumbnail": raw_album.get("thumbnail"),
                "year": raw_album.get("year") or "",
                "track_count": raw_album.get("track_count") or raw_album.get("trackCount") or 0,
                "generator_score": round(base_score, 3),
            }
        )

    for index, album_hint in enumerate(profile.get("top_albums") or []):
        for offset, album in enumerate(_assistant_tool_search_albums(album_hint, 5)):
            add_album(album, 4.2 - (index * 0.28) - (offset * 0.16))
            if len(albums) >= 18:
                break
        if len(albums) >= 18:
            break

    if len(albums) < 12:
        album_queries = []
        for query in (profile.get("recent_queries") or []) + (profile.get("taste_queries") or []):
            normalized_query = _recommendation_trim_text(query)
            if not normalized_query:
                continue
            if normalized_query in album_queries:
                continue
            album_queries.append(normalized_query)
            if len(album_queries) >= 4:
                break
        for index, album_query in enumerate(album_queries):
            for offset, album in enumerate(_assistant_tool_search_albums(album_query, 4)):
                add_album(album, 3.95 - (index * 0.24) - (offset * 0.14))
                if len(albums) >= 18:
                    break
            if len(albums) >= 18:
                break

    for index, artist_hint in enumerate(profile.get("top_artists") or []):
        direct_artists = _assistant_tool_search_artists_direct(artist_hint, 1)
        if direct_artists:
            artist_id = _recommendation_trim_text(direct_artists[0].get("id"))
            if artist_id:
                try:
                    artist_payload = _build_artist_details_payload(artist_id)
                except Exception:
                    artist_payload = {}
                for offset, album in enumerate(artist_payload.get("albums") or []):
                    add_album(album, 4.0 - (index * 0.22) - (offset * 0.18))
                    if len(albums) >= 18:
                        break
        if len(albums) < 12:
            for offset, album in enumerate(_assistant_tool_search_albums(f"{artist_hint} album", 4)):
                add_album(album, 3.6 - (index * 0.18) - (offset * 0.14))
                if len(albums) >= 18:
                    break
        if len(albums) >= 18:
            break

    if len(albums) < 12:
        snapshot_tracks = [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("recent_track_snapshots") or []),
        ]
        for index, track in enumerate(snapshot_tracks):
            album_title = _recommendation_trim_text(track.get("album"))
            if not album_title:
                continue
            add_album(
                {
                    "id": track.get("album_id"),
                    "title": album_title,
                    "artist": track.get("channel"),
                    "thumbnail": track.get("thumbnail"),
                },
                3.2 - (index * 0.12),
            )
            if len(albums) >= 18:
                break

    if len(albums) < 1:
        return None

    album_embeddings = _recommendation_album_embeddings(albums)
    vectors = profile.get("vectors") or {}
    top_album_names = {
        _normalize_text(name)
        for name in (profile.get("top_albums") or [])
        if _normalize_text(name)
    }
    top_artist_names = {
        _normalize_text(name)
        for name in (profile.get("top_artists") or [])
        if _normalize_text(name)
    }
    for album in albums:
        album_key = _recommendation_album_embedding_key(album)
        album_vector = album_embeddings.get(album_key) or []
        similarities = {
            "taste": _assistant_cosine_similarity(
                album_vector,
                vectors.get("taste_vector") or [],
            ),
            "artist": _assistant_cosine_similarity(
                album_vector,
                vectors.get("artist_vector") or [],
            ),
            "query": _assistant_cosine_similarity(
                album_vector,
                vectors.get("query_vector") or [],
            ),
            "long": _assistant_cosine_similarity(
                album_vector,
                vectors.get("long_term_vector") or [],
            ),
        }
        ranking_score = (
            (float(album.get("generator_score") or 0.0) * 0.45)
            + (similarities["taste"] * 4.8)
            + (similarities["artist"] * 3.6)
            + (similarities["query"] * 1.5)
            + (similarities["long"] * 1.4)
        )
        if _normalize_text(album.get("title") or "") in top_album_names:
            ranking_score += 0.8
        if _normalize_text(album.get("artist") or "") in top_artist_names:
            ranking_score += 0.6
        album["generator_score"] = round(ranking_score, 3)
        album["ml_similarities"] = {
            name: round(value, 4)
            for name, value in similarities.items()
        }

    albums.sort(key=lambda item: item.get("generator_score", 0), reverse=True)
    return {
        "title": "Recommended albums",
        "kind": "recommended_albums",
        "item_type": "album",
        "items": albums[:18],
    }


def _recommendation_trending_row(profile):
    candidates = []
    for track, base_score in _fallback_home_candidates(24):
        candidate = _recommendation_candidate(
            track,
            "trending_for_you",
            2.2 + float(base_score),
            "Trending, filtered through your taste.",
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    return {
        "title": "Trending for you",
        "kind": "trending_for_you",
        "candidates": candidates,
    }


def _recommendation_build_rows(profile):
    rows = []
    used_track_ids = set()
    generator_timings = {}
    row_diagnostics = {}

    row_builders = [
        _recommendation_continue_listening_row,
        _recommendation_because_you_played_row,
        _recommendation_listeners_like_you_row,
        _recommendation_frequently_listened_row,
        _recommendation_rediscover_row,
        _recommendation_recommended_albums_row,
        _recommendation_deep_cuts_row,
        _recommendation_offline_ready_row,
        _recommendation_search_rebound_row,
        _recommendation_trending_row,
        _recommendation_quiet_picks_row,
    ]

    for builder in row_builders:
        started_at = time.perf_counter()
        row_seed = builder(profile)
        builder_ms = int((time.perf_counter() - started_at) * 1000)
        generator_timings[builder.__name__] = builder_ms
        row_kind = (row_seed or {}).get("kind") or builder.__name__.replace("_recommendation_", "").replace("_row", "")
        row_diagnostics[row_kind] = {
            "builder": builder.__name__,
            "builder_ms": builder_ms,
            "status": "empty",
        }
        if not row_seed:
            continue
        if row_seed.get("item_type") == "album":
            item_count = len(row_seed.get("items") or [])
            rows.append(
                {
                    "id": row_seed["kind"],
                    "kind": row_seed["kind"],
                    "title": row_seed["title"],
                    "item_type": "album",
                    "items": list(row_seed.get("items") or [])[:18],
                }
            )
            row_diagnostics[row_seed["kind"]] = {
                "builder": builder.__name__,
                "builder_ms": builder_ms,
                "status": "emitted" if item_count > 0 else "empty",
                "item_count": min(item_count, 18),
            }
            continue
        candidate_count = len(row_seed.get("candidates") or [])
        row_diagnostics[row_seed["kind"]] = {
            "builder": builder.__name__,
            "builder_ms": builder_ms,
            "status": "seeded",
            "candidate_count": candidate_count,
        }
        max_items = 72 if row_seed["kind"] == "quiet_picks" else 18
        finalized = _recommendation_finalize_row_items(
            row_seed["kind"],
            row_seed["title"],
            row_seed["candidates"],
            profile,
            used_track_ids,
            max_items=max_items,
        )
        if finalized is not None:
            if row_seed["kind"] == "quiet_picks":
                finalized["base_query"] = _recommendation_trim_text(
                    row_seed.get("quiet_query")
                )
                finalized["extension_cycle"] = 0
                finalized["can_extend"] = True
            rows.append(finalized)
            row_diagnostics[row_seed["kind"]] = {
                "builder": builder.__name__,
                "builder_ms": builder_ms,
                "status": "emitted",
                "candidate_count": candidate_count,
                "item_count": len(finalized.get("items") or []),
            }
        else:
            row_diagnostics[row_seed["kind"]] = {
                "builder": builder.__name__,
                "builder_ms": builder_ms,
                "status": "filtered_out",
                "candidate_count": candidate_count,
            }

    return rows, generator_timings, row_diagnostics


def _recommendation_prune_feed_cache():
    now = time.time()
    with recommendation_feed_lock:
        expired_session_ids = [
            session_id
            for session_id, payload in recommendation_feed_sessions.items()
            if payload.get("expires_at", 0) <= now
        ]
        for session_id in expired_session_ids:
            recommendation_feed_sessions.pop(session_id, None)
        expired_profile_keys = [
            key
            for key, payload in recommendation_feed_index.items()
            if payload.get("expires_at", 0) <= now
        ]
        for key in expired_profile_keys:
            recommendation_feed_index.pop(key, None)


def _recommendation_row_slice(row, offset: int, page_size: int):
    items = list(row.get("items") or [])
    bounded_offset = max(int(offset or 0), 0)
    page_limit = max(1, min(page_size, 12))
    visible = items[bounded_offset: bounded_offset + page_limit]
    next_offset = bounded_offset + len(visible)
    can_extend = row.get("kind") == "quiet_picks" and bool(row.get("can_extend"))
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "item_type": row.get("item_type") or "track",
        "items": visible,
        "next_offset": next_offset,
        "has_more": next_offset < len(items) or can_extend,
    }


def _recommendation_build_session(req: SearchRequest):
    _recommendation_prune_feed_cache()
    started_at = time.perf_counter()
    profile_started_at = time.perf_counter()
    profile = _recommendation_build_profile(req)
    profile_ms = int((time.perf_counter() - profile_started_at) * 1000)
    profile_key = profile["profile_key"]

    if not req.force_refresh:
        with recommendation_feed_lock:
            cached = recommendation_feed_index.get(profile_key)
            if cached and cached.get("expires_at", 0) > time.time():
                session = recommendation_feed_sessions.get(cached["session_id"])
                if session is not None:
                    session["diagnostics"]["cache_hit"] = True
                    return session

    row_started_at = time.perf_counter()
    rows, generator_timings, row_diagnostics = _recommendation_build_rows(profile)
    row_ms = int((time.perf_counter() - row_started_at) * 1000)
    session_id = str(uuid.uuid4())
    now = time.time()
    session = {
        "session_id": session_id,
        "user_scope_id": profile["user_scope_id"],
        "profile_key": profile_key,
        "profile": profile,
        "generated_at": now,
        "expires_at": now + RECOMMENDATION_FEED_SESSION_TTL_SECONDS,
        "rows": rows,
        "diagnostics": {
            "cache_hit": False,
            "ranking_backend": "embedding_profile",
            "embedding_backend": ASSISTANT_EMBED_BACKEND,
            "collaborative_model_ready": bool((profile.get("collaborative") or {}).get("model_ready")),
            "collaborative_model_type": (profile.get("collaborative") or {}).get("model_type") or "",
            "collaborative_model_id": (profile.get("collaborative") or {}).get("model_id") or "",
            "experiment_key": RECOMMENDATION_EXPERIMENT_KEY,
            "experiment_variant": profile.get("experiment_variant") or "control",
            "active_promotion_variant": (_recommendation_active_promotion() or {}).get("promoted_variant") or "",
            "external_worker_expected": RECOMMENDATION_EXTERNAL_WORKER,
            "sync_dsn_configured": bool(RECOMMENDATION_SYNC_DATABASE_DSN),
            "scheduler_enabled": RECOMMENDATION_ENABLE_SCHEDULER,
            "profile_build_ms": profile_ms,
            "row_assembly_ms": row_ms,
            "generator_timings_ms": generator_timings,
            "row_status": row_diagnostics,
            "row_order": [row["id"] for row in rows],
            "row_item_counts": {row["id"]: len(row.get("items") or []) for row in rows},
            "profile_key": profile_key,
            "total_build_ms": int((time.perf_counter() - started_at) * 1000),
        },
    }
    with recommendation_feed_lock:
        recommendation_feed_sessions[session_id] = session
        recommendation_feed_index[profile_key] = {
            "session_id": session_id,
            "expires_at": session["expires_at"],
        }
    _recommendation_record_impressions(session, rows)
    return session


def _recommendation_feed_response(session):
    initial_rows = [
        _recommendation_row_slice(row, 0, RECOMMENDATION_ROW_PAGE_SIZE)
        for row in session.get("rows") or []
    ]
    flatten_priority = {
        "continue_listening": 0,
        "because_you_played": 1,
        "listeners_like_you": 2,
        "search_rebound": 3,
        "rediscover": 4,
        "deep_cuts": 5,
        "offline_ready": 6,
        "trending_for_you": 7,
        "quiet_picks": 8,
        "frequently_listened": 9,
    }
    flattened = []
    for row in sorted(
        initial_rows,
        key=lambda item: flatten_priority.get(item.get("kind"), 50),
    ):
        flattened.extend(row.get("items") or [])
        if len(flattened) >= 18:
            break
    return {
        "status": "success",
        "session_id": session["session_id"],
        "generated_at": session["generated_at"],
        "expires_at": session["expires_at"],
        "rows": initial_rows,
        "recommendations": flattened[:18],
        "has_more": any(row["has_more"] for row in initial_rows),
        "next_offset": sum(len(row.get("items") or []) for row in initial_rows),
        "diagnostics": session.get("diagnostics") or {},
    }


def _recommendation_row_page_response(req: SearchRequest):
    _recommendation_prune_feed_cache()
    session_id = _recommendation_trim_text(req.session_id)
    row_id = _recommendation_trim_text(req.row_id)
    if not session_id or not row_id:
        raise HTTPException(status_code=400, detail="session_id and row_id are required")
    with recommendation_feed_lock:
        session = recommendation_feed_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recommendation session expired")
    if session.get("user_scope_id") != _recommendation_trim_text(req.user_scope_id or "guest"):
        raise HTTPException(status_code=403, detail="Recommendation session scope mismatch")
    stored_rows = list(session.get("rows") or [])
    for index, row in enumerate(stored_rows):
        if row.get("id") == row_id:
            target_row = row
            if _recommendation_should_extend_row(
                target_row,
                req.offset,
                req.limit or RECOMMENDATION_ROW_PAGE_SIZE,
            ):
                extended_row = _recommendation_extend_quiet_picks_row(
                    target_row,
                    session.get("profile") or {},
                    page_size=max(req.limit or RECOMMENDATION_ROW_PAGE_SIZE, 10),
                )
                stored_rows[index] = extended_row
                session["rows"] = stored_rows
                with recommendation_feed_lock:
                    recommendation_feed_sessions[session_id] = session
                target_row = extended_row
            sliced = _recommendation_row_slice(
                target_row,
                req.offset,
                req.limit or RECOMMENDATION_ROW_PAGE_SIZE,
            )
            return {
                "status": "success",
                "session_id": session["session_id"],
                "generated_at": session["generated_at"],
                "expires_at": session["expires_at"],
                "row": sliced,
                "diagnostics": session.get("diagnostics") or {},
            }
    raise HTTPException(status_code=404, detail="Recommendation row not found")

@app.post("/recommend")
def get_recommendations(req: SearchRequest):
    try:
        request_started_at = time.perf_counter()
        if _recommendation_trim_text(req.session_id) and _recommendation_trim_text(req.row_id):
            response = _recommendation_row_page_response(req)
        else:
            session = _recommendation_build_session(req)
            response = _recommendation_feed_response(session)

        diagnostics = response.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.setdefault(
                "request_ms",
                int((time.perf_counter() - request_started_at) * 1000),
            )
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/assistant/sessions")
def assistant_list_sessions(user_scope_id: str, include_archived: bool = False):
    sessions = _assistant_list_sessions(user_scope_id, include_archived=include_archived)
    return {"status": "success", "sessions": sessions}


@app.post("/assistant/sessions")
def assistant_create_session(req: AssistantSessionCreateRequest):
    session = _assistant_create_session(req.user_scope_id, title=req.title)
    return {"status": "success", "session": session}


@app.get("/assistant/sessions/{session_id}")
def assistant_get_session(session_id: str, user_scope_id: str):
    detail = _assistant_get_session_detail(session_id, user_scope_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success", **detail}


@app.patch("/assistant/sessions/{session_id}")
def assistant_update_session(session_id: str, req: AssistantSessionUpdateRequest):
    session = _assistant_update_session(
        session_id,
        req.user_scope_id,
        title=req.title,
        archived=req.archived,
        pinned=req.pinned,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success", "session": session}


@app.delete("/assistant/sessions/{session_id}")
def assistant_delete_session(session_id: str, user_scope_id: str):
    deleted = _assistant_delete_session(session_id, user_scope_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Assistant session not found")
    return {"status": "success"}


@app.post("/assistant/chat")
def assistant_chat(req: AssistantChatRequest):
    session = None
    scope_id = _assistant_safe_scope_id(req.user_scope_id)
    try:
        request_started_at = time.perf_counter()
        if req.session_id:
            session = _assistant_get_session(req.session_id, scope_id)
        if session is None:
            session = _assistant_create_session(scope_id, seed_message=req.message)
            req.session_id = session["id"]

        _assistant_store_session_message(
            session["id"],
            scope_id,
            role="user",
            content=req.message,
            payload={"role": "user"},
        )
        _assistant_touch_session(
            session["id"],
            scope_id,
            last_message_preview=req.message,
        )

        selected_model = _assistant_model_for_request(req)
        if USE_LANGGRAPH_ASSISTANT and langgraph_runtime_available():
            payload = run_langgraph_assistant(req, _assistant_langgraph_deps(req))
        else:
            reply = _assistant_fallback_chat_reply(req, model_override=selected_model)
            _assistant_store_turn_memory(
                req,
                {
                    "reply": reply,
                    "action_type": "chat",
                    "selected_track_ids": [],
                    "playlist_name": None,
                    "playlist_summary": None,
                },
                selected_tracks=[],
                target_playlist=None,
            )
            payload = {
                "status": "success",
                "mode": "conversation",
                "reply": reply,
                "follow_up_question": None,
                "tracks": [],
                "playlist_draft": None,
                "target_playlist": None,
                "playlist_options": [],
                "fact_cards": [],
                "source_links": [],
                "clarification_options": [],
                "action_type": "chat",
            }
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.setdefault("model", selected_model)
            diagnostics.setdefault(
                "total_http_ms",
                int((time.perf_counter() - request_started_at) * 1000),
            )
            payload["diagnostics"] = diagnostics
            print(
                "[assistant_chat] "
                f"session={session['id']} "
                f"mode={payload.get('mode')} "
                f"action={payload.get('action_type')} "
                f"model={diagnostics.get('model')} "
                f"planned={','.join(diagnostics.get('planned_tools') or []) or 'none'} "
                f"executed={','.join(diagnostics.get('executed_tools') or []) or 'none'} "
                f"totalMs={diagnostics.get('total_http_ms')}"
            )
        else:
            print(
                "[assistant_chat] "
                f"session={session['id']} "
                f"mode={payload.get('mode')} "
                f"action={payload.get('action_type')} "
                f"model={selected_model} "
                f"totalMs={int((time.perf_counter() - request_started_at) * 1000)}"
            )

        _assistant_store_session_message(
            session["id"],
            scope_id,
            role="assistant",
            content=payload.get("reply") or "",
            payload=payload,
        )
        _assistant_touch_session(
            session["id"],
            scope_id,
            last_message_preview=payload.get("reply") or req.message,
            last_mode=payload.get("mode"),
        )
        refreshed_session = _assistant_get_session(session["id"], scope_id) or session
        return {
            **payload,
            "session_id": refreshed_session["id"],
            "session_title": refreshed_session["title"],
            "session": refreshed_session,
        }
    except Exception as e:
        print("[assistant_chat][error]", traceback.format_exc())
        if session is not None:
            payload = {
                "status": "success",
                "mode": "conversation",
                "reply": "I hit a snag pulling that together, but I can keep going. Try rephrasing it or ask me to narrow the request.",
                "follow_up_question": None,
                "tracks": [],
                "playlist_draft": None,
                "target_playlist": None,
                "playlist_options": [],
                "fact_cards": [],
                "source_links": [],
                "clarification_options": [],
                "action_type": "chat",
                "diagnostics": {
                    "error": str(e),
                    "total_http_ms": 0,
                },
            }
            _assistant_store_session_message(
                session["id"],
                scope_id,
                role="assistant",
                content=payload["reply"],
                payload=payload,
            )
            _assistant_touch_session(
                session["id"],
                scope_id,
                last_message_preview=payload["reply"],
                last_mode=payload["mode"],
            )
            refreshed_session = _assistant_get_session(session["id"], scope_id) or session
            return {
                **payload,
                "session_id": refreshed_session["id"],
                "session_title": refreshed_session["title"],
                "session": refreshed_session,
            }
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/warm_streams")
def warm_streams(req: WarmStreamRequest):
    prepared, failed = _prepare_streams_with_failures(
        req.video_ids,
        limit=18,
        current_video_id=req.current_video_id,
        active_queue=req.active_queue,
    )
    return {
        "status": "success",
        "streams": prepared,
        "failed": failed,
        "failed_ids": list(failed.keys()),
    }

@app.post("/download")
def download_audio(req: DownloadRequest):
    out_path = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.mp3")
    json_cache = os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json")
    
    # Aggressive Cache Purging if Windows Host yt_dlp choked and left a 0-byte MP3 artifact
    if os.path.exists(out_path) and os.path.getsize(out_path) < 100:
        os.remove(out_path)
        if os.path.exists(json_cache):
            os.remove(json_cache)
            
    if os.path.exists(out_path) and os.path.exists(json_cache):
        try:
            with open(json_cache, "r") as f:
                meta = json.load(f)
                meta["message"] = "Already downloaded"
                return meta
        except Exception:
            pass
            
    if os.path.exists(out_path):
        ydl_opts = {'quiet': True, 'no_warnings': True}
        url = f"https://www.youtube.com/watch?v={req.video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                meta = {
                    "status": "success", 
                    "video_id": req.video_id,
                    "title": info.get("title") or "Unknown Track",
                    "thumbnail": info.get("thumbnail"),
                    "duration": info.get("duration") or 0,
                    "filesize": os.path.getsize(out_path),
                    "filename": f"{req.video_id}.mp3",
                    "author": info.get("channel") or info.get("uploader"),
                    "message": "Already downloaded"
                }
                with open(json_cache, "w") as f:
                    json.dump(meta, f)
                return meta
            except Exception as e:
                # If extraction fails on an existing file, the file must be deleted to allow re-downloads!
                os.remove(out_path)
                raise HTTPException(status_code=500, detail=str(e))

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, f"{req.video_id}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': False,
        'no_warnings': True,
    }

    url = f"https://www.youtube.com/watch?v={req.video_id}"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # We first extract info to get the metadata for the UI (thumbnail, title, byte size)
            info = ydl.extract_info(url, download=True)
            meta = {
                "status": "success",
                "video_id": req.video_id,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration") or 0,
                "filesize": info.get("filesize_approx") or info.get("filesize") or 0,
                "filename": f"{req.video_id}.mp3",
                "author": info.get("channel") or info.get("uploader"),
            }
        except Exception as e:
            if "unavailable" in str(e).lower() or "sign in" in str(e).lower():
                search_query = f"ytsearch1:{req.title} audio" if req.title else f"ytsearch1:{req.video_id} audio"
                info_list = ydl.extract_info(search_query, download=True)
                if "entries" in info_list and len(info_list["entries"]) > 0:
                    info = info_list["entries"][0]
                    meta = {
                        "status": "success",
                        "video_id": req.video_id,
                        "title": info.get("title"),
                        "thumbnail": info.get("thumbnail"),
                        "duration": info.get("duration") or 0,
                        "filesize": info.get("filesize_approx") or info.get("filesize") or 0,
                        "filename": f"{req.video_id}.mp3",
                        "author": info.get("channel") or info.get("uploader"),
                    }
                else:
                    raise HTTPException(status_code=500, detail=f"Fallback search failed for {req.video_id}")
            else:
                raise HTTPException(status_code=500, detail=str(e))
                
        try:
            with open(os.path.join(DOWNLOAD_DIR, f"{req.video_id}.json"), "w") as f:
                json.dump(meta, f)
        except Exception as e:
            print(f"JSON DUMP ERROR: {e}")
            pass
        return meta

@app.get("/stream/{video_id}")
def stream_audio(video_id: str):
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please download first.")
    
    return FileResponse(file_path, media_type="audio/mpeg", filename=f"{video_id}.mp3")


@app.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    try:
        stream_info = get_stream_info(video_id)
        cached_chunk = _get_cached_stream_chunk(video_id)
        range_header = request.headers.get("range")

        if cached_chunk:
            cached_bytes = cached_chunk.get("bytes") or b""
            content_type = cached_chunk.get("content_type") or stream_info.get("mime_type") or "audio/mp4"
            total_length = cached_chunk.get("total_length")
            parsed_range = _parse_byte_range(range_header, total_length)

            if not range_header or parsed_range is not None:
                start = parsed_range[0] if parsed_range else 0
                end = parsed_range[1] if parsed_range else None

                if start < len(cached_bytes):
                    cached_end = len(cached_bytes) - 1
                    requested_end = end if end is not None else cached_end
                    slice_end = min(cached_end, requested_end)
                    cached_slice = cached_bytes[start:slice_end + 1]

                    def generate_cached_then_upstream():
                        if cached_slice:
                            yield cached_slice

                        upstream_start = len(cached_bytes)
                        if upstream_start <= start:
                            upstream_start = start

                        if end is not None and upstream_start > end:
                            return

                        if total_length is not None and upstream_start >= total_length:
                            return

                        yield from _iter_upstream_stream(
                            video_id,
                            stream_info,
                            start=upstream_start,
                            end=end,
                        )

                    resp_headers = {"Accept-Ranges": "bytes"}
                    status_code = 206 if range_header else 200
                    if total_length is not None:
                        response_end = end if end is not None else total_length - 1
                        if status_code == 206:
                            resp_headers["Content-Range"] = (
                                f"bytes {start}-{response_end}/{total_length}"
                            )
                            resp_headers["Content-Length"] = str(
                                max(response_end - start + 1, 0)
                            )
                        else:
                            resp_headers["Content-Length"] = str(total_length)
                    elif status_code == 206 and end is not None:
                        resp_headers["Content-Length"] = str(
                            max(end - start + 1, 0)
                        )

                    return StreamingResponse(
                        generate_cached_then_upstream(),
                        status_code=status_code,
                        headers=resp_headers,
                        media_type=content_type,
                    )

        total_length = cached_chunk.get("total_length") if cached_chunk else None
        parsed_range = _parse_byte_range(range_header, total_length)
        start = parsed_range[0] if parsed_range else 0
        end = parsed_range[1] if parsed_range else None

        def generate_upstream():
            yield from _iter_upstream_stream(
                video_id,
                stream_info,
                start=start,
                end=end,
            )

        resp_headers = {"Accept-Ranges": "bytes"}
        status_code = 206 if range_header and parsed_range is not None else 200
        if total_length is not None:
            response_end = end if end is not None else total_length - 1
            if status_code == 206:
                resp_headers["Content-Range"] = f"bytes {start}-{response_end}/{total_length}"
                resp_headers["Content-Length"] = str(max(response_end - start + 1, 0))
            elif start == 0:
                resp_headers["Content-Length"] = str(total_length)
        return StreamingResponse(
            generate_upstream(),
            status_code=status_code,
            headers=resp_headers,
            media_type=stream_info.get("mime_type", "audio/mp4")
        )
    except HTTPException:
        raise
    except Exception as e:
        classified = _classify_stream_failure(e)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )

@app.get("/direct_url/{video_id}")
def direct_stream_url(video_id: str):
    try:
        stream_info = get_stream_info(video_id)
        return {
            "status": "success",
            "url": stream_info["url"],
            "headers": stream_info["headers"],
            "mime_type": stream_info["mime_type"],
            "duration": stream_info["duration"],
        }
    except HTTPException:
        raise
    except Exception as e:
        classified = _classify_stream_failure(e)
        raise HTTPException(
            status_code=classified["status_code"],
            detail={
                "code": classified["code"],
                "message": classified["message"],
                "video_id": video_id,
            },
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Auralis Proxy Server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)



