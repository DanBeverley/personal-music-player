from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple
import hashlib
import time

from ..domain.artist_recommendations import ArtistRecommendationService
from ..search.runtime import search_artist_seed_tracks
from ..storage.session_store import get_session_store
from .feature_layer import artist_catalog_alignment
from .home_config import (
    _HOME_ALBUM_CAP,
    _HOME_ARTIST_CAP,
    _HOME_ARTIST_MEMORY_TTL_SECONDS,
    _HOME_ARTIST_NEIGHBOR_LIMIT,
    _HOME_ARTIST_NEIGHBOR_TRACK_LIMIT,
    _HOME_POOL_CANDIDATE_CAP,
)
from .pool_runtime import _extend_pool, _track_list_to_candidates
from .source_runtime import (
    _recommendation_candidate_sources_for_track,
    _recommendation_recommended_albums_row,
)


_ARTIST_RECOMMENDATION_SERVICE = ArtistRecommendationService()


def _artist_recommendation_service(server: Any) -> ArtistRecommendationService:
    _ARTIST_RECOMMENDATION_SERVICE._server = server
    return _ARTIST_RECOMMENDATION_SERVICE


def home_artist_request(server: Any, profile: Dict[str, Any], *, limit: int) -> Any:
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        6,
    )
    return server.SearchRequest(
        query="",
        limit=limit,
        surface="home_feed",
        user_scope_id=profile.get("user_scope_id") or "guest",
        taste_queries=list(profile.get("taste_queries") or [])[:4],
        artist_hints=server._recommendation_unique_strings(
            [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
                *(profile.get("listened_artists") or []),
            ],
            12,
        ),
        anchor_artist_hints=server._recommendation_unique_strings(
            [
                *(profile.get("top_artists") or []),
                *(profile.get("artist_hints") or []),
            ],
            8,
        ),
        album_hints=list(profile.get("top_albums") or [])[:8],
        recent_track_ids=list(profile.get("recent_track_ids") or [])[:12],
        top_track_ids=list(profile.get("top_track_ids") or [])[:12],
        recent_queries=[],
        library_track_ids=list(profile.get("library_track_ids") or [])[:12],
        offline_track_ids=list(profile.get("offline_track_ids") or [])[:12],
        recent_track_snapshots=list(profile.get("recent_track_snapshots") or [])[:12],
        top_track_snapshots=list(profile.get("top_track_snapshots") or [])[:12],
        anchor_track_snapshots=anchor_tracks,
        last_played_tracks=list(profile.get("last_played_tracks") or [])[:12],
    )


def artist_rotation_offset(server: Any, profile: Dict[str, Any], item_count: int) -> int:
    if item_count <= 1:
        return 0
    payload = {
        "recent_track_ids": list(profile.get("recent_track_ids") or [])[:8],
        "top_track_ids": list(profile.get("top_track_ids") or [])[:8],
        "recent_queries": list(profile.get("recent_queries") or [])[:4],
        "artist_hints": list(profile.get("artist_hints") or [])[:6],
    }
    digest = hashlib.sha1(
        repr(payload).encode("utf-8")
    ).hexdigest()
    return int(digest[:6], 16) % item_count


def recommended_artist_memory_key(user_scope_id: str) -> str:
    return f"auralis:recommend:artist_row_memory:{user_scope_id}"


def load_recent_artist_memory(server: Any, profile: Dict[str, Any]) -> Set[str]:
    try:
        payload = get_session_store().get(
            recommended_artist_memory_key(profile.get("user_scope_id") or "guest")
        )
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return {
            server._normalize_text(item)
            for item in list(payload.get("artist_keys") or [])
            if server._normalize_text(item)
        }
    return set()


def store_recent_artist_memory(server: Any, profile: Dict[str, Any], artist_keys: Sequence[str]) -> None:
    payload = {
        "artist_keys": [
            server._normalize_text(item)
            for item in artist_keys
            if server._normalize_text(item)
        ][:24]
    }
    try:
        get_session_store().set(
            recommended_artist_memory_key(profile.get("user_scope_id") or "guest"),
            payload,
            _HOME_ARTIST_MEMORY_TTL_SECONDS,
        )
    except Exception:
        return


