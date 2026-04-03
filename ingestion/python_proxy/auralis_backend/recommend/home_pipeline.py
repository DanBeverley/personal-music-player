from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import os
import time

from ..domain.artist_recommendations import ArtistRecommendationService
from .allocator import (
    build_profile_allocator_features,
    build_row_allocation_plan,
    summarize_snapshot_pool_features,
)
from .feature_layer import (
    artist_catalog_alignment,
    build_catalog_feature_profile,
    candidate_catalog_alignment,
    script_bucket,
)
from .freshness_runtime import (
    recent_row_impression_track_ids as _recent_row_impression_track_ids,
)
from .policy import apply_required_row_fallback_policy, row_kinds, row_title
from .row_registry import max_feed_same_artist
from .row_ranking import (
    is_query_derived_source,
    max_same_artist,
    min_items as row_min_items,
    quality_floor,
    track_score,
)
from .source_runtime import (
    _recommendation_candidate_sources_for_track,
    _recommendation_home_fallback_tracks,
    _recommendation_recommended_albums_row,
    _recommendation_taste_filtered_tracks,
)
from ..legacy import build_search_request
from ..search.runtime import search_artist_seed_tracks
from ..storage.session_store import get_session_store


_HOME_HISTORY_POOL_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_HISTORY_POOL_CAP", "24")),
)
_HOME_COLLAB_POOL_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_COLLAB_POOL_CAP", "24")),
)
_HOME_FALLBACK_POOL_CAP = max(
    16,
    int(os.environ.get("AURALIS_HOME_FALLBACK_POOL_CAP", "32")),
)
_HOME_ANCHOR_LIMIT = max(
    1,
    int(os.environ.get("AURALIS_HOME_ANCHOR_LIMIT", "3")),
)
_HOME_ARTIST_NEIGHBOR_LIMIT = max(
    1,
    int(os.environ.get("AURALIS_HOME_ARTIST_NEIGHBOR_LIMIT", "3")),
)
_HOME_POOL_CANDIDATE_CAP = max(
    16,
    int(os.environ.get("AURALIS_HOME_POOL_CANDIDATE_CAP", "48")),
)
_HOME_ALBUM_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_ALBUM_CAP", "18")),
)
_HOME_ARTIST_CAP = max(
    6,
    int(os.environ.get("AURALIS_HOME_ARTIST_CAP", "12")),
)
_HOME_ARTIST_NEIGHBOR_TRACK_LIMIT = max(
    6,
    int(os.environ.get("AURALIS_HOME_ARTIST_NEIGHBOR_TRACK_LIMIT", "10")),
)
_HOME_ARTIST_MEMORY_TTL_SECONDS = max(
    900,
    int(os.environ.get("AURALIS_HOME_ARTIST_MEMORY_TTL_SECONDS", "21600")),
)

_ARTIST_RECOMMENDATION_SERVICE = ArtistRecommendationService()


def _prefiltered_pool_name(row_kind: str) -> str:
    if row_kind == "quiet_picks":
        return "quiet_prefiltered"
    if row_kind == "deep_cuts":
        return "deep_cuts_prefiltered"
    return ""


def _prefilter_pool_order(row_kind: str) -> Tuple[str, ...]:
    if row_kind == "quiet_picks":
        return (
            "peer_scene",
            "genre_subgenre",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "collaborative",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "rediscovery",
            "history_top",
            "history_recent",
            "same_artist",
            "offline_library",
            "taste_fallback",
            "exploration",
        )
    if row_kind == "deep_cuts":
        return (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "rediscovery",
            "collaborative",
            "history_top",
            "history_recent",
            "popularity_taste",
            "exploration",
            "taste_fallback",
        )
    return tuple()

def _candidate_signature(server: Any, candidate: Dict[str, Any]) -> str:
    track = candidate.get("track") if isinstance(candidate.get("track"), dict) else candidate
    return server._recommendation_track_signature(track)


def _candidate_copy(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    track = candidate.get("track")
    payload = {
        "generator_name": candidate.get("generator_name") or "",
        "generator_score": float(candidate.get("generator_score") or 0.0),
        "reason": candidate.get("reason") or "",
        "source_score": float(candidate.get("source_score") or 0.0),
        "source_votes": int(candidate.get("source_votes") or 1),
    }
    if isinstance(track, dict):
        payload["track"] = dict(track)
    else:
        payload["track"] = {}
    return payload


def _trim_candidate_pool(server: Any, candidates: Sequence[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    trimmed: List[Dict[str, Any]] = []
    seen = set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        trimmed.append(copied)
        if len(trimmed) >= limit:
            break
    return trimmed


def _feature_pool_candidate(
    candidate: Dict[str, Any],
    *,
    pool_name: str,
    score: float,
    reason: str,
) -> Dict[str, Any]:
    payload = _candidate_copy(candidate)
    payload["generator_name"] = pool_name
    payload["generator_score"] = max(
        float(payload.get("generator_score") or 0.0),
        float(score or 0.0),
    )
    payload["source_score"] = max(
        float(payload.get("source_score") or 0.0),
        float(score or 0.0),
    )
    if not payload.get("reason"):
        payload["reason"] = reason
    return payload


def _merge_pool_order(
    preferred_pool_names: Sequence[str],
    allocator_pool_names: Sequence[str],
    available_pools: Dict[str, Any],
) -> List[str]:
    merged: List[str] = []
    available = set(dict(available_pools or {}).keys())
    for pool_name in [*list(preferred_pool_names or ()), *list(allocator_pool_names or ())]:
        normalized = str(pool_name or "").strip()
        if not normalized or normalized in merged or normalized not in available:
            continue
        merged.append(normalized)
    return merged


def _build_feature_aware_pools(
    server: Any,
    profile: Dict[str, Any],
    base_pools: Dict[str, Sequence[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    candidate_universe: List[Dict[str, Any]] = []
    seen_signatures = set()
    for pool_name in (
        "collaborative",
        "artist_neighbors",
        "anchor_neighbors",
        "primary_anchor_neighbors",
        "history_recent",
        "history_top",
        "rediscovery",
        "taste_fallback",
        "exploration",
        "offline_library",
    ):
        for candidate in list((base_pools or {}).get(pool_name) or []):
            if not isinstance(candidate, dict):
                continue
            copied = _candidate_copy(candidate)
            signature = _candidate_signature(server, copied)
            if not signature or signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            candidate_universe.append(copied)
    if not candidate_universe:
        return {}

    feature_profile = build_catalog_feature_profile(server, profile)
    dominant_artist_keys = set(feature_profile.get("dominant_artist_keys") or set())
    affinity_artists = set(feature_profile.get("affinity_artists") or set())
    scored_buckets: Dict[str, List[tuple[float, Dict[str, Any]]]] = defaultdict(list)
    for candidate in candidate_universe:
        track = candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        if not track:
            continue
        alignment = candidate_catalog_alignment(server, track, profile)
        base_score = float(
            candidate.get("source_score")
            or candidate.get("generator_score")
            or 0.0
        )
        artist_key = server._normalize_text(alignment.get("artist_key") or "")
        if artist_key and artist_key in (dominant_artist_keys | affinity_artists):
            scored_buckets["same_artist"].append(
                (
                    1.9
                    + float(alignment.get("scene_affinity") or 0.0)
                    + float(alignment.get("genre_affinity") or 0.0)
                    + (base_score * 0.08),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="same_artist",
                        score=max(base_score, 4.4),
                        reason="More from the artists anchoring your current taste.",
                    ),
                )
            )
        if (
            float(alignment.get("peer_scene_bonus") or 0.0) > 0.0
            or float(alignment.get("scene_affinity") or 0.0) >= 0.55
        ):
            scored_buckets["peer_scene"].append(
                (
                    1.2
                    + float(alignment.get("scene_affinity") or 0.0) * 1.4
                    + float(alignment.get("peer_scene_bonus") or 0.0) * 1.2
                    + (base_score * 0.06),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="peer_scene",
                        score=max(base_score, 4.1),
                        reason="Coming from the same scene and neighboring artists you lean toward.",
                    ),
                )
            )
        if (
            float(alignment.get("genre_affinity") or 0.0) >= 0.65
            or float(alignment.get("subgenre_affinity") or 0.0) >= 0.55
        ):
            scored_buckets["genre_subgenre"].append(
                (
                    1.0
                    + float(alignment.get("genre_affinity") or 0.0) * 1.35
                    + float(alignment.get("subgenre_affinity") or 0.0) * 1.15
                    + (base_score * 0.05),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="genre_subgenre",
                        score=max(base_score, 3.8),
                        reason="A genre and subgenre match for the music you keep returning to.",
                    ),
                )
            )
        if (
            float(alignment.get("era_affinity") or 0.0) > 0.0
            or float(alignment.get("adjacent_era_affinity") or 0.0) > 0.0
        ):
            scored_buckets["era_neighbors"].append(
                (
                    0.85
                    + float(alignment.get("era_affinity") or 0.0) * 1.4
                    + float(alignment.get("adjacent_era_affinity") or 0.0) * 0.9
                    + (base_score * 0.04),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="era_neighbors",
                        score=max(base_score, 3.6),
                        reason="Released in the era your listening profile keeps orbiting around.",
                    ),
                )
            )
        if (
            float(alignment.get("language_affinity") or 0.0) >= 0.72
            and float(alignment.get("script_affinity") or 0.0) >= 0.72
        ):
            scored_buckets["language_safe"].append(
                (
                    0.72
                    + float(alignment.get("language_affinity") or 0.0) * 1.1
                    + float(alignment.get("script_affinity") or 0.0) * 0.9
                    + (base_score * 0.03),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="language_safe",
                        score=max(base_score, 3.2),
                        reason="A language and script fit for the music you regularly engage with.",
                    ),
                )
            )
        if (
            float(alignment.get("popularity_taste_fit") or 0.0) >= 0.62
            and float(alignment.get("negative_feedback_penalty") or 0.0) < 0.9
        ):
            scored_buckets["popularity_taste"].append(
                (
                    0.7
                    + float(alignment.get("popularity_taste_fit") or 0.0) * 1.15
                    + float(alignment.get("scene_affinity") or 0.0) * 0.4
                    + float(alignment.get("genre_affinity") or 0.0) * 0.35
                    + (base_score * 0.05),
                    _feature_pool_candidate(
                        candidate,
                        pool_name="popularity_taste",
                        score=max(base_score, 3.5),
                        reason="Popular right now inside the lane your taste profile supports.",
                    ),
                )
            )

    feature_pools: Dict[str, List[Dict[str, Any]]] = {}
    for pool_name, scored_items in scored_buckets.items():
        scored_items.sort(key=lambda item: item[0], reverse=True)
        feature_pools[pool_name] = _trim_candidate_pool(
            server,
            [candidate for _score, candidate in scored_items],
            limit=_HOME_POOL_CANDIDATE_CAP,
        )
    return feature_pools


def _quiet_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any],
) -> List[str]:
    return _merge_pool_order(
        _prefilter_pool_order("quiet_picks"),
        allocation_plan.get("pool_names") or (),
        dict(snapshot.get("pools") or {}),
    )


