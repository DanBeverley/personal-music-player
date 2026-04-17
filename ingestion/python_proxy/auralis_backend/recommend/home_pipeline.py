from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import os
import time

from ..domain.artist_recommendations import ArtistRecommendationService
from ..domain.catalog import normalize_artist_name, normalize_track_title
from .allocator import (
    build_profile_allocator_features,
    build_row_allocation_plan,
    summarize_snapshot_pool_features,
)
from .candidate_snapshot_stage import (
    assemble_snapshot_payload,
    prepare_snapshot_inputs,
    resolve_snapshot_fetches,
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
from .row_seed_dispatch import (
    build_allocated_row_seed,
    build_required_fallback_seed_with_overrides,
    build_specialized_row_seed,
)
from .specialized_row_builders import (
    build_continue_listening_seed as build_continue_listening_seed_stage,
    build_mixed_for_you_seed as build_mixed_for_you_seed_stage,
    build_todays_pick_seed as build_todays_pick_seed_stage,
    build_trending_by_genre_seed as build_trending_by_genre_seed_stage,
    build_trending_genre_tabs as build_trending_genre_tabs_stage,
    refresh_trending_by_genre_row_builder,
)
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
_HOME_TODAYS_PICK_CANDIDATE_CAP = max(
    24,
    int(os.environ.get("AURALIS_HOME_TODAYS_PICK_CANDIDATE_CAP", "64")),
)
_HOME_LAUNCH_ROW_CANDIDATE_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_LAUNCH_ROW_CANDIDATE_CAP", "28")),
)
_HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP", "24")),
)
_HOME_LAUNCH_MIX_TRACK_CAP = max(
    6,
    int(os.environ.get("AURALIS_HOME_LAUNCH_MIX_TRACK_CAP", "10")),
)
_HOME_MIX_TRACK_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_MIX_TRACK_CAP", "12")),
)
_HOME_MIX_MIN_COUNT = max(
    2,
    int(os.environ.get("AURALIS_HOME_MIX_MIN_COUNT", "2")),
)
_HOME_MIX_MAX_COUNT = max(
    _HOME_MIX_MIN_COUNT,
    min(6, int(os.environ.get("AURALIS_HOME_MIX_MAX_COUNT", "5"))),
)
_HOME_GENRE_TAB_LIMIT = max(
    3,
    min(6, int(os.environ.get("AURALIS_HOME_GENRE_TAB_LIMIT", "5"))),
)
_HOME_GENRE_TRACK_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_GENRE_TRACK_CAP", "8")),
)
_HOME_GENRE_CANDIDATE_CAP = max(
    72,
    int(os.environ.get("AURALIS_HOME_GENRE_CANDIDATE_CAP", "160")),
)
_ROW_TRACK_PAGE_SIZE = max(
    4,
    int(os.environ.get("AURALIS_ROW_TRACK_PAGE_SIZE", "4")),
)
_UNOFFICIAL_ARTIST_TOKEN_RE = re.compile(
    r"\b(tribute|karaoke|cover|covers|revival|experience|orchestra|ensemble|project)\b",
    re.IGNORECASE,
)
_UNOFFICIAL_TRACK_TOKEN_RE = re.compile(
    r"\b(tribute|karaoke|cover|instrumental|soundalike)\b",
    re.IGNORECASE,
)

_HOME_MIX_ACCENTS: Tuple[str, ...] = (
    "#7C69FF",
    "#4B89FF",
    "#F2EEE6",
    "#59B38C",
    "#E7A64A",
    "#C86B6B",
)

_ARTIST_RECOMMENDATION_SERVICE = ArtistRecommendationService()