def select_rotated_artists(
    server: Any,
    profile: Dict[str, Any],
    artists: Sequence[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    affinity_keys = {
        server._normalize_text(name)
        for name in [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
        ]
        if server._normalize_text(name)
    }
    recent_memory = load_recent_artist_memory(server, profile)
    current_anchor_keys = {
        server._normalize_text(name)
        for name in [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
        ]
        if server._normalize_text(name)
    }
    candidates = [
        dict(artist)
        for artist in artists or []
        if isinstance(artist, dict)
    ]
    if not candidates:
        return []
    offset = artist_rotation_offset(server, profile, len(candidates))
    rotated = candidates[offset:] + candidates[:offset]
    ranked = []
    for index, artist in enumerate(rotated):
        artist_key = server._normalize_text(artist.get("name") or "")
        if not artist_key:
            continue
        alignment = artist_catalog_alignment(server, artist, profile)
        score = (
            (float(alignment.get("scene_affinity") or 0.0) * 2.2)
            + (float(alignment.get("peer_scene_bonus") or 0.0) * 1.6)
            + (float(alignment.get("genre_affinity") or 0.0) * 1.1)
            + (float(alignment.get("subgenre_affinity") or 0.0) * 0.55)
            + (float(alignment.get("era_affinity") or 0.0) * 0.65)
            + (float(alignment.get("language_affinity") or 0.0) * 0.3)
            - (float(alignment.get("negative_feedback_penalty") or 0.0) * 2.3)
            - (0.85 if artist_key in recent_memory else 0.0)
            - (0.55 if artist_key in affinity_keys else 0.0)
            - (0.35 if artist_key in current_anchor_keys else 0.0)
            - (index * 0.015)
        )
        enriched_artist = dict(artist)
        enriched_artist["item_feature_summary"] = dict(
            alignment.get("item_feature_summary") or {}
        )
        ranked.append((score, enriched_artist))
    if not ranked:
        return []
    ranked.sort(key=lambda item: item[0], reverse=True)
    non_affinity_ranked = [
        (score, artist)
        for score, artist in ranked
        if server._normalize_text((artist or {}).get("name") or "") not in affinity_keys
    ]
    if non_affinity_ranked:
        ranked = list(non_affinity_ranked)
    selected: List[Dict[str, Any]] = []
    seen_names = set()
    for _score, artist in ranked:
        normalized_name = server._normalize_text(artist.get("name") or "")
        if not normalized_name or normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        selected.append(artist)
        if len(selected) >= limit:
            break
    return selected


def build_artist_artifacts(
    server: Any,
    profile: Dict[str, Any],
    *,
    recommendation_service=None,
    search_artist_seed_tracks_fn=None,
    recommended_artist_items_fn=None,
    full_refinement: bool = False,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    recommendation_service = recommendation_service or _ARTIST_RECOMMENDATION_SERVICE
    search_artist_seed_tracks_fn = search_artist_seed_tracks_fn or search_artist_seed_tracks
    recommended_artist_items_fn = recommended_artist_items_fn or build_profile_artist_items
    request = home_artist_request(server, profile, limit=max(_HOME_ARTIST_CAP * 2, 14))
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        6,
    )
    try:
        recommendation_service._server = server
        payload = recommendation_service.recommend(
            legacy_req=request,
            profile=profile,
            limit=max(_HOME_ARTIST_CAP * 2, 14),
            anchor_tracks=anchor_tracks,
            anchor_artist_names=list(profile.get("top_artists") or [])[:6],
        )
    except Exception:
        payload = {}

    ranked_artists = [
        dict(artist)
        for artist in list((payload or {}).get("artists") or [])
        if isinstance(artist, dict)
    ]
    selected_artists = select_rotated_artists(
        server,
        profile,
        ranked_artists,
        limit=_HOME_ARTIST_CAP,
    )
    if not selected_artists:
        selected_artists = recommended_artist_items_fn(server, profile)
    store_recent_artist_memory(
        server,
        profile,
        [
            artist.get("name") or ""
            for artist in selected_artists
            if isinstance(artist, dict)
        ],
    )

    peer_seed_names = server._recommendation_unique_strings(
        [
            *(artist.get("name") for artist in ranked_artists if isinstance(artist, dict)),
            *(artist.get("name") for artist in selected_artists if isinstance(artist, dict)),
        ],
        max(_HOME_ARTIST_NEIGHBOR_LIMIT + 1, 4),
    )
    candidates: List[Dict[str, Any]] = []
    searched_seed_count = 0
    neighbor_ready_threshold = (
        _HOME_POOL_CANDIDATE_CAP
        if full_refinement
        else min(
            _HOME_POOL_CANDIDATE_CAP,
            max(_HOME_ARTIST_NEIGHBOR_LIMIT * _HOME_ARTIST_NEIGHBOR_TRACK_LIMIT, 24),
        )
    )
    for index, artist_name in enumerate(peer_seed_names):
        try:
            tracks = search_artist_seed_tracks_fn(
                artist_name,
                _HOME_ARTIST_NEIGHBOR_TRACK_LIMIT,
                server=server,
            )
        except Exception:
            tracks = []
        searched_seed_count += 1
        _extend_pool(
            server,
            candidates,
            _track_list_to_candidates(
                server,
                tracks,
                generator_name=f"peer_artist_neighbors:{server._normalize_text(artist_name) or 'artist'}",
                base_score=max(4.5 - (index * 0.18), 3.2),
                reason=f"Adjacent to the artists shaping your current taste: {artist_name}.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        )
        if (
            not full_refinement
            and len(candidates) >= neighbor_ready_threshold
            and searched_seed_count >= _HOME_ARTIST_NEIGHBOR_LIMIT
        ):
            break
    recommendation_diagnostics = dict((payload or {}).get("diagnostics") or {})
    partial_ready = False
    if not full_refinement:
        partial_ready = bool(
            recommendation_diagnostics.get("home_related_skipped_after_ready")
        )
        partial_ready = partial_ready or searched_seed_count < len(peer_seed_names)
    return {
        "artists": selected_artists[:_HOME_ARTIST_CAP],
        "neighbor_candidates": candidates[:_HOME_POOL_CANDIDATE_CAP],
        "meta": {
            "build_ms": int((time.perf_counter() - started_at) * 1000),
            "peer_seed_count": len(peer_seed_names),
            "searched_seed_count": searched_seed_count,
            "neighbor_ready_threshold": neighbor_ready_threshold,
            "neighbor_candidate_count": len(candidates),
            "partial_ready": partial_ready,
            **(
                {
                    "loading_label": "Refining",
                    "loading_message":
                        "Expanding this artist lane with nearby acts and stronger links.",
                }
                if partial_ready
                else {}
            ),
        },
    }


def build_album_items(
    server: Any,
    profile: Dict[str, Any],
    *,
    existing_candidate_cache: List[Dict[str, Any]] | None = None,
    return_row: bool = False,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    prepared_profile = dict(profile or {})
    if existing_candidate_cache:
        prepared_profile["recommended_album_candidate_cache"] = [
            dict(album)
            for album in list(existing_candidate_cache or [])
            if isinstance(album, dict)
        ]
    row = _recommendation_recommended_albums_row(prepared_profile, server=server)
    if not isinstance(row, dict):
        return {} if return_row else []
    if return_row:
        return row
    items: List[Dict[str, Any]] = []
    seen = set()
    for album in list(row.get("items") or []):
        if not isinstance(album, dict):
            continue
        album_id = server._recommendation_trim_text(album.get("id"))
        title = server._recommendation_trim_text(album.get("title"))
        artist = server._recommendation_trim_text(album.get("artist"))
        key = album_id or f"{server._normalize_text(title)}|{server._normalize_text(artist)}"
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(dict(album))
        if len(items) >= _HOME_ALBUM_CAP:
            break
    return items


def build_profile_artist_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    artist_names = server._recommendation_unique_strings(
        [
            *(profile.get("top_artists") or []),
            *(profile.get("artist_hints") or []),
            *(profile.get("listened_artists") or []),
        ],
        _HOME_ARTIST_CAP * 2,
    )
    source_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        24,
    )
    artist_track_lookup: Dict[str, Dict[str, Any]] = {}
    for track in source_tracks:
        artist_candidates = server._recommendation_unique_strings(
            [
                *(server.extract_artist_names(track) or []),
                server._recommendation_trim_text(track.get("channel")),
                server._recommendation_trim_text(track.get("artist")),
                server._recommendation_trim_text(track.get("author")),
            ],
            4,
        )
        for artist_name in artist_candidates:
            artist_key = server._normalize_text(artist_name)
            if artist_key and artist_key not in artist_track_lookup:
                artist_track_lookup[artist_key] = dict(track)

    if not artist_names:
        artist_names = [
            server._recommendation_trim_text(track.get("channel") or track.get("artist") or track.get("author"))
            for track in source_tracks
            if server._recommendation_trim_text(track.get("channel") or track.get("artist") or track.get("author"))
        ]

    artists: List[Dict[str, Any]] = []
    seen = set()
    for artist_name in artist_names:
        artist_key = server._normalize_text(artist_name)
        if not artist_key or artist_key in seen:
            continue
        seen.add(artist_key)
        track = dict(artist_track_lookup.get(artist_key) or {})
        artists.append(
            {
                "id": f"profile_artist:{artist_key}",
                "name": artist_name,
                "thumbnail": track.get("thumbnail") or "",
                "description": track.get("title") or "",
                "source": "profile_affinity",
            }
        )
        if len(artists) >= _HOME_ARTIST_CAP:
            break
    return artists


def timed_call(fn, *args, **kwargs):
    started_at = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, int((time.perf_counter() - started_at) * 1000)


def fetch_anchor_candidate_pools(
    server: Any,
    anchor_tracks: Sequence[Dict[str, Any]],
    recent_track_ids: set[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    started_at = time.perf_counter()
    anchor_neighbor_candidates: List[Dict[str, Any]] = []
    primary_anchor_candidates: List[Dict[str, Any]] = []
    rediscovery_neighbor_candidates: List[Dict[str, Any]] = []
    for anchor_index, anchor_track in enumerate(anchor_tracks):
        anchor_title = server._recommendation_trim_text(anchor_track.get("title")) or "your recent listening"
        for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(
            anchor_track,
            server=server,
        ):
            if source_name not in {"similar", "collaborative", "artist_seed", "album_context", "fallback_context"}:
                continue
            source_candidates = _track_list_to_candidates(
                server,
                source_tracks,
                generator_name=f"anchor_neighbors:{source_name}",
                base_score=max(float(base_score) - (anchor_index * 0.18), 2.0),
                reason=f"Expanded from {anchor_title}.",
            )
            _extend_pool(
                server,
                anchor_neighbor_candidates,
                source_candidates,
                limit=_HOME_POOL_CANDIDATE_CAP,
            )
            if anchor_index == 0:
                _extend_pool(
                    server,
                    primary_anchor_candidates,
                    source_candidates,
                    limit=_HOME_POOL_CANDIDATE_CAP,
                )
            if source_name in {"similar", "collaborative", "artist_seed"}:
                filtered = [
                    candidate
                    for candidate in source_candidates
                    if server._recommendation_trim_text(
                        (candidate.get("track") or {}).get("id")
                    ) not in recent_track_ids
                ]
                _extend_pool(
                    server,
                    rediscovery_neighbor_candidates,
                    filtered,
                    limit=_HOME_POOL_CANDIDATE_CAP,
                )
    return {
        "anchor_neighbors": anchor_neighbor_candidates,
        "primary_anchor_neighbors": primary_anchor_candidates,
        "rediscovery_neighbors": rediscovery_neighbor_candidates,
    }, int((time.perf_counter() - started_at) * 1000)