def _track_list_to_candidates(
    server: Any,
    tracks: Iterable[Dict[str, Any]],
    *,
    generator_name: str,
    base_score: float,
    reason: str,
) -> List[Dict[str, Any]]:
    return server._recommendation_candidates_from_tracks(
        list(tracks or []),
        generator_name,
        float(base_score),
        reason,
    )


def _extend_pool(
    server: Any,
    pool: List[Dict[str, Any]],
    candidates: Iterable[Dict[str, Any]],
    *,
    limit: int,
) -> None:
    existing_signatures = {
        signature
        for signature in (
            _candidate_signature(server, candidate)
            for candidate in pool
        )
        if signature
    }
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in existing_signatures:
            continue
        pool.append(copied)
        existing_signatures.add(signature)
        if len(pool) >= limit:
            break


def _home_artist_request(server: Any, profile: Dict[str, Any], *, limit: int) -> Any:
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        6,
    )
    return build_search_request(
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


def _artist_rotation_offset(server: Any, profile: Dict[str, Any], item_count: int) -> int:
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


def _recommended_artist_memory_key(user_scope_id: str) -> str:
    return f"auralis:recommend:artist_row_memory:{user_scope_id}"


def _load_recent_artist_memory(server: Any, profile: Dict[str, Any]) -> Set[str]:
    try:
        payload = get_session_store().get(
            _recommended_artist_memory_key(profile.get("user_scope_id") or "guest")
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


def _store_recent_artist_memory(server: Any, profile: Dict[str, Any], artist_keys: Sequence[str]) -> None:
    payload = {
        "artist_keys": [
            server._normalize_text(item)
            for item in artist_keys
            if server._normalize_text(item)
        ][:24]
    }
    try:
        get_session_store().set(
            _recommended_artist_memory_key(profile.get("user_scope_id") or "guest"),
            payload,
            _HOME_ARTIST_MEMORY_TTL_SECONDS,
        )
    except Exception:
        return


def _select_rotated_artists(
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
    recent_memory = _load_recent_artist_memory(server, profile)
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
    offset = _artist_rotation_offset(server, profile, len(candidates))
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


def _build_artist_artifacts(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    request = _home_artist_request(server, profile, limit=max(_HOME_ARTIST_CAP * 2, 14))
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        6,
    )
    try:
        payload = _ARTIST_RECOMMENDATION_SERVICE.recommend(
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
    selected_artists = _select_rotated_artists(
        server,
        profile,
        ranked_artists,
        limit=_HOME_ARTIST_CAP,
    )
    if not selected_artists:
        selected_artists = _build_profile_artist_items(server, profile)
    _store_recent_artist_memory(
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
        max(_HOME_ARTIST_NEIGHBOR_LIMIT * 2, 8),
    )
    candidates: List[Dict[str, Any]] = []
    for index, artist_name in enumerate(peer_seed_names):
        try:
            tracks = search_artist_seed_tracks(
                artist_name,
                _HOME_ARTIST_NEIGHBOR_TRACK_LIMIT,
            )
        except Exception:
            tracks = []
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
    return {
        "artists": selected_artists[:_HOME_ARTIST_CAP],
        "neighbor_candidates": candidates[:_HOME_POOL_CANDIDATE_CAP],
    }


def _build_album_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    row = _recommendation_recommended_albums_row(profile)
    if not isinstance(row, dict):
        return []
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


def _build_recommended_artist_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((_build_artist_artifacts(server, profile).get("artists") or []))


def _build_profile_artist_items(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
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


def _timed_call(fn, *args, **kwargs):
    started_at = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, int((time.perf_counter() - started_at) * 1000)


def _fetch_anchor_candidate_pools(
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
        for source_name, source_tracks, base_score in _recommendation_candidate_sources_for_track(anchor_track):
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


def build_home_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    stage_timings: Dict[str, int] = {}
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    anchor_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
            *(profile.get("top_track_snapshots") or []),
        ],
        _HOME_ANCHOR_LIMIT,
    )
    primary_anchor_track = anchor_tracks[0] if anchor_tracks else None

    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    if len(top_history_tracks) < _HOME_HISTORY_POOL_CAP:
        seen_top_ids = {
            server._recommendation_trim_text(track.get("id"))
            for track in top_history_tracks
            if isinstance(track, dict)
        }
        missing_top_ids = [
            track_id
            for track_id in list(profile.get("top_track_ids") or [])
            if track_id not in seen_top_ids
        ]
        if missing_top_ids:
            top_history_tracks.extend(
                server._recommendation_fetch_tracks_for_ids(
                    missing_top_ids,
                    limit=max(_HOME_HISTORY_POOL_CAP - len(top_history_tracks), 0),
                )
            )
            top_history_tracks = server._recommendation_unique_snapshot_tracks(
                top_history_tracks,
                _HOME_HISTORY_POOL_CAP,
            )
    stage_timings["history_prepare_ms"] = int((time.perf_counter() - started_at) * 1000)

    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )
    rediscovery_ids = [
        track_id
        for track_id in list(profile.get("top_track_ids") or [])
        if track_id not in recent_track_ids
    ]
    outer_executor = getattr(server, "recommendation_row_executor", None) or getattr(server, "recommendation_executor")
    fetch_futures = {
        "collaborative": outer_executor.submit(
            _timed_call,
            server._recommendation_fetch_tracks_for_ids,
            collaborative_ids,
            _HOME_COLLAB_POOL_CAP,
        ),
        "fallback": outer_executor.submit(
            _timed_call,
            _recommendation_home_fallback_tracks,
            profile,
            limit=_HOME_FALLBACK_POOL_CAP,
        ),
        "artist_artifacts": outer_executor.submit(
            _timed_call,
            _build_artist_artifacts,
            server,
            profile,
        ),
        "anchor_neighbors": outer_executor.submit(
            _fetch_anchor_candidate_pools,
            server,
            anchor_tracks,
            recent_track_ids,
        ),
        "rediscovery_tracks": outer_executor.submit(
            _timed_call,
            server._recommendation_fetch_tracks_for_ids,
            rediscovery_ids,
            _HOME_HISTORY_POOL_CAP,
        ),
        "offline_tracks": outer_executor.submit(
            _timed_call,
            server._recommendation_fetch_tracks_for_ids,
            list(profile.get("offline_track_ids") or profile.get("library_track_ids") or []),
            _HOME_HISTORY_POOL_CAP,
        ),
        "albums": outer_executor.submit(
            _timed_call,
            _build_album_items,
            server,
            profile,
        ),
    }

    collaborative_tracks = []
    fallback_tracks = []
    artist_neighbor_candidates = []
    anchor_neighbor_candidates: List[Dict[str, Any]] = []
    primary_anchor_candidates: List[Dict[str, Any]] = []
    rediscovery_neighbor_candidates: List[Dict[str, Any]] = []
    rediscovery_tracks = []
    offline_tracks = []
    album_items = []
    artist_items = []

    for future_name, future in fetch_futures.items():
        try:
            payload = future.result()
        except Exception:
            payload = ({} if future_name == "anchor_neighbors" else [], 0)
        if future_name == "anchor_neighbors":
            anchor_payload, elapsed_ms = payload
            anchor_payload = dict(anchor_payload or {})
            anchor_neighbor_candidates = list(anchor_payload.get("anchor_neighbors") or [])
            primary_anchor_candidates = list(anchor_payload.get("primary_anchor_neighbors") or [])
            rediscovery_neighbor_candidates = list(anchor_payload.get("rediscovery_neighbors") or [])
            stage_timings["anchor_neighbor_ms"] = int(elapsed_ms or 0)
            continue
        result, elapsed_ms = payload
        stage_key = {
            "collaborative": "collaborative_fetch_ms",
            "fallback": "fallback_fetch_ms",
            "artist_artifacts": "artist_neighbor_ms",
            "rediscovery_tracks": "rediscovery_fetch_ms",
            "offline_tracks": "offline_fetch_ms",
            "albums": "album_pool_ms",
        }.get(future_name)
        if stage_key:
            stage_timings[stage_key] = int(elapsed_ms or 0)
        if future_name == "collaborative":
            collaborative_tracks = list(result or [])
        elif future_name == "fallback":
            fallback_tracks = list(result or [])
        elif future_name == "artist_artifacts":
            artifact_payload = dict(result or {})
            artist_neighbor_candidates = list(
                artifact_payload.get("neighbor_candidates") or []
            )
            artist_items = list(artifact_payload.get("artists") or [])
        elif future_name == "rediscovery_tracks":
            rediscovery_tracks = list(result or [])
        elif future_name == "offline_tracks":
            offline_tracks = list(result or [])
        elif future_name == "albums":
            album_items = list(result or [])

    exploration_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(collaborative_tracks[_HOME_COLLAB_POOL_CAP // 2:] or []),
            *(fallback_tracks[_HOME_FALLBACK_POOL_CAP // 3:] or []),
            *((candidate.get("track") or {}) for candidate in artist_neighbor_candidates[:18]),
        ],
        _HOME_HISTORY_POOL_CAP,
    )

    pools: Dict[str, List[Dict[str, Any]]] = {
        "history_recent": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                recent_history_tracks,
                generator_name="history_recent",
                base_score=5.0,
                reason="From your recent listening history.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "history_top": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                top_history_tracks,
                generator_name="history_top",
                base_score=5.1,
                reason="You come back to these often.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "offline_library": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                offline_tracks,
                generator_name="offline_library",
                base_score=4.6,
                reason="Ready when you need it offline.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "collaborative": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                collaborative_tracks,
                generator_name="collaborative_home",
                base_score=4.8,
                reason="Listeners with similar taste also played this.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "artist_neighbors": _trim_candidate_pool(
            server,
            artist_neighbor_candidates,
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "anchor_neighbors": _trim_candidate_pool(
            server,
            anchor_neighbor_candidates,
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "primary_anchor_neighbors": _trim_candidate_pool(
            server,
            primary_anchor_candidates,
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "taste_fallback": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                fallback_tracks,
                generator_name="taste_fallback",
                base_score=2.6,
                reason="Taste-filtered fallback while the feed warms up.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "rediscovery": _trim_candidate_pool(
            server,
            [
                *_track_list_to_candidates(
                    server,
                    rediscovery_tracks,
                    generator_name="rediscovery_history",
                    base_score=4.2,
                    reason="A favorite worth bringing back.",
                ),
                *rediscovery_neighbor_candidates,
            ],
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "exploration": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                exploration_tracks,
                generator_name="exploration_pool",
                base_score=2.9,
                reason="A measured step outside your usual rotation.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
    }
    feature_pool_started_at = time.perf_counter()
    pools.update(
        _build_feature_aware_pools(
            server,
            profile,
            pools,
        )
    )
    stage_timings["feature_pool_ms"] = int(
        (time.perf_counter() - feature_pool_started_at) * 1000
    )
    for row_kind in ("quiet_picks", "deep_cuts"):
        prefiltered_candidates, _ = _combine_pools(
            server,
            {"pools": pools},
            _prefilter_pool_order(row_kind),
            limit=max(_HOME_POOL_CANDIDATE_CAP * 2, 72),
        )
        prefiltered_candidates = _post_filter_row_candidates(
            server,
            row_kind,
            profile,
            prefiltered_candidates,
        )
        if prefiltered_candidates:
            pools[_prefiltered_pool_name(row_kind)] = _trim_candidate_pool(
                server,
                prefiltered_candidates,
                limit=max(_HOME_POOL_CANDIDATE_CAP, 56),
            )
    profile_allocator_features = build_profile_allocator_features(profile)
    pool_allocator_features = summarize_snapshot_pool_features(
        server,
        profile=profile,
        pools=pools,
    )

    total_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "generated_at": time.time(),
        "build_ms": total_ms,
        "pool_counts": {
            key: len(value or [])
            for key, value in pools.items()
        },
        "row_mode": "candidate_snapshot_v42",
        "albums_count": len(album_items),
        "artists_count": len(artist_items),
        "primary_anchor_id": server._recommendation_trim_text(
            (primary_anchor_track or {}).get("id")
        ),
        "quiet_seed": server._recommendation_quiet_base_query(profile),
        "profile_allocator_features": profile_allocator_features,
        "pool_allocator_features": pool_allocator_features,
        "stage_timings_ms": stage_timings,
        "pools": pools,
        "albums": album_items,
        "artists": artist_items,
    }


def build_home_candidate_snapshot_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    stage_timings: Dict[str, int] = {}
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )
    collaborative_ids = list(
        ((profile.get("collaborative") or {}).get("candidate_track_ids") or [])
    )[:_HOME_COLLAB_POOL_CAP]
    outer_executor = getattr(server, "recommendation_row_executor", None) or getattr(server, "recommendation_executor")
    fetch_futures = {
        "collaborative": outer_executor.submit(
            _timed_call,
            server._recommendation_fetch_tracks_for_ids,
            collaborative_ids,
            _HOME_COLLAB_POOL_CAP,
        ),
        "fallback": outer_executor.submit(
            _timed_call,
            _recommendation_home_fallback_tracks,
            profile,
            limit=_HOME_FALLBACK_POOL_CAP,
        ),
        "offline_tracks": outer_executor.submit(
            _timed_call,
            server._recommendation_fetch_tracks_for_ids,
            list(profile.get("offline_track_ids") or profile.get("library_track_ids") or []),
            _HOME_HISTORY_POOL_CAP,
        ),
        "albums": outer_executor.submit(
            _timed_call,
            _build_album_items,
            server,
            profile,
        ),
        "artists": outer_executor.submit(
            _timed_call,
            _build_artist_artifacts,
            server,
            profile,
        ),
    }

    collaborative_tracks = []
    fallback_tracks = []
    offline_tracks = []
    album_items = []
    artist_items = []
    for future_name, future in fetch_futures.items():
        try:
            result, elapsed_ms = future.result()
        except Exception:
            result, elapsed_ms = [], 0
        stage_key = {
            "collaborative": "fallback_collaborative_fetch_ms",
            "fallback": "fallback_pool_fetch_ms",
            "offline_tracks": "fallback_offline_fetch_ms",
            "albums": "fallback_album_pool_ms",
            "artists": "fallback_artist_pool_ms",
        }.get(future_name)
        if stage_key:
            stage_timings[stage_key] = int(elapsed_ms or 0)
        if future_name == "collaborative":
            collaborative_tracks = list(result or [])
        elif future_name == "fallback":
            fallback_tracks = list(result or [])
        elif future_name == "offline_tracks":
            offline_tracks = list(result or [])
        elif future_name == "albums":
            album_items = list(result or [])
        elif future_name == "artists":
            artifact_payload = dict(result or {})
            artist_items = list(
                artifact_payload.get("artists")
                or _build_profile_artist_items(server, profile)
            )

    rediscovery_tracks = [
        track
        for track in top_history_tracks
        if server._recommendation_trim_text(track.get("id")) not in recent_track_ids
    ][:_HOME_HISTORY_POOL_CAP]
    exploration_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(collaborative_tracks[_HOME_COLLAB_POOL_CAP // 2:] or []),
            *(fallback_tracks[_HOME_FALLBACK_POOL_CAP // 3:] or []),
        ],
        _HOME_HISTORY_POOL_CAP,
    )

    pools: Dict[str, List[Dict[str, Any]]] = {
        "history_recent": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                recent_history_tracks,
                generator_name="history_recent",
                base_score=5.0,
                reason="From your recent listening history.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "history_top": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                top_history_tracks,
                generator_name="history_top",
                base_score=5.1,
                reason="You come back to these often.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "offline_library": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                offline_tracks,
                generator_name="offline_library",
                base_score=4.6,
                reason="Ready when you need it offline.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "collaborative": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                collaborative_tracks,
                generator_name="collaborative_home",
                base_score=4.4,
                reason="Listeners with similar taste also played this.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "artist_neighbors": [],
        "anchor_neighbors": [],
        "primary_anchor_neighbors": [],
        "taste_fallback": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                fallback_tracks,
                generator_name="taste_fallback",
                base_score=2.6,
                reason="Taste-filtered fallback while the feed warms up.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "rediscovery": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                rediscovery_tracks,
                generator_name="rediscovery_history",
                base_score=4.0,
                reason="A favorite worth bringing back.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
        "exploration": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                exploration_tracks,
                generator_name="exploration_pool",
                base_score=2.8,
                reason="A measured step outside your usual rotation.",
            ),
            limit=_HOME_POOL_CANDIDATE_CAP,
        ),
    }
    feature_pool_started_at = time.perf_counter()
    pools.update(
        _build_feature_aware_pools(
            server,
            profile,
            pools,
        )
    )
    stage_timings["feature_pool_ms"] = int(
        (time.perf_counter() - feature_pool_started_at) * 1000
    )
    for row_kind in ("quiet_picks", "deep_cuts"):
        prefiltered_candidates, _ = _combine_pools(
            server,
            {"pools": pools},
            _prefilter_pool_order(row_kind),
            limit=max(_HOME_POOL_CANDIDATE_CAP * 2, 72),
        )
        prefiltered_candidates = _post_filter_row_candidates(
            server,
            row_kind,
            profile,
            prefiltered_candidates,
        )
        if prefiltered_candidates:
            pools[_prefiltered_pool_name(row_kind)] = _trim_candidate_pool(
                server,
                prefiltered_candidates,
                limit=max(_HOME_POOL_CANDIDATE_CAP, 56),
            )
    return {
        "generated_at": time.time(),
        "build_ms": int((time.perf_counter() - started_at) * 1000),
        "pool_counts": {
            key: len(value or [])
            for key, value in pools.items()
        },
        "row_mode": "candidate_snapshot_fallback_v43",
        "albums_count": len(album_items),
        "artists_count": len(artist_items),
        "primary_anchor_id": "",
        "quiet_seed": server._recommendation_quiet_base_query(profile),
        "profile_allocator_features": build_profile_allocator_features(profile),
        "pool_allocator_features": summarize_snapshot_pool_features(
            server,
            profile=profile,
            pools=pools,
        ),
        "stage_timings_ms": {
            **stage_timings,
            "fallback_snapshot_ms": int((time.perf_counter() - started_at) * 1000),
        },
        "pools": pools,
        "albums": album_items,
        "artists": artist_items,
    }


def trim_home_candidate_snapshot(
    server: Any,
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    pools = {}
    for pool_name, candidates in dict(snapshot.get("pools") or {}).items():
        pools[pool_name] = _trim_candidate_pool(
            server,
            list(candidates or []),
            limit=_HOME_POOL_CANDIDATE_CAP,
        )
    albums = []
    for album in list(snapshot.get("albums") or [])[:_HOME_ALBUM_CAP]:
        if isinstance(album, dict):
            albums.append(dict(album))
    artists = []
    for artist in list(snapshot.get("artists") or [])[:_HOME_ARTIST_CAP]:
        if isinstance(artist, dict):
            artists.append(dict(artist))
    return {
        "generated_at": float(snapshot.get("generated_at") or 0.0),
        "build_ms": int(snapshot.get("build_ms") or 0),
        "pool_counts": {
            key: len(value or [])
            for key, value in pools.items()
        },
        "row_mode": snapshot.get("row_mode") or "candidate_snapshot_v42",
        "albums_count": len(albums),
        "artists_count": len(artists),
        "primary_anchor_id": snapshot.get("primary_anchor_id") or "",
        "quiet_seed": snapshot.get("quiet_seed") or "",
        "profile_allocator_features": dict(
            snapshot.get("profile_allocator_features") or {}
        ),
        "pool_allocator_features": {
            pool_name: dict(features or {})
            for pool_name, features in dict(
                snapshot.get("pool_allocator_features") or {}
            ).items()
        },
        "stage_timings_ms": dict(snapshot.get("stage_timings_ms") or {}),
        "pools": pools,
        "albums": albums,
        "artists": artists,
    }


def _combine_pools(
    server: Any,
    snapshot: Dict[str, Any],
    pool_names: Sequence[str],
    *,
    limit: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    pools = dict(snapshot.get("pools") or {})
    combined: List[Dict[str, Any]] = []
    source_pool_counts: Dict[str, int] = {}
    for pool_name in pool_names:
        pool = list(pools.get(pool_name) or [])
        source_pool_counts[pool_name] = len(pool)
        _extend_pool(
            server,
            combined,
            pool,
            limit=limit,
        )
        if len(combined) >= limit:
            break
    return combined[:limit], source_pool_counts


def _script_bucket(text: str) -> str:
    return script_bucket(text)


def _row_affinity_profile(server: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    cached = profile.get("_row_affinity_profile")
    if isinstance(cached, dict) and cached:
        return cached
    catalog_profile = build_catalog_feature_profile(server, profile)

    affinity = {
        "artists": set(catalog_profile.get("affinity_artists") or set()),
        "albums": set(catalog_profile.get("affinity_albums") or set()),
        "titles": set(catalog_profile.get("affinity_titles") or set()),
        "preferred_genres": set(catalog_profile.get("preferred_genres") or set()),
        "preferred_subgenres": set(catalog_profile.get("preferred_subgenres") or set()),
        "dominant_script": catalog_profile.get("dominant_script") or "latin",
        "supported_scripts": set(catalog_profile.get("supported_scripts") or {"latin"}),
        "supported_languages": set(catalog_profile.get("supported_languages") or {"english"}),
        "supported_eras": set(catalog_profile.get("supported_eras") or set()),
        "dominant_era": catalog_profile.get("dominant_era") or "",
        "supported_type_tags": set(catalog_profile.get("supported_type_tags") or set()),
        "peer_scene_keys": set(catalog_profile.get("peer_scene_keys") or set()),
    }
    profile["_row_affinity_profile"] = affinity
    return affinity


def _row_candidate_evidence(
    server: Any,
    profile: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    affinity = _row_affinity_profile(server, profile)
    track = dict(candidate.get("track") or {})
    artist_key = server._normalize_text(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    )
    album_key = server._normalize_text(track.get("album") or "")
    title_key = server._normalize_text(track.get("title") or "")
    source_names = {
        server._normalize_text(source_name)
        for source_name in [
            candidate.get("generator_name"),
            candidate.get("primary_source"),
            *(candidate.get("source_names") or []),
        ]
        if server._normalize_text(source_name)
    }
    track_text = " ".join(
        part
        for part in [
            server._recommendation_trim_text(track.get("title")),
            server._recommendation_trim_text(
                track.get("channel") or track.get("artist") or track.get("author")
            ),
            server._recommendation_trim_text(track.get("album")),
        ]
        if part
    )
    script_bucket = _script_bucket(track_text)
    artist_match = bool(artist_key and artist_key in set(affinity.get("artists") or []))
    album_match = bool(album_key and album_key in set(affinity.get("albums") or []))
    title_match = bool(title_key and title_key in set(affinity.get("titles") or []))
    affinity_score = 0.0
    if artist_match:
        affinity_score += 2.2
    if album_match:
        affinity_score += 1.35
    if title_match:
        affinity_score += 0.55

    trusted_source = any(
        token in source_name
        for source_name in source_names
        for token in (
            "collaborative",
            "peer_artist_neighbors",
            "anchor_neighbors",
            "primary_anchor_neighbors",
            "rediscovery",
            "history_",
            "offline_library",
        )
    )
    exploratory_source = any(
        token in source_name
        for source_name in source_names
        for token in (
            "exploration",
            "fallback",
        )
    )
    supported_script = (
        script_bucket == "unknown"
        or script_bucket == affinity.get("dominant_script")
        or script_bucket in set(affinity.get("supported_scripts") or set())
    )
    catalog_alignment = candidate_catalog_alignment(server, track, profile)
    catalog_score = (
        float(catalog_alignment.get("scene_affinity") or 0.0)
        + (float(catalog_alignment.get("peer_scene_bonus") or 0.0) * 0.85)
        + (float(catalog_alignment.get("genre_affinity") or 0.0) * 0.7)
        + (float(catalog_alignment.get("subgenre_affinity") or 0.0) * 0.45)
        + (float(catalog_alignment.get("era_affinity") or 0.0) * 0.7)
        + (float(catalog_alignment.get("adjacent_era_affinity") or 0.0) * 0.4)
        + (float(catalog_alignment.get("language_affinity") or 0.0) * 0.35)
        + (float(catalog_alignment.get("type_affinity") or 0.0) * 0.45)
        + (float(catalog_alignment.get("script_affinity") or 0.0) * 0.2)
    )
    return {
        "artist_match": artist_match,
        "album_match": album_match,
        "title_match": title_match,
        "affinity_score": affinity_score,
        "trusted_source": trusted_source,
        "exploratory_source": exploratory_source,
        "supported_script": supported_script,
        "script_bucket": script_bucket,
        "source_names": source_names,
        "scene_affinity": float(catalog_alignment.get("scene_affinity") or 0.0),
        "peer_scene_bonus": float(catalog_alignment.get("peer_scene_bonus") or 0.0),
        "genre_affinity": float(catalog_alignment.get("genre_affinity") or 0.0),
        "subgenre_affinity": float(catalog_alignment.get("subgenre_affinity") or 0.0),
        "era_affinity": float(catalog_alignment.get("era_affinity") or 0.0),
        "adjacent_era_affinity": float(catalog_alignment.get("adjacent_era_affinity") or 0.0),
        "language_affinity": float(catalog_alignment.get("language_affinity") or 0.0),
        "type_affinity": float(catalog_alignment.get("type_affinity") or 0.0),
        "script_affinity": float(catalog_alignment.get("script_affinity") or 0.0),
        "popularity_taste_fit": float(catalog_alignment.get("popularity_taste_fit") or 0.0),
        "novelty_tolerance_fit": float(catalog_alignment.get("novelty_tolerance_fit") or 0.0),
        "negative_feedback_penalty": float(catalog_alignment.get("negative_feedback_penalty") or 0.0),
        "catalog_score": float(catalog_score),
    }


def _post_filter_row_candidates(
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    *,
    relaxed: bool = False,
) -> List[Dict[str, Any]]:
    if row_kind not in {"quiet_picks", "deep_cuts", "trending_for_you", "rediscover"}:
        return list(candidates or [])
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_row_track_ids = _recent_row_impression_track_ids(
        server,
        profile,
        row_kind,
    )
    filtered = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        evidence = _row_candidate_evidence(server, profile, candidate)
        track_id = server._recommendation_trim_text(
            (candidate.get("track") or {}).get("id")
        )
        if evidence["negative_feedback_penalty"] >= (0.65 if relaxed else 0.4):
            continue
        if (
            row_kind in {"deep_cuts", "rediscover"}
            and track_id
            and track_id in recent_row_track_ids
        ):
            continue
        if row_kind == "quiet_picks":
            if track_id and track_id in recent_track_ids and len(filtered) >= 4:
                continue
            similarity_supported = (
                evidence["artist_match"]
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["scene_affinity"] >= (0.4 if relaxed else 0.5)
                or evidence["genre_affinity"] >= (0.48 if relaxed else 0.58)
                or evidence["subgenre_affinity"] >= (0.32 if relaxed else 0.42)
            )
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.05 if relaxed else 1.3)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.45)
                and not evidence["trusted_source"]
                and not similarity_supported
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and not evidence["trusted_source"]
                and not (evidence["artist_match"] or evidence["peer_scene_bonus"] > 0.0)
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
            ):
                continue
            if (
                not similarity_supported
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.0 if relaxed else 1.35)
                and not evidence["trusted_source"]
            ):
                continue
        elif row_kind == "rediscover":
            history_supported = any(
                token in source_name
                for source_name in evidence["source_names"]
                for token in (
                    "rediscovery",
                    "history_",
                    "primary_anchor_neighbors",
                    "anchor_neighbors",
                    "offline_library",
                )
            )
            if track_id and track_id in recent_track_ids:
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.15 if relaxed else 1.55)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.6)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.7)
                and not evidence["trusted_source"]
                and not evidence["artist_match"]
                and evidence["peer_scene_bonus"] <= 0.0
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.65)
                and not history_supported
                and not evidence["trusted_source"]
            ):
                continue
            if (
                not history_supported
                and not evidence["artist_match"]
                and evidence["peer_scene_bonus"] <= 0.0
                and evidence["scene_affinity"] < (0.4 if relaxed else 0.52)
                and evidence["genre_affinity"] < (0.45 if relaxed else 0.58)
            ):
                continue
        elif row_kind == "deep_cuts":
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.4 if relaxed else 1.8)
            ):
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.55)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.35 if relaxed else 1.8)
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.25 if relaxed else 1.7)
                and not (evidence["artist_match"] or evidence["peer_scene_bonus"] > 0.0)
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.45 if relaxed else 1.95)
                and not evidence["trusted_source"]
            ):
                continue
            if track_id and track_id in recent_track_ids and len(filtered) >= 3:
                continue
        elif row_kind == "trending_for_you":
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.35 if relaxed else 1.75)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.2 if relaxed else 1.65)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.35 if relaxed else 1.85)
                and not (evidence["artist_match"] or evidence["peer_scene_bonus"] > 0.0)
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.15 if relaxed else 1.5)
                and not evidence["trusted_source"]
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.35 if relaxed else 1.85)
                and not evidence["trusted_source"]
            ):
                continue
        filtered.append(candidate)
    if filtered:
        return filtered
    if row_kind in {"deep_cuts", "trending_for_you", "rediscover"}:
        return []
    return list(candidates or [])