def _artist_recommendation_service(server: Any) -> ArtistRecommendationService:
    _ARTIST_RECOMMENDATION_SERVICE._server = server
    return _ARTIST_RECOMMENDATION_SERVICE


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
        payload = _artist_recommendation_service(server).recommend(
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
                server=server,
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


def build_home_candidate_snapshot(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    preparation = prepare_snapshot_inputs(
        server=server,
        profile=profile,
        started_at=started_at,
        anchor_limit=_HOME_ANCHOR_LIMIT,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
    )
    fetched = resolve_snapshot_fetches(
        server=server,
        profile=profile,
        preparation=preparation,
        collab_pool_cap=_HOME_COLLAB_POOL_CAP,
        fallback_pool_cap=_HOME_FALLBACK_POOL_CAP,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
        timed_call_fn=_timed_call,
        home_fallback_tracks_fn=_recommendation_home_fallback_tracks,
        build_artist_artifacts_fn=_build_artist_artifacts,
        fetch_anchor_candidate_pools_fn=_fetch_anchor_candidate_pools,
        build_album_items_fn=_build_album_items,
    )
    return assemble_snapshot_payload(
        server=server,
        profile=profile,
        started_at=started_at,
        preparation=preparation,
        fetched=fetched,
        history_pool_cap=_HOME_HISTORY_POOL_CAP,
        home_fallback_pool_cap=_HOME_FALLBACK_POOL_CAP,
        home_pool_candidate_cap=_HOME_POOL_CANDIDATE_CAP,
        track_list_to_candidates_fn=_track_list_to_candidates,
        trim_candidate_pool_fn=_trim_candidate_pool,
        build_feature_aware_pools_fn=_build_feature_aware_pools,
        combine_pools_fn=_combine_pools,
        prefilter_pool_order_fn=_prefilter_pool_order,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
        prefiltered_pool_name_fn=_prefiltered_pool_name,
        build_profile_allocator_features_fn=build_profile_allocator_features,
        summarize_snapshot_pool_features_fn=summarize_snapshot_pool_features,
    )


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


def build_home_candidate_snapshot_fast_fallback(
    *,
    server: Any,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    recent_track_ids = set(profile.get("recent_track_ids") or [])
    recent_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        max(12, _HOME_HISTORY_POOL_CAP // 2),
    )
    top_history_tracks = server._recommendation_unique_snapshot_tracks(
        [
            *(profile.get("top_track_snapshots") or []),
            *(profile.get("last_played_tracks") or []),
            *(profile.get("recent_track_snapshots") or []),
        ],
        max(12, _HOME_HISTORY_POOL_CAP // 2),
    )
    rediscovery_tracks = [
        track
        for track in top_history_tracks
        if server._recommendation_trim_text(track.get("id")) not in recent_track_ids
    ][: max(8, _HOME_HISTORY_POOL_CAP // 2)]
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
            limit=max(16, _HOME_POOL_CANDIDATE_CAP // 2),
        ),
        "history_top": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                top_history_tracks,
                generator_name="history_top",
                base_score=4.8,
                reason="You come back to these often.",
            ),
            limit=max(16, _HOME_POOL_CANDIDATE_CAP // 2),
        ),
        "rediscovery": _trim_candidate_pool(
            server,
            _track_list_to_candidates(
                server,
                rediscovery_tracks,
                generator_name="rediscovery_history",
                base_score=3.9,
                reason="A favorite worth bringing back.",
            ),
            limit=max(12, _HOME_POOL_CANDIDATE_CAP // 3),
        ),
        "offline_library": [],
        "collaborative": [],
        "artist_neighbors": [],
        "anchor_neighbors": [],
        "primary_anchor_neighbors": [],
        "taste_fallback": [],
        "exploration": [],
    }
    return {
        "generated_at": time.time(),
        "build_ms": int((time.perf_counter() - started_at) * 1000),
        "pool_counts": {
            key: len(value or [])
            for key, value in pools.items()
        },
        "row_mode": "candidate_snapshot_launch_fast_v1",
        "albums_count": 0,
        "artists_count": 0,
        "primary_anchor_id": "",
        "quiet_seed": "",
        "profile_allocator_features": build_profile_allocator_features(profile),
        "pool_allocator_features": summarize_snapshot_pool_features(
            server,
            profile=profile,
            pools=pools,
        ),
        "stage_timings_ms": {
            "launch_fast_snapshot_ms": int((time.perf_counter() - started_at) * 1000),
        },
        "pools": pools,
        "albums": [],
        "artists": [],
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
        "genre_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("genre_scores") or {}).items()
            if str(key or "").strip()
        },
        "subgenre_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("subgenre_scores") or {}).items()
            if str(key or "").strip()
        },
        "era_scores": {
            str(key): float(value or 0.0)
            for key, value in dict(catalog_profile.get("era_scores") or {}).items()
            if str(key or "").strip()
        },
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
            trend_supported = (
                evidence["artist_match"]
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["scene_affinity"] >= (0.34 if relaxed else 0.46)
                or evidence["genre_affinity"] >= (0.4 if relaxed else 0.52)
                or evidence["subgenre_affinity"] >= (0.26 if relaxed else 0.36)
            )
            if (
                not evidence["supported_script"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.15 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                evidence["exploratory_source"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.0 if relaxed else 1.45)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                "exploration_pool" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.55)
                and not trend_supported
            ):
                continue
            if (
                "taste_fallback" in evidence["source_names"]
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (0.95 if relaxed else 1.3)
                and not evidence["trusted_source"]
                and not trend_supported
            ):
                continue
            if (
                evidence["language_affinity"] <= 0.0
                and (evidence["affinity_score"] + evidence["catalog_score"]) < (1.1 if relaxed else 1.55)
                and not evidence["trusted_source"]
                and not trend_supported
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


def _display_token(value: str) -> str:
    normalized = re.sub(r"[_\-]+", " ", str(value or "").strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""
    return normalized.title()


def _genre_tab_identifier(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(label or "").strip().lower()).strip("_")


def _trending_primary_pool_order(
    snapshot: Dict[str, Any],
    allocation_plan: Dict[str, Any] | None = None,
) -> List[str]:
    return _merge_pool_order(
        (
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
            "exploration",
        ),
        list((allocation_plan or {}).get("pool_names") or ()),
        dict(snapshot.get("pools") or {}),
    )


def _sorted_display_tokens(values: Iterable[str], *, limit: int) -> List[str]:
    output: List[str] = []
    seen = set()
    for raw_value in sorted(
        {
            _display_token(str(value or ""))
            for value in values or []
            if str(value or "").strip()
        }
    ):
        normalized = raw_value.lower()
        if not raw_value or normalized in seen:
            continue
        seen.add(normalized)
        output.append(raw_value)
        if len(output) >= limit:
            break
    return output


def _trending_taste_facets(server: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    affinity = _row_affinity_profile(server, profile)
    facets: List[Dict[str, Any]] = []
    seen_ids = set()

    def add_facet(kind: str, label: str, score: float) -> None:
        display = _display_token(label)
        if not display:
            return
        facet_id = f"{kind}:{_genre_tab_identifier(display)}"
        if not facet_id or facet_id in seen_ids:
            return
        seen_ids.add(facet_id)
        facets.append(
            {
                "id": facet_id,
                "kind": kind,
                "label": display,
                "score": float(score),
            }
        )

    def add_ranked_facets(
        kind: str,
        scores: Dict[str, Any] | None,
        *,
        limit: int,
        threshold: float,
        base_score: float,
    ) -> None:
        ordered = sorted(
            [
                (str(key or "").strip(), float(value or 0.0))
                for key, value in dict(scores or {}).items()
                if str(key or "").strip()
            ],
            key=lambda item: (-item[1], item[0]),
        )
        for label, score in ordered[: max(limit * 2, limit)]:
            if score < threshold:
                continue
            add_facet(kind, label, base_score + score)
            if len([facet for facet in facets if str(facet.get("kind") or "") == kind]) >= limit:
                break

    add_ranked_facets(
        "genre",
        affinity.get("genre_scores") or {},
        limit=5,
        threshold=0.06,
        base_score=3.0,
    )
    add_ranked_facets(
        "subgenre",
        affinity.get("subgenre_scores") or {},
        limit=4,
        threshold=0.05,
        base_score=2.6,
    )
    add_ranked_facets(
        "era",
        affinity.get("era_scores") or {},
        limit=4,
        threshold=0.06,
        base_score=2.1,
    )
    for genre_name in _sorted_display_tokens(
        affinity.get("preferred_genres") or set(),
        limit=5,
    ):
        add_facet("genre", genre_name, 2.9)
    for subgenre_name in _sorted_display_tokens(
        affinity.get("preferred_subgenres") or set(),
        limit=4,
    ):
        add_facet("subgenre", subgenre_name, 2.55)
    dominant_era = _display_token(str(affinity.get("dominant_era") or ""))
    if dominant_era:
        add_facet("era", dominant_era, 2.15)
    for era_name in _sorted_display_tokens(
        affinity.get("supported_eras") or set(),
        limit=4,
    ):
        add_facet("era", era_name, 1.95)
    return sorted(facets, key=lambda facet: (-float(facet.get("score") or 0.0), str(facet.get("label") or "")))


def _trending_facet_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    facet: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    facet_kind = str(facet.get("kind") or "").strip()
    facet_label = str(facet.get("label") or "").strip()
    facet_key = server._normalize_text(facet_label)
    if not facet_kind or not facet_key:
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        track = copied.get("track") if isinstance(copied.get("track"), dict) else {}
        if not track:
            continue
        alignment = candidate_catalog_alignment(server, track, profile)
        evidence = _row_candidate_evidence(server, profile, copied)
        match_score = 0.0
        if facet_kind == "genre":
            primary = server._normalize_text(alignment.get("primary_genre") or "")
            secondary = {
                server._normalize_text(value)
                for value in list(alignment.get("secondary_genres") or [])[:4]
            }
            if primary == facet_key:
                match_score = 2.4
            elif facet_key in secondary:
                match_score = 1.7
            else:
                continue
            match_score += float(alignment.get("genre_affinity") or 0.0) * 1.0
        elif facet_kind == "subgenre":
            subgenre = server._normalize_text(alignment.get("subgenre") or "")
            if subgenre != facet_key:
                continue
            match_score = 2.2 + float(alignment.get("subgenre_affinity") or 0.0) * 1.1
        elif facet_kind == "era":
            era_bucket = server._normalize_text(alignment.get("era_bucket") or "")
            if era_bucket != facet_key:
                continue
            match_score = (
                2.0
                + float(alignment.get("era_affinity") or 0.0) * 1.0
                + float(alignment.get("adjacent_era_affinity") or 0.0) * 0.5
            )
        else:
            continue
        match_score += float(evidence.get("scene_affinity") or 0.0) * 0.55
        match_score += float(evidence.get("peer_scene_bonus") or 0.0) * 0.45
        match_score += float(copied.get("source_score") or copied.get("generator_score") or 0.0) * 0.06
        scored.append((match_score, copied))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _score, candidate in scored]


def _track_artist_label(track: Dict[str, Any]) -> str:
    return str(
        track.get("channel") or track.get("artist") or track.get("author") or ""
    ).strip()


def _artist_family_identity(value: Any) -> str:
    normalized = normalize_artist_name(value)
    if not normalized:
        return ""
    family = _UNOFFICIAL_ARTIST_TOKEN_RE.sub(" ", normalized)
    family = re.sub(r"\bband\b$", " ", family).strip()
    family = re.sub(r"\s+", " ", family).strip()
    return family or normalized


def _track_authenticity_penalty(track: Dict[str, Any]) -> float:
    artist_name = _track_artist_label(track)
    title = str(track.get("title") or track.get("name") or "").strip()
    artist_normalized = normalize_artist_name(artist_name)
    title_normalized = normalize_track_title(title)
    penalty = 0.0
    if _UNOFFICIAL_ARTIST_TOKEN_RE.search(artist_normalized):
        penalty += 1.3
    if _UNOFFICIAL_TRACK_TOKEN_RE.search(title_normalized):
        penalty += 0.25
    return penalty


def _artist_family_caps(
    row_kind: str,
    *,
    max_same_artist_value: int,
    max_feed_same_artist_value: int,
) -> Tuple[int, int]:
    if row_kind in {"because_you_played", "rediscover", "trending_by_genre", "mixed_for_you"}:
        return (1, 1)
    return (
        max(1, int(max_same_artist_value or 1)),
        max(1, int(max_feed_same_artist_value or 1)),
    )


def _mix_artist_line(tracks: Sequence[Dict[str, Any]], *, limit: int = 3) -> str:
    artist_names: List[str] = []
    seen = set()
    for track in tracks or []:
        artist_name = _track_artist_label(track)
        normalized = artist_name.lower()
        if not artist_name or normalized in seen:
            continue
        seen.add(normalized)
        artist_names.append(artist_name)
        if len(artist_names) >= limit:
            break
    if not artist_names:
        return "Picked from the lane your listening is leaning toward."
    return ", ".join(artist_names)


def _theme_accent(seed: str, palette: Sequence[str]) -> str:
    if not palette:
        return "#5B6770"
    return list(palette)[sum(ord(char) for char in str(seed or "")) % len(palette)]


def _mix_rotation_seed(profile: Dict[str, Any]) -> int:
    payload = (
        f"{profile.get('profile_key') or profile.get('user_scope_id') or 'guest'}|"
        f"{time.strftime('%Y-%m-%d', time.gmtime())}"
    )
    return int(hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8], 16)


def _mix_anchor_artists(server: Any, profile: Dict[str, Any], *, limit: int = 5) -> List[str]:
    artist_names: List[str] = []
    seen = set()
    sources: List[str] = []
    for track in [
        *(profile.get("last_played_tracks") or []),
        *(profile.get("recent_track_snapshots") or []),
        *(profile.get("top_track_snapshots") or []),
    ]:
        if not isinstance(track, dict):
            continue
        artist_name = _track_artist_label(track)
        if artist_name:
            sources.append(artist_name)
    sources.extend(list(profile.get("top_artists") or []))
    sources.extend(list(profile.get("artist_hints") or []))
    sources.extend(list(profile.get("listened_artists") or []))
    for raw_name in sources:
        artist_name = str(raw_name or "").strip()
        normalized = server._normalize_text(artist_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        artist_names.append(artist_name)
        if len(artist_names) >= limit:
            break
    return artist_names


def _mix_anchor_genres(server: Any, profile: Dict[str, Any], *, limit: int = 4) -> List[str]:
    preferred = [
        _display_token(value)
        for value in list((_row_affinity_profile(server, profile).get("preferred_genres") or []))
    ]
    genres: List[str] = []
    seen = set()
    for genre_name in preferred:
        normalized = server._normalize_text(genre_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        genres.append(genre_name)
        if len(genres) >= limit:
            break
    return genres


def _mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    blueprints: List[Dict[str, Any]] = []
    for artist_name in _mix_anchor_artists(server, profile):
        normalized = server._normalize_text(artist_name)
        blueprints.append(
            {
                "id": f"artist:{normalized}",
                "anchor_type": "artist",
                "label": artist_name,
                "ranking_row_kind": "because_you_played",
                "pool_names": (
                    "history_recent",
                    "history_top",
                    "artist_neighbors",
                    "primary_anchor_neighbors",
                    "anchor_neighbors",
                    "peer_scene",
                    "genre_subgenre",
                    "collaborative",
                ),
                "accent_seed": artist_name,
            }
        )
    for genre_name in _mix_anchor_genres(server, profile):
        normalized = server._normalize_text(genre_name)
        blueprints.append(
            {
                "id": f"genre:{normalized}",
                "anchor_type": "genre",
                "label": genre_name,
                "ranking_row_kind": "trending_for_you",
                "pool_names": (
                    "peer_scene",
                    "genre_subgenre",
                    "popularity_taste",
                    "collaborative",
                    "artist_neighbors",
                    "exploration",
                ),
                "accent_seed": genre_name,
            }
        )
    return blueprints


def _select_mix_blueprints(
    server: Any,
    profile: Dict[str, Any],
    blueprints: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    available = [dict(blueprint) for blueprint in list(blueprints or []) if isinstance(blueprint, dict)]
    if not available:
        return []
    desired_count = min(len(available), _HOME_MIX_MAX_COUNT)
    if len(available) > _HOME_MIX_MIN_COUNT:
        rotation_seed = _mix_rotation_seed(profile)
        desired_count = min(
            len(available),
            _HOME_MIX_MIN_COUNT + (rotation_seed % max(1, (_HOME_MIX_MAX_COUNT - _HOME_MIX_MIN_COUNT + 1))),
        )
    desired_count = max(min(desired_count, len(available)), min(_HOME_MIX_MIN_COUNT, len(available)))
    rotation_offset = _mix_rotation_seed(profile) % len(available)
    rotated = available[rotation_offset:] + available[:rotation_offset]
    selected: List[Dict[str, Any]] = []
    used_labels = set()
    for blueprint in rotated:
        label_key = server._normalize_text(blueprint.get("label") or "")
        if label_key and label_key in used_labels:
            continue
        if label_key:
            used_labels.add(label_key)
        selected.append(blueprint)
        if len(selected) >= desired_count:
            break
    return selected


def _mix_blueprint_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    blueprint: Dict[str, Any],
) -> List[Dict[str, Any]]:
    ranking_row_kind = str(blueprint.get("ranking_row_kind") or "trending_for_you")
    anchor_type = str(blueprint.get("anchor_type") or "").strip()
    anchor_label = str(blueprint.get("label") or "").strip()
    anchor_key = server._normalize_text(anchor_label)
    base_candidates = _custom_row_candidates(
        server=server,
        profile=profile,
        snapshot=snapshot,
        row_kind=ranking_row_kind,
        pool_names=tuple(blueprint.get("pool_names") or ()),
        limit=96,
        relaxed=False,
    )
    if not base_candidates:
        return []
    strict: List[Dict[str, Any]] = []
    supportive: List[Dict[str, Any]] = []
    seen = set()
    for candidate in base_candidates:
        if not isinstance(candidate, dict):
            continue
        copied = _candidate_copy(candidate)
        signature = _candidate_signature(server, copied)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        track = copied.get("track") if isinstance(copied.get("track"), dict) else {}
        evidence = _row_candidate_evidence(server, profile, copied)
        if anchor_type == "artist":
            track_artist = server._normalize_text(_track_artist_label(track))
            source_names = set(evidence.get("source_names") or set())
            artist_neighbor_like = any(
                token in source_name
                for source_name in source_names
                for token in ("same_artist", "artist_neighbors", "primary_anchor_neighbors", "anchor_neighbors")
            )
            if track_artist and track_artist == anchor_key:
                strict.append(copied)
            elif artist_neighbor_like and (
                evidence["scene_affinity"] >= 0.35
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.42
            ):
                strict.append(copied)
            elif (
                evidence["scene_affinity"] >= 0.48
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.58
            ):
                supportive.append(copied)
        elif anchor_type == "genre":
            alignment = candidate_catalog_alignment(server, track, profile)
            genre_tokens = {
                server._normalize_text(alignment.get("primary_genre") or ""),
                *[
                    server._normalize_text(value)
                    for value in list(alignment.get("secondary_genres") or [])[:3]
                ],
            }
            genre_tokens.discard("")
            if anchor_key and anchor_key in genre_tokens:
                strict.append(copied)
            elif (
                evidence["scene_affinity"] >= 0.55
                or evidence["peer_scene_bonus"] > 0.0
                or evidence["genre_affinity"] >= 0.62
                or evidence["popularity_taste_fit"] >= 0.65
            ):
                supportive.append(copied)
        else:
            strict.append(copied)
    combined = strict + supportive
    return combined[:96]


def _custom_row_candidates(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    row_kind: str,
    pool_names: Sequence[str],
    limit: int,
    relaxed: bool = False,
) -> List[Dict[str, Any]]:
    candidates, _ = _combine_pools(
        server,
        snapshot,
        tuple(pool_names),
        limit=max(int(limit or 0), 1),
    )
    return _post_filter_row_candidates(
        server,
        row_kind,
        profile,
        candidates,
        relaxed=relaxed,
    )


def _finalize_custom_track_items(
    *,
    server: Any,
    profile: Dict[str, Any],
    ranking_row_kind: str,
    title: str,
    candidates: Sequence[Dict[str, Any]],
    limit: int,
    used_track_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    finalized = finalize_row_items(
        server=server,
        row_kind=ranking_row_kind,
        title=title,
        candidates=list(candidates or []),
        profile=profile,
        used_track_ids=set(used_track_ids or set()),
        used_artist_counts={},
        enforce_feed_artist_cap=False,
        max_items=max(int(limit or 0), 1),
    )
    return list((finalized or {}).get("items") or [])


def snapshot_substrate_mode(snapshot: Dict[str, Any] | None) -> str:
    payload = dict(snapshot or {})
    resolved_from = str(payload.get("resolved_from") or "").strip().lower()
    pool_counts = {
        str(pool_name): int(count or 0)
        for pool_name, count in dict(payload.get("pool_counts") or {}).items()
    }
    if "fallback" in resolved_from or "error" in resolved_from:
        return "thin_core"
    history_signal = sum(
        1
        for pool_name in ("history_recent", "history_top", "collaborative")
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    anchor_signal = sum(
        1
        for pool_name in (
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
        )
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    feature_signal = sum(
        1
        for pool_name in (
            "peer_scene",
            "genre_subgenre",
            "popularity_taste",
        )
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    richness_signal = sum(
        1
        for pool_name in (
            "artist_neighbors",
            "primary_anchor_neighbors",
            "anchor_neighbors",
            "peer_scene",
            "genre_subgenre",
            "popularity_taste",
        )
        if int(pool_counts.get(pool_name) or 0) > 0
    )
    if history_signal <= 0:
        return "thin_core"
    if anchor_signal >= 2:
        return "rich_personalized"
    if anchor_signal >= 1 and (
        feature_signal >= 1
        or int(pool_counts.get("collaborative") or 0) > 0
        or int(pool_counts.get("history_top") or 0) > 0
    ):
        return "rich_personalized"
    if feature_signal >= 2 and int(pool_counts.get("collaborative") or 0) > 0:
        return "rich_personalized"
    if richness_signal < 2:
        return "thin_core"
    return "rich_personalized"


def _build_continue_listening_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
) -> Dict[str, Any] | None:
    return build_continue_listening_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        combine_pools_fn=_combine_pools,
        snapshot_substrate_mode_fn=snapshot_substrate_mode,
    )


def build_continue_listening_row(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    resolved_snapshot = dict(snapshot or {})
    if not resolved_snapshot:
        resolved_snapshot = trim_home_candidate_snapshot(
            server,
            build_home_candidate_snapshot_fast_fallback(
                server=server,
                profile=profile,
            ),
        )
    row = _build_continue_listening_seed(
        server=server,
        profile=profile,
        snapshot=resolved_snapshot,
        title=row_title("continue_listening", profile),
    )
    if isinstance(row, dict):
        row["id"] = "continue_listening"
    return row


def _build_todays_pick_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
) -> Dict[str, Any] | None:
    return build_todays_pick_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        home_todays_pick_candidate_cap=(
            _HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP
            if launch_tier_only
            else _HOME_TODAYS_PICK_CANDIDATE_CAP
        ),
        custom_row_candidates_fn=_custom_row_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        track_artist_label_fn=_track_artist_label,
        theme_accent_fn=_theme_accent,
    )


def _build_mixed_for_you_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
    launch_tier_only: bool = False,
) -> Dict[str, Any] | None:
    if snapshot_substrate_mode(snapshot) != "rich_personalized":
        return None
    return build_mixed_for_you_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        home_mix_track_cap=(
            min(_HOME_MIX_TRACK_CAP, _HOME_LAUNCH_MIX_TRACK_CAP)
            if launch_tier_only
            else _HOME_MIX_TRACK_CAP
        ),
        home_mix_max_count=_HOME_MIX_MAX_COUNT,
        home_mix_accents=_HOME_MIX_ACCENTS,
        select_mix_blueprints_fn=_select_mix_blueprints,
        mix_blueprints_fn=_mix_blueprints,
        mix_blueprint_candidates_fn=_mix_blueprint_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        theme_accent_fn=_theme_accent,
        mix_artist_line_fn=_mix_artist_line,
    )


def _build_trending_by_genre_seed(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    title: str,
) -> Dict[str, Any] | None:
    return build_trending_by_genre_seed_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        title=title,
        trending_genre_tabs_fn=_build_trending_genre_tabs,
    )


def _build_trending_genre_tabs(
    *,
    server: Any,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    preferred_tab_ids: Sequence[str] | None = None,
    selected_tab_id: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
    return build_trending_genre_tabs_stage(
        server=server,
        profile=profile,
        snapshot=snapshot,
        preferred_tab_ids=preferred_tab_ids,
        selected_tab_id=selected_tab_id,
        home_genre_candidate_cap=_HOME_GENRE_CANDIDATE_CAP,
        home_genre_tab_limit=_HOME_GENRE_TAB_LIMIT,
        home_genre_track_cap=_HOME_GENRE_TRACK_CAP,
        build_row_allocation_plan_fn=build_row_allocation_plan,
        trending_primary_pool_order_fn=_trending_primary_pool_order,
        combine_pools_fn=_combine_pools,
        trending_taste_facets_fn=_trending_taste_facets,
        trending_facet_candidates_fn=_trending_facet_candidates,
        finalize_custom_track_items_fn=_finalize_custom_track_items,
        mix_artist_line_fn=_mix_artist_line,
        genre_tab_identifier_fn=_genre_tab_identifier,
    )


def refresh_trending_by_genre_row(
    *,
    server: Any,
    row: Dict[str, Any],
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    tab_id: str,
) -> Dict[str, Any]:
    return refresh_trending_by_genre_row_builder(
        row=row,
        tab_id=tab_id,
        trending_genre_tabs_fn=lambda **kwargs: _build_trending_genre_tabs(
            server=server,
            profile=profile,
            snapshot=snapshot,
            **kwargs,
        ),
    )


def build_row_seed(
    *,
    server: Any,
    row_kind: str,
    profile: Dict[str, Any],
    snapshot: Dict[str, Any],
    relaxed_filter: bool = False,
    pool_names_override: Sequence[str] | None = None,
    candidate_limit_override: int | None = None,
    allow_empty_diagnostics: bool = False,
    launch_tier_only: bool = False,
) -> Dict[str, Any] | None:
    title = row_title(row_kind, profile)
    row_started_at = time.perf_counter()
    specialized_row_seed = build_specialized_row_seed(
        row_kind=row_kind,
        title=title,
        snapshot=snapshot,
        row_started_at=row_started_at,
        specialized_builders={
            "todays_pick": lambda: _build_todays_pick_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
                launch_tier_only=launch_tier_only,
            ),
            "mixed_for_you": lambda: _build_mixed_for_you_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
                launch_tier_only=launch_tier_only,
            ),
            "trending_by_genre": lambda: _build_trending_by_genre_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
            ),
            "continue_listening": lambda: _build_continue_listening_seed(
                server=server,
                profile=profile,
                snapshot=snapshot,
                title=title,
            ),
        },
        home_album_cap=_HOME_ALBUM_CAP,
        home_artist_cap=_HOME_ARTIST_CAP,
    )
    if isinstance(specialized_row_seed, dict):
        return specialized_row_seed
    return build_allocated_row_seed(
        server=server,
        row_kind=row_kind,
        profile=profile,
        snapshot=snapshot,
        title=title,
        row_started_at=row_started_at,
        relaxed_filter=relaxed_filter,
        pool_names_override=pool_names_override,
        candidate_limit_override=candidate_limit_override,
        allow_empty_diagnostics=allow_empty_diagnostics,
        launch_tier_only=launch_tier_only,
        build_row_allocation_plan_fn=build_row_allocation_plan,
        prefiltered_pool_name_fn=_prefiltered_pool_name,
        quiet_primary_pool_order_fn=_quiet_primary_pool_order,
        trending_primary_pool_order_fn=_trending_primary_pool_order,
        prefilter_pool_order_fn=_prefilter_pool_order,
        merge_pool_order_fn=_merge_pool_order,
        combine_pools_fn=_combine_pools,
        post_filter_row_candidates_fn=_post_filter_row_candidates,
    )


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
        launch_tier_only=False,
    )
    if not isinstance(row_seed, dict):
        row_seed = build_required_fallback_seed_with_overrides(
            row_kind=row_kind,
            snapshot=snapshot,
            build_row_seed_fn=lambda **kwargs: build_row_seed(
                server=server,
                row_kind=row_kind,
                profile=profile,
                snapshot=snapshot,
                **kwargs,
            ),
            apply_required_row_fallback_policy_fn=apply_required_row_fallback_policy,
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
        same_artist_votes = sum(
            1 for source_name in source_names if str(source_name or "").strip() == "same_artist"
        )
        if query_derived_votes > 0:
            candidate_score -= min(0.35 * query_derived_votes, 0.9)
        if row_kind in {"because_you_played", "rediscover", "mixed_for_you", "trending_by_genre"}:
            candidate_score -= min(0.55 * same_artist_votes, 0.85)
        authenticity_penalty = _track_authenticity_penalty(
            candidate.get("track") if isinstance(candidate.get("track"), dict) else {}
        )
        if authenticity_penalty > 0.0:
            candidate_score -= authenticity_penalty
            ranking_features = dict(score_payload.get("ranking_features") or {})
            ranking_features["authenticity_penalty"] = -authenticity_penalty
            score_payload["ranking_features"] = ranking_features
        if same_artist_votes > 0 and row_kind in {"because_you_played", "rediscover", "mixed_for_you", "trending_by_genre"}:
            ranking_features = dict(score_payload.get("ranking_features") or {})
            ranking_features["same_artist_source_penalty"] = -min(0.55 * same_artist_votes, 0.85)
            score_payload["ranking_features"] = ranking_features
        ranked.append((candidate_score, candidate, score_payload))
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = []
    artist_counts = {}
    global_artist_counts = used_artist_counts if isinstance(used_artist_counts, dict) else {}
    artist_family_counts = {}
    global_artist_family_counts = global_artist_counts
    query_derived_selected = 0
    quality_floor_value = quality_floor(row_kind)
    max_same_artist_value = max_same_artist(row_kind)
    max_feed_same_artist_value = max_feed_same_artist(row_kind)
    max_same_family_value, max_feed_same_family_value = _artist_family_caps(
        row_kind,
        max_same_artist_value=max_same_artist_value,
        max_feed_same_artist_value=max_feed_same_artist_value,
    )
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
        artist_family_key = _artist_family_identity(
            track.get("channel") or track.get("artist") or track.get("author") or ""
        )
        feed_count = 0
        if artist_key:
            current_count = artist_counts.get(artist_key, 0)
            strict_row_family_caps = row_kind in {
                "because_you_played",
                "rediscover",
                "mixed_for_you",
                "trending_by_genre",
            }
            if current_count >= max_same_artist_value and (
                strict_row_family_caps or len(selected) + 1 < max_items
            ):
                continue
            feed_count = int(global_artist_counts.get(artist_key) or 0)
            if (
                enforce_feed_artist_cap
                and
                feed_count >= max_feed_same_artist_value
                and (strict_row_family_caps or len(selected) + 1 < max_items)
            ):
                continue
        if artist_family_key:
            current_family_count = int(artist_family_counts.get(artist_family_key) or 0)
            if current_family_count >= max_same_family_value and (
                strict_row_family_caps or len(selected) + 1 < max_items
            ):
                continue
            global_family_count = int(
                global_artist_family_counts.get(f"family:{artist_family_key}") or 0
            )
            if (
                enforce_feed_artist_cap
                and global_family_count >= max_feed_same_family_value
                and (strict_row_family_caps or len(selected) + 1 < max_items)
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
        if artist_family_key:
            artist_family_counts[artist_family_key] = (
                int(artist_family_counts.get(artist_family_key) or 0) + 1
            )
            global_artist_family_counts[f"family:{artist_family_key}"] = (
                int(global_artist_family_counts.get(f"family:{artist_family_key}") or 0)
                + 1
            )
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
