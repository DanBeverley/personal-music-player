from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Any, Dict, Optional
import hashlib
import json
import time

from ..storage.session_store import get_session_store
from .model_runtime import get_collaborative_model
from .taste_runtime import build_taste_profile


_PROFILE_CACHE: Dict[str, Dict[str, Any]] = {}
_PROFILE_LOCK = Lock()
_PROFILE_STORE_PREFIX = "auralis:recommend:profile:"


def _profile_store_key(profile_key: str) -> str:
    return f"{_PROFILE_STORE_PREFIX}{profile_key}"


def invalidate_profile_cache() -> None:
    with _PROFILE_LOCK:
        _PROFILE_CACHE.clear()


def build_profile_key(server: Any, req) -> str:
    recent_snapshot_tracks = server._recommendation_unique_snapshot_tracks(
        [*(req.last_played_tracks or []), *(req.recent_track_snapshots or [])],
        16,
    )
    top_snapshot_tracks = server._recommendation_unique_snapshot_tracks(
        req.top_track_snapshots,
        16,
    )
    payload = {
        "user_scope_id": server._recommendation_trim_text(req.user_scope_id or "guest"),
        "surface": server._recommendation_trim_text(req.surface or "home_feed") or "home_feed",
        "seed_id": server._recommendation_trim_text(req.seed_id),
        "seed_ids": server._recommendation_unique_track_ids(req.seed_ids, 16),
        "recent_track_ids": server._recommendation_unique_track_ids(req.recent_track_ids, 16),
        "top_track_ids": server._recommendation_unique_track_ids(req.top_track_ids, 16),
        "recent_track_snapshot_ids": [track.get("id") for track in recent_snapshot_tracks],
        "top_track_snapshot_ids": [track.get("id") for track in top_snapshot_tracks],
        "artist_hints": server._recommendation_unique_strings(req.artist_hints, 12),
        "anchor_artist_hints": server._recommendation_unique_strings(req.anchor_artist_hints, 8),
        "album_hints": server._recommendation_unique_strings(req.album_hints, 12),
        "taste_queries": server._recommendation_unique_strings(req.taste_queries, 12),
        "recent_queries": server._recommendation_unique_strings(req.recent_queries, 12),
        "playlist_names": server._recommendation_unique_strings(req.playlist_names, 12),
        "library_track_ids": server._recommendation_unique_track_ids(req.library_track_ids, 24),
        "offline_track_ids": server._recommendation_unique_track_ids(req.offline_track_ids, 24),
        "anchor_track_ids": [
            track.get("id")
            for track in server._recommendation_unique_snapshot_tracks(req.anchor_track_snapshots, 8)
        ],
        "query": server._recommendation_trim_text(req.query),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _ttl_seconds(server: Any) -> int:
    return max(int(server.RECOMMENDATION_PROFILE_CACHE_TTL_SECONDS or 0), 60)


def _with_runtime_meta(profile: Dict[str, Any], *, cache_hit: bool, source: str) -> Dict[str, Any]:
    payload = deepcopy(profile)
    payload["profile_runtime"] = {
        "cache_hit": bool(cache_hit),
        "source": source,
        "generated_at": float(time.time()),
    }
    return payload


def get_cached_profile(server: Any, profile_key: str) -> Optional[Dict[str, Any]]:
    normalized_key = server._recommendation_trim_text(profile_key)
    if not normalized_key:
        return None
    now = time.time()
    with _PROFILE_LOCK:
        cached = _PROFILE_CACHE.get(normalized_key)
        if cached and float(cached.get("expires_at") or 0.0) > now:
            return _with_runtime_meta(
                cached.get("profile") or {},
                cache_hit=True,
                source="memory",
            )
    try:
        stored = get_session_store().get(_profile_store_key(normalized_key))
    except Exception:
        stored = None
    if isinstance(stored, dict):
        with _PROFILE_LOCK:
            _PROFILE_CACHE[normalized_key] = {
                "profile": deepcopy(stored),
                "expires_at": now + _ttl_seconds(server),
            }
        return _with_runtime_meta(stored, cache_hit=True, source="session_store")
    return None


def store_cached_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    profile_key = server._recommendation_trim_text(profile.get("profile_key"))
    if not profile_key:
        return _with_runtime_meta(profile, cache_hit=False, source="computed")
    ttl_seconds = _ttl_seconds(server)
    payload = deepcopy(profile)
    payload.pop("profile_runtime", None)
    with _PROFILE_LOCK:
        _PROFILE_CACHE[profile_key] = {
            "profile": deepcopy(payload),
            "expires_at": time.time() + ttl_seconds,
        }
    try:
        get_session_store().set(_profile_store_key(profile_key), payload, ttl_seconds)
    except Exception:
        pass
    return _with_runtime_meta(payload, cache_hit=False, source="computed")


def build_collaborative_profile(
    server: Any,
    profile,
    *,
    force_refresh: bool = False,
):
    model = get_collaborative_model(
        server,
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
    user_scope_id = server._assistant_safe_scope_id(profile.get("user_scope_id") or "guest")
    seed_track_ids = server._recommendation_unique_track_ids(
        [
            *(profile.get("recent_track_ids") or []),
            *(profile.get("top_track_ids") or []),
            *(profile.get("library_track_ids") or []),
        ],
        16,
    )
    session_vector = server._vector_weighted_average(
        [
            (item_factors.get(track_id) or [], max(1.7 - (index * 0.12), 0.45))
            for index, track_id in enumerate(seed_track_ids[:10])
            if item_factors.get(track_id)
        ]
    )
    stored_user_vector = user_factors.get(user_scope_id) or []
    user_vector = server._vector_weighted_average(
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
        for neighbor in (item_neighbors.get(track_id) or [])[:server.RECOMMENDATION_MODEL_NEIGHBOR_LIMIT]:
            candidate_id = server._recommendation_trim_text(neighbor.get("track_id"))
            if not candidate_id or candidate_id in known_track_ids:
                continue
            candidate_scores[candidate_id] = candidate_scores.get(candidate_id, 0.0) + (
                float(neighbor.get("score") or 0.0) * decay
            )

    if user_vector:
        for track_id, item_vector in item_factors.items():
            if track_id in known_track_ids:
                continue
            latent_score = max(0.0, server._assistant_cosine_similarity(user_vector, item_vector))
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
        artist_key = server._normalize_text(track_artists.get(track_id) or "")
        if not artist_key:
            continue
        artist_scores[artist_key] = artist_scores.get(artist_key, 0.0) + float(score)

    blended_queries = server._recommendation_unique_strings(
        [
            *(profile.get("recent_queries") or []),
            *(profile.get("taste_queries") or []),
        ],
        8,
    )
    for query in blended_queries:
        normalized_query = server._normalize_text(query)
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


def build_profile_vectors(server: Any, profile):
    short_term_tracks = [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("anchor_track_snapshots") or [])[:4],
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

    track_embeddings = server._recommendation_track_embeddings(
        [track for _, _, track in track_items]
    )

    query_entries = []
    for index, query in enumerate(profile.get("recent_queries") or []):
        text = (query or "").strip()
        if not text:
            continue
        key = server._recommendation_text_embedding_key("query", text)
        query_entries.append((key, text, max(1.6 - (index * 0.12), 0.5)))
    for index, query in enumerate(profile.get("taste_queries") or []):
        text = (query or "").strip()
        if not text:
            continue
        key = server._recommendation_text_embedding_key("taste", text)
        query_entries.append((key, text, max(1.45 - (index * 0.1), 0.45)))
    for index, artist_hint in enumerate(profile.get("artist_hints") or []):
        artist_value = (artist_hint or "").strip()
        if not artist_value:
            continue
        text = f"artist {artist_value}"
        key = server._recommendation_text_embedding_key("artist_hint", text)
        query_entries.append((key, text, max(1.3 - (index * 0.08), 0.4)))
    for index, album_hint in enumerate(profile.get("album_hints") or []):
        album_value = (album_hint or "").strip()
        if not album_value:
            continue
        text = f"album {album_value}"
        key = server._recommendation_text_embedding_key("album_hint", text)
        query_entries.append((key, text, max(1.15 - (index * 0.08), 0.35)))
    for index, playlist_name in enumerate(profile.get("playlist_names") or []):
        playlist_value = (playlist_name or "").strip()
        if not playlist_value:
            continue
        text = f"playlist {playlist_value}"
        key = server._recommendation_text_embedding_key("playlist", text)
        query_entries.append((key, text, max(0.9 - (index * 0.06), 0.25)))

    text_embeddings = server._recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in query_entries],
    )

    short_term_vectors = []
    long_term_vectors = []
    for scope, index, track in track_items:
        key = server._recommendation_track_embedding_key(track)
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
        key = server._recommendation_text_embedding_key("profile_artist", text)
        artist_entries.append((key, text, max(1.8 - (index * 0.12), 0.55)))
    artist_embeddings = server._recommendation_embed_entries(
        "text",
        [(key, text) for key, text, _weight in artist_entries],
    )
    artist_vectors = []
    for key, _text, weight in artist_entries:
        vector = artist_embeddings.get(key) or []
        if not vector:
            continue
        artist_vectors.append((vector, weight))

    short_term_vector = server._vector_weighted_average(short_term_vectors)
    long_term_vector = server._vector_weighted_average(long_term_vectors)
    query_vector = server._vector_weighted_average(query_vectors)
    artist_vector = server._vector_weighted_average(artist_vectors)
    anchor_track = (
        (profile.get("anchor_track_snapshots") or [None])[0]
        or (profile.get("last_played_tracks") or [None])[0]
        or (profile.get("recent_track_snapshots") or [None])[0]
        or (profile.get("top_track_snapshots") or [None])[0]
    )
    anchor_vector = []
    anchor_key = server._recommendation_track_embedding_key(anchor_track)
    if anchor_key:
        anchor_vector = track_embeddings.get(anchor_key) or []
    taste_vector = server._vector_weighted_average(
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


def hydrate_state_profile(
    server: Any,
    profile: Dict[str, Any],
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    profile_key = server._recommendation_trim_text(profile.get("profile_key"))
    if profile_key and not force_refresh:
        cached = get_cached_profile(server, profile_key)
        if isinstance(cached, dict):
            return cached

    hydrated = deepcopy(profile)
    vectors = build_profile_vectors(server, hydrated)
    collaborative = build_collaborative_profile(
        server,
        hydrated,
        force_refresh=force_refresh,
    )
    hydrated["vectors"] = vectors
    hydrated["embedding_profile"] = vectors
    hydrated["collaborative"] = collaborative
    hydrated["collaborative_profile"] = collaborative
    taste_profile = build_taste_profile(
        server,
        hydrated,
        force_refresh=force_refresh,
    )
    hydrated["taste_profile"] = taste_profile
    hydrated["catalog_feature_version"] = (
        taste_profile.get("catalog_feature_version") or ""
    )
    hydrated["taste_profile_version"] = (
        taste_profile.get("profile_version") or ""
    )
    hydrated["scene_graph_version"] = (
        taste_profile.get("scene_graph_version") or ""
    )
    hydrated["feature_source"] = taste_profile.get("feature_source") or ""
    hydrated["negative_feedback_applied"] = bool(
        int((taste_profile.get("negative_feedback") or {}).get("count") or 0) > 0
    )
    return store_cached_profile(server, hydrated)


def build_profile(server: Any, req) -> Dict[str, Any]:
    profile_key = build_profile_key(server, req)
    if not bool(getattr(req, "force_refresh", False)):
        cached = get_cached_profile(server, profile_key)
        if isinstance(cached, dict):
            return cached

    recent_track_snapshots = server._recommendation_unique_snapshot_tracks(
        [*(req.last_played_tracks or []), *(req.recent_track_snapshots or [])],
        16,
    )
    top_track_snapshots = server._recommendation_unique_snapshot_tracks(
        req.top_track_snapshots,
        16,
    )
    last_played_tracks = server._recommendation_unique_snapshot_tracks(
        req.last_played_tracks,
        12,
    )
    anchor_track_snapshots = server._recommendation_unique_snapshot_tracks(
        req.anchor_track_snapshots,
        8,
    )

    recent_track_ids = server._recommendation_unique_track_ids(
        [
            req.seed_id,
            *(track.get("id") for track in recent_track_snapshots),
            *(req.seed_ids or []),
            *(req.recent_track_ids or []),
        ],
        16,
    )
    top_track_ids = server._recommendation_unique_track_ids(
        [
            *(track.get("id") for track in top_track_snapshots),
            *(req.top_track_ids or []),
            *(req.seed_ids or []),
            req.seed_id,
        ],
        16,
    )
    recent_queries = server._recommendation_unique_strings(
        [*(req.recent_queries or []), req.query, *(req.taste_queries or [])],
        12,
    )
    anchor_artist_hints = server._recommendation_unique_strings(
        [
            *(req.anchor_artist_hints or []),
            *(
                track.get("channel")
                for track in anchor_track_snapshots
                if isinstance(track, dict) and track.get("channel")
            ),
        ],
        8,
    )
    snapshot_artist_hints = [
        track.get("channel")
        for track in [
            *anchor_track_snapshots,
            *top_track_snapshots,
            *recent_track_snapshots,
            *last_played_tracks,
        ]
        if track.get("channel")
    ]
    snapshot_album_hints = [
        track.get("album")
        for track in [
            *anchor_track_snapshots,
            *top_track_snapshots,
            *recent_track_snapshots,
            *last_played_tracks,
        ]
        if track.get("album")
    ]
    artist_hints = server._recommendation_unique_strings(
        [*anchor_artist_hints, *(req.artist_hints or []), *snapshot_artist_hints],
        12,
    )
    album_hints = server._recommendation_unique_strings(
        [*(req.album_hints or []), *snapshot_album_hints],
        12,
    )
    playlist_names = server._recommendation_unique_strings(req.playlist_names, 12)
    library_track_ids = server._recommendation_unique_track_ids(req.library_track_ids, 28)
    offline_track_ids = server._recommendation_unique_track_ids(req.offline_track_ids, 28)

    artist_weights = {}
    for index, track in enumerate(top_track_snapshots):
        artist_name = server._recommendation_trim_text(track.get("channel"))
        if not artist_name:
            continue
        artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
            1.8 - (index * 0.14),
            0.55,
        )
    for index, track in enumerate(recent_track_snapshots):
        artist_name = server._recommendation_trim_text(track.get("channel"))
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
    listened_artist_names = server._recommendation_unique_strings(
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
        "profile_key": profile_key,
        "user_scope_id": server._recommendation_trim_text(req.user_scope_id or "guest") or "guest",
        "surface": server._recommendation_trim_text(req.surface or "home_feed") or "home_feed",
        "recent_track_ids": recent_track_ids,
        "top_track_ids": top_track_ids,
        "recent_track_snapshots": recent_track_snapshots,
        "top_track_snapshots": top_track_snapshots,
        "anchor_track_snapshots": anchor_track_snapshots,
        "last_played_tracks": last_played_tracks,
        "recent_queries": recent_queries,
        "taste_queries": server._recommendation_unique_strings(req.taste_queries, 12),
        "anchor_artist_hints": anchor_artist_hints,
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
        "experiment_variant": server._recommendation_assignment_for_user(
            server._recommendation_trim_text(req.user_scope_id or "guest") or "guest"
        ),
    }
    return hydrate_state_profile(
        server,
        profile,
        force_refresh=bool(getattr(req, "force_refresh", False)),
    )