def _quiet_extension_pool_names(row_seed: Dict[str, Any]) -> List[str]:
    pool_names: List[str] = []
    for pool_name in list(row_seed.get("allocator_pool_order") or []):
        normalized = str(pool_name or "").strip()
        if normalized and normalized not in pool_names:
            pool_names.append(normalized)
    for fallback_pool in _prefilter_pool_order("quiet_picks"):
        if fallback_pool not in pool_names:
            pool_names.append(fallback_pool)
    return pool_names


def _row_extension_pool_names(row_kind: str, row_seed: Dict[str, Any]) -> List[str]:
    if row_kind == "quiet_picks":
        return _quiet_extension_pool_names(row_seed)
    pool_names: List[str] = []
    for pool_name in list(row_seed.get("allocator_pool_order") or []):
        normalized = str(pool_name or "").strip()
        if normalized and normalized not in pool_names:
            pool_names.append(normalized)
    fallback_order = {
        "continue_listening": (
            "history_recent",
            "same_artist",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "collaborative",
            "history_top",
        ),
        "because_you_played": (
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "collaborative",
            "history_recent",
            "history_top",
        ),
        "trending_for_you": (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "language_safe",
            "popularity_taste",
            "collaborative",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "history_top",
            "history_recent",
        ),
        "deep_cuts": (
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "rediscovery",
            "collaborative",
            "history_top",
            "history_recent",
            "popularity_taste",
        ),
        "rediscover": (
            "rediscovery",
            "history_recent",
            "history_top",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "artist_neighbors",
            "peer_scene",
            "genre_subgenre",
            "era_neighbors",
            "collaborative",
        ),
    }.get(row_kind, ())
    for fallback_pool in fallback_order:
        if fallback_pool not in pool_names:
            pool_names.append(fallback_pool)
    return pool_names


def build_row_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    relaxed_filter: bool = False,
    pool_names_override: Sequence[str] | None = None,
    candidate_limit_override: int | None = None,
) -> Dict[str, Any] | None:
    title = row_title(row_kind, profile)
    row_started_at = time.perf_counter()
    if row_kind == "recommended_albums":
        items = list(snapshot.get("albums") or [])[:_HOME_ALBUM_CAP]
        if not items:
            return None
        return {
            "title": title,
            "kind": row_kind,
            "item_type": "album",
            "items": items[:18],
            "row_strategy": "personalized",
            "fallback_reason": "",
            "source_pool_counts": {"albums": len(items)},
            "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
        }
    if row_kind == "recommended_artists":
        items = list(snapshot.get("artists") or [])[:_HOME_ARTIST_CAP]
        if not items:
            return None
        return {
            "title": title,
            "kind": row_kind,
            "item_type": "artist",
            "items": items[:_HOME_ARTIST_CAP],
            "row_strategy": "personalized",
            "fallback_reason": "",
            "source_pool_counts": {"artists": len(items)},
            "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
        }
    allocation_plan = build_row_allocation_plan(
        server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
    )
    if not isinstance(allocation_plan, dict):
        return None
    preferred_pool_name = _prefiltered_pool_name(row_kind)
    pool_names = list(pool_names_override or allocation_plan.get("pool_names") or ())
    if row_kind == "quiet_picks" and not pool_names_override:
        pool_names = _quiet_primary_pool_order(snapshot, allocation_plan)
    if preferred_pool_name and preferred_pool_name in dict(snapshot.get("pools") or {}):
        pool_names = [
            preferred_pool_name,
            *[pool_name for pool_name in pool_names if pool_name != preferred_pool_name],
        ]
    candidate_limit = int(candidate_limit_override or allocation_plan.get("candidate_limit") or 18)
    if row_kind == "quiet_picks":
        candidate_limit = max(candidate_limit, 120)
    candidates, source_pool_counts = _combine_pools(
        server,
        snapshot,
        tuple(pool_names),
        limit=candidate_limit,
    )
    candidates = _post_filter_row_candidates(
        server,
        row_kind,
        profile,
        candidates,
        relaxed=relaxed_filter,
    )
    if not candidates:
        return None
    row_seed = {
        "title": title,
        "kind": row_kind,
        "candidates": candidates,
        "row_strategy": allocation_plan.get("row_strategy") or "fallback",
        "fallback_reason": allocation_plan.get("fallback_reason") or "",
        "source_pool_counts": source_pool_counts,
        "allocator_model": {
            "key": allocation_plan.get("model_key") or "",
            "version": allocation_plan.get("model_version") or "",
        },
        "allocator_pool_order": list(pool_names),
        "allocator_pool_scores": list(allocation_plan.get("pool_scores") or []),
        "allocator_ms": int((time.perf_counter() - row_started_at) * 1000),
    }
    if row_kind == "quiet_picks":
        row_seed["quiet_query"] = snapshot.get("quiet_seed") or ""
        row_seed["used_queries"] = []
    return row_seed


def build_required_fallback_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, Any] | None:
    row_seed = build_row_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        relaxed_filter=True,
    )
    if not isinstance(row_seed, dict):
        fallback_pool_order = {
            "trending_for_you": (
                "peer_scene",
                "genre_subgenre",
                "era_neighbors",
                "language_safe",
                "popularity_taste",
                "collaborative",
                "artist_neighbors",
                "primary_anchor_neighbors",
                "anchor_neighbors",
                "history_top",
                "history_recent",
                "taste_fallback",
                "exploration",
            ),
            "quiet_picks": (
                "quiet_prefiltered",
                "peer_scene",
                "genre_subgenre",
                "artist_neighbors",
                "primary_anchor_neighbors",
                "anchor_neighbors",
                "collaborative",
                "era_neighbors",
                "language_safe",
                "popularity_taste",
                "rediscovery",
                "history_top",
                "history_recent",
                "same_artist",
                "offline_library",
                "taste_fallback",
                "exploration",
            ),
        }.get(row_kind)
        if fallback_pool_order:
            fallback_limit = max(
                32,
                int((snapshot.get("pool_counts") or {}).get("history_top") or 0),
                24,
            )
            row_seed = build_row_seed(
                server=server,
                row_kind=row_kind,
                profile=profile,
                snapshot=snapshot,
                relaxed_filter=True,
                pool_names_override=fallback_pool_order,
                candidate_limit_override=fallback_limit,
            )
    if not isinstance(row_seed, dict):
        return None
    return apply_required_row_fallback_policy(row_seed)


def finalize_row_items(
    *,
    server: Any,
    row_kind: str,
    title: str,
    candidates: Iterable[Dict[str, Any]],
    profile: Dict[str, Any],
    used_track_ids: set[str],
    used_artist_counts: Dict[str, int] | None = None,
    enforce_feed_artist_cap: bool = True,
    max_items: int = 18,
    embedding_lookup: Dict[str, List[float]] | None = None,
    metadata_enrich_limit: int | None = None,
) -> Dict[str, Any] | None:
    if not candidates:
        return None

    input_count = len([candidate for candidate in candidates if isinstance(candidate, dict)])
    source_counts = defaultdict(int)
    merged_candidates = {}
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        raw_track = raw_candidate.get("track") if isinstance(raw_candidate.get("track"), dict) else raw_candidate
        normalized_track = server.normalize_recommendation_track(raw_track)
        if normalized_track is None:
            continue
        if isinstance(raw_track, dict):
            normalized_track = server._merge_track_metadata(raw_track, normalized_track)
        track_signature = server._recommendation_track_signature(normalized_track)
        if not track_signature:
            continue
        source_name = (
            server._recommendation_trim_text(
                raw_candidate.get("generator_name")
                or (raw_track.get("generator_name") if isinstance(raw_track, dict) else "")
            )
            or "candidate_pool"
        )
        source_score = float(
            raw_candidate.get("generator_score")
            or (raw_track.get("generator_score") if isinstance(raw_track, dict) else 0.0)
            or 0.0
        )
        reason = server._recommendation_trim_text(
            raw_candidate.get("reason")
            or (raw_track.get("recommendation_reason") if isinstance(raw_track, dict) else "")
        )
        source_counts[source_name] += 1
        current = merged_candidates.get(track_signature)
        if current is None:
            merged_candidates[track_signature] = {
                "track": normalized_track,
                "source_score": source_score,
                "source_votes": 1,
                "primary_source": source_name,
                "source_names": {source_name},
                "reasons": [reason] if reason else [],
            }
            continue
        current["source_votes"] = int(current.get("source_votes") or 0) + 1
        current["source_names"].add(source_name)
        if source_score > float(current.get("source_score") or 0.0):
            current["source_score"] = source_score
            current["primary_source"] = source_name
        if reason and reason not in current["reasons"] and len(current["reasons"]) < 4:
            current["reasons"].append(reason)
        if server._track_metadata_incomplete(current.get("track")):
            current["track"] = server._merge_track_metadata(current["track"], normalized_track)

    if not merged_candidates:
        return None

    candidate_embeddings = dict(embedding_lookup or {})
    missing_embedding_tracks = []
    for candidate in merged_candidates.values():
        track = candidate.get("track")
        candidate_key = server._recommendation_track_embedding_key(track)
        if not candidate_key:
            continue
        if candidate_key not in candidate_embeddings:
            missing_embedding_tracks.append(track)
    if missing_embedding_tracks:
        candidate_embeddings.update(
            server._recommendation_track_embeddings(missing_embedding_tracks)
        )
    ranked = []
    model_key = "home_global_ranker_v4"
    model_version = server._ranking_model_version(model_key)
    for candidate in merged_candidates.values():
        candidate_key = server._recommendation_track_embedding_key(candidate.get("track"))
        candidate_vector = candidate_embeddings.get(candidate_key) or []
        score_payload = track_score(
            server,
            candidate,
            profile,
            row_kind,
            candidate_vector,
        )
        candidate_score = float(score_payload.get("score") or 0.0)
        source_names = candidate.get("source_names") or set()
        query_derived_votes = sum(
            1 for source_name in source_names
            if is_query_derived_source(server, source_name)
        )
        if query_derived_votes > 0:
            candidate_score -= min(0.35 * query_derived_votes, 0.9)
        ranked.append((candidate_score, candidate, score_payload))
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = []
    artist_counts = {}
    global_artist_counts = used_artist_counts if isinstance(used_artist_counts, dict) else {}
    query_derived_selected = 0
    quality_floor_value = quality_floor(row_kind)
    max_same_artist_value = max_same_artist(row_kind)
    max_feed_same_artist_value = max_feed_same_artist(row_kind)
    minimum_items = row_min_items(row_kind)
    query_derived_limit = min(
        server.RECOMMENDATION_QUERY_DERIVED_SOURCE_ITEM_CAP,
        max(2, int(max_items * server.RECOMMENDATION_QUERY_DERIVED_SOURCE_SHARE_CAP)),
    )

    for candidate_score, candidate, score_payload in ranked:
        if candidate_score < quality_floor_value:
            continue
        track = dict(candidate["track"])
        track_key = server._recommendation_track_signature(track)
        if not track_key or track_key in used_track_ids:
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        feed_count = 0
        if artist_key:
            current_count = artist_counts.get(artist_key, 0)
            if current_count >= max_same_artist_value and len(selected) + 1 < max_items:
                continue
            feed_count = int(global_artist_counts.get(artist_key) or 0)
            if (
                enforce_feed_artist_cap
                and
                feed_count >= max_feed_same_artist_value
                and len(selected) + 1 < max_items
            ):
                continue
        source_names = sorted(candidate.get("source_names") or [])
        is_query_derived = any(
            is_query_derived_source(server, source_name)
            for source_name in source_names
        )
        if (
            is_query_derived
            and query_derived_selected >= query_derived_limit
            and len(selected) + 1 < max_items
        ):
            continue
        if is_query_derived:
            query_derived_selected += 1
        track["generator_score"] = round(candidate_score, 3)
        track["ml_similarities"] = dict(score_payload.get("ml_similarities") or {})
        track["ranking_features"] = {
            key: round(float(value), 4)
            for key, value in (score_payload.get("ranking_features") or {}).items()
        }
        track["recommendation_source"] = candidate.get("primary_source") or ""
        track["recommendation_sources"] = source_names
        if candidate.get("reasons"):
            track["recommendation_reason"] = candidate["reasons"][0]
        track["ranking_model"] = {
            "key": score_payload.get("model_key") or model_key,
            "version": score_payload.get("model_version") or model_version,
        }
        selected.append(track)
        if artist_key:
            artist_counts[artist_key] = int(artist_counts.get(artist_key) or 0) + 1
            global_artist_counts[artist_key] = feed_count + 1
        used_track_ids.add(track_key)
        if len(selected) >= max_items:
            break

    if len(selected) < minimum_items:
        return None

    incomplete_indexes = [
        index
        for index, track in enumerate(selected)
        if server._track_metadata_incomplete(track)
    ]
    if incomplete_indexes and metadata_enrich_limit != 0:
        if metadata_enrich_limit is not None and metadata_enrich_limit > 0:
            incomplete_indexes = incomplete_indexes[:metadata_enrich_limit]
        futures = {
            index: server.recommendation_executor.submit(
                server._recommendation_enrich_track_metadata,
                selected[index],
            )
            for index in incomplete_indexes
        }
        for index, future in futures.items():
            try:
                enriched = future.result(
                    timeout=server.RECOMMENDATION_METADATA_ENRICH_PER_TRACK_TIMEOUT_SECONDS
                )
            except Exception:
                enriched = None
            if enriched is not None:
                preserved = dict(selected[index])
                merged = server._merge_track_metadata(preserved, enriched)
                for key in (
                    "generator_score",
                    "ml_similarities",
                    "ranking_features",
                    "recommendation_source",
                    "recommendation_sources",
                    "recommendation_reason",
                    "ranking_model",
                ):
                    if key in preserved:
                        merged[key] = preserved[key]
                selected[index] = merged

    selected_source_counts = defaultdict(int)
    for track in selected:
        for source_name in track.get("recommendation_sources") or []:
            selected_source_counts[source_name] += 1

    row_feature_mix_totals: Dict[str, float] = defaultdict(float)
    row_feature_mix_counts: Dict[str, int] = defaultdict(int)
    for track in selected:
        for feature_name, feature_value in dict(track.get("ranking_features") or {}).items():
            row_feature_mix_totals[feature_name] += float(feature_value or 0.0)
            row_feature_mix_counts[feature_name] += 1
    row_feature_mix = {
        feature_name: round(
            row_feature_mix_totals[feature_name] / max(row_feature_mix_counts[feature_name], 1),
            4,
        )
        for feature_name in row_feature_mix_totals.keys()
        if row_feature_mix_counts.get(feature_name)
    }

    return {
        "id": row_kind,
        "kind": row_kind,
        "title": title,
        "items": selected,
        "_diagnostics": {
            "model_key": model_key,
            "model_version": model_version,
            "quality_floor": round(float(quality_floor_value), 4),
            "candidate_count_input": input_count,
            "candidate_count_merged": len(merged_candidates),
            "selected_count": len(selected),
            "source_counts": dict(source_counts),
            "selected_source_counts": dict(selected_source_counts),
            "row_feature_mix": row_feature_mix,
        },
    }


def apply_track_row_runtime_fields(
    *,
    server: Any,
    finalized: Dict[str, Any],
    row_seed: Dict[str, Any],
) -> Dict[str, Any]:
    updated = dict(finalized or {})
    row_kind = server._recommendation_trim_text(
        row_seed.get("kind") or updated.get("kind")
    )
    candidate_count = len(row_seed.get("candidates") or [])
    item_count = len(updated.get("items") or [])
    updated["extension_cycle"] = 0
    updated["can_extend"] = candidate_count > item_count or row_kind in {
        "continue_listening",
        "because_you_played",
        "trending_for_you",
        "quiet_picks",
        "deep_cuts",
        "rediscover",
    }
    updated["used_signatures"] = [
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in (updated.get("items") or [])
        )
        if signature
    ]
    return updated


def apply_quiet_row_runtime_fields(
    *,
    server: Any,
    finalized: Dict[str, Any],
    row_seed: Dict[str, Any],
) -> Dict[str, Any]:
    updated = apply_track_row_runtime_fields(
        server=server,
        finalized=finalized,
        row_seed=row_seed,
    )
    updated["base_query"] = server._recommendation_trim_text(
        row_seed.get("quiet_query")
    )
    initial_used_queries = [
        query
        for query in (row_seed.get("used_queries") or [])
        if server._recommendation_trim_text(query)
    ]
    if not initial_used_queries:
        quiet_query = server._recommendation_trim_text(row_seed.get("quiet_query"))
        initial_used_queries = [quiet_query] if quiet_query else []
    updated["used_queries"] = initial_used_queries
    return updated


def extend_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return row
    extended_row = dict(row)
    row_kind = server._recommendation_trim_text(extended_row.get("kind"))
    item_type = server._recommendation_trim_text(
        extended_row.get("item_type") or "track"
    )
    if item_type and item_type != "track":
        return extended_row
    row_seed = build_row_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        candidate_limit_override=max(
            len(extended_row.get("items") or []) + (page_size * 10),
            96,
        ),
    )
    if not isinstance(row_seed, dict):
        extended_row["can_extend"] = False
        return extended_row

    existing_items = list(extended_row.get("items") or [])
    existing_signatures = {
        signature
        for signature in (
            server._recommendation_track_signature(track)
            for track in existing_items
        )
        if signature
    }
    existing_signatures.update(
        signature
        for signature in (extended_row.get("used_signatures") or [])
        if server._recommendation_trim_text(signature)
    )
    extension_candidates, _source_pool_counts = _combine_pools(
        server,
        snapshot,
        _row_extension_pool_names(row_kind, row_seed),
        limit=max(len(existing_items) + (page_size * 8), 96),
    )
    extension_candidates = _post_filter_row_candidates(
        server,
        row_kind,
        profile,
        extension_candidates,
    )
    target_bank_size = max(
        len(existing_items) + (page_size * 4),
        64 if row_kind == "quiet_picks" else 40,
    )
    target_new_items = max(target_bank_size - len(existing_items), page_size * 3, 18)
    existing_artist_counts: Dict[str, int] = defaultdict(int)
    for track in existing_items:
        if not isinstance(track, dict):
            continue
        artist_key = server._normalize_text(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        if artist_key:
            existing_artist_counts[artist_key] += 1
    finalized = finalize_row_items(
        server=server,
        row_kind=row_kind,
        title=extended_row.get("title") or row_seed.get("title") or row_title(row_kind, profile),
        candidates=extension_candidates,
        profile=profile,
        used_track_ids=set(existing_signatures),
        used_artist_counts=dict(existing_artist_counts),
        max_items=target_new_items,
    )
    new_items = []
    for track in (finalized or {}).get("items") or []:
        signature = server._recommendation_track_signature(track)
        if not signature or signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        new_items.append(track)
    extended_row["used_signatures"] = list(existing_signatures)
    extended_row["used_queries"] = []
    extended_row["extension_cycle"] = max(int(extended_row.get("extension_cycle") or 0), 0) + 1
    if new_items:
        extended_row["items"] = existing_items + new_items
    else:
        extended_row["can_extend"] = False
        return extended_row
    remaining_available = 0
    for candidate in extension_candidates:
        signature = _candidate_signature(server, candidate)
        if signature and signature not in existing_signatures:
            remaining_available += 1
    extended_row["can_extend"] = remaining_available > 0
    return extended_row


def extend_quiet_row_from_snapshot(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    page_size: int = 10,
) -> Dict[str, Any]:
    return extend_row_from_snapshot(
        server=server,
        row=row,
        profile=profile,
        snapshot=snapshot,
        page_size=page_size,
    )
