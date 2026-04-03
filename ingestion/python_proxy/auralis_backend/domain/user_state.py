from __future__ import annotations

from typing import Any, Dict, List, Tuple, TypedDict

from ..contracts import (
    RecommendationHomeV3Request,
    SearchV3Request,
    SimilarArtistsV3Request,
    SuggestV2Request,
)
from ..legacy import build_search_request, get_server, trim_text, unique_snapshot_tracks, unique_strings
from ..recommend.profile_runtime import build_profile_key, hydrate_state_profile


_NOISY_QUERY_PHRASES = (
    "deluxe remastered",
    "deluxe edition",
    "original soundtrack",
    "motion picture",
    "tribute to",
    "karaoke version",
    "radio edit",
    "from the motion picture",
)
_NOISY_QUERY_TOKENS = {
    "bonus",
    "deluxe",
    "edition",
    "karaoke",
    "mono",
    "original",
    "remaster",
    "remastered",
    "soundtrack",
    "stereo",
    "tribute",
    "version",
}


class UserStateSnapshot(TypedDict, total=False):
    profile_key: str
    user_scope_id: str
    session_id: str
    surface: str
    recent_track_ids: List[str]
    top_track_ids: List[str]
    recent_track_snapshots: List[Dict[str, Any]]
    top_track_snapshots: List[Dict[str, Any]]
    anchor_track_snapshots: List[Dict[str, Any]]
    last_played_tracks: List[Dict[str, Any]]
    recent_queries: List[str]
    taste_queries: List[str]
    anchor_artist_hints: List[str]
    artist_hints: List[str]
    album_hints: List[str]
    playlist_names: List[str]
    library_track_ids: List[str]
    offline_track_ids: List[str]
    top_artists: List[str]
    listened_artists: List[str]
    top_albums: List[str]
    repeat_intensity: float
    novelty_tolerance: float
    novelty_preference: float
    repeat_tolerance: float
    experiment_variant: str
    vectors: Dict[str, Any]
    embedding_profile: Dict[str, Any]
    collaborative: Dict[str, Any]
    collaborative_profile: Dict[str, Any]
    profile_runtime: Dict[str, Any]


def _normalize_track_snapshots(raw_tracks, limit: int) -> List[Dict[str, Any]]:
    return unique_snapshot_tracks(raw_tracks or [], limit)


def _is_metadata_heavy_query(raw_query: str) -> bool:
    normalized = trim_text(raw_query).lower()
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _NOISY_QUERY_PHRASES):
        return True
    hits = sum(1 for token in normalized.split() if token in _NOISY_QUERY_TOKENS)
    return hits >= 2


def _clean_query_values(values, *, limit: int) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for raw_value in values or []:
        value = trim_text(raw_value)
        if not value or _is_metadata_heavy_query(value):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _snapshot_artist_hints(server, tracks: List[Dict[str, Any]]) -> List[str]:
    hints: List[str] = []
    for track in tracks:
        hints.extend(server.extract_artist_names(track))
        channel_name = trim_text(track.get("channel"))
        if channel_name:
            hints.append(channel_name)
    return unique_strings(hints, 16)


def _build_state_snapshot(legacy_req) -> UserStateSnapshot:
    server = get_server()
    recent_track_snapshots = server._recommendation_unique_snapshot_tracks(
        [*(legacy_req.last_played_tracks or []), *(legacy_req.recent_track_snapshots or [])],
        16,
    )
    top_track_snapshots = server._recommendation_unique_snapshot_tracks(
        legacy_req.top_track_snapshots,
        16,
    )
    last_played_tracks = server._recommendation_unique_snapshot_tracks(
        legacy_req.last_played_tracks,
        12,
    )
    anchor_track_snapshots = server._recommendation_unique_snapshot_tracks(
        legacy_req.anchor_track_snapshots,
        8,
    )

    recent_track_ids = server._recommendation_unique_track_ids(
        [
            legacy_req.seed_id,
            *(track.get("id") for track in recent_track_snapshots),
            *(legacy_req.seed_ids or []),
            *(legacy_req.recent_track_ids or []),
        ],
        16,
    )
    top_track_ids = server._recommendation_unique_track_ids(
        [
            *(track.get("id") for track in top_track_snapshots),
            *(legacy_req.top_track_ids or []),
            *(legacy_req.seed_ids or []),
            legacy_req.seed_id,
        ],
        16,
    )
    taste_queries = _clean_query_values(legacy_req.taste_queries, limit=12)
    recent_queries = _clean_query_values(
        [*(legacy_req.recent_queries or []), legacy_req.query, *taste_queries],
        limit=12,
    )
    anchor_artist_hints = unique_strings(
        [
            *(legacy_req.anchor_artist_hints or []),
            *_snapshot_artist_hints(server, anchor_track_snapshots),
        ],
        8,
    )
    combined_tracks = [
        *anchor_track_snapshots,
        *top_track_snapshots,
        *recent_track_snapshots,
        *last_played_tracks,
    ]
    artist_hints = unique_strings(
        [
            *anchor_artist_hints,
            *(legacy_req.artist_hints or []),
            *_snapshot_artist_hints(server, combined_tracks),
        ],
        12,
    )
    album_hints = unique_strings(
        [
            *(legacy_req.album_hints or []),
            *[
                trim_text(track.get("album"))
                for track in combined_tracks
                if trim_text(track.get("album"))
            ],
        ],
        12,
    )
    playlist_names = unique_strings(legacy_req.playlist_names, 12)
    library_track_ids = server._recommendation_unique_track_ids(
        legacy_req.library_track_ids,
        28,
    )
    offline_track_ids = server._recommendation_unique_track_ids(
        legacy_req.offline_track_ids,
        28,
    )

    artist_weights: Dict[str, float] = {}
    for index, track in enumerate(top_track_snapshots):
        for artist_name in server.extract_artist_names(track):
            artist_weights[artist_name] = artist_weights.get(artist_name, 0.0) + max(
                1.8 - (index * 0.14),
                0.55,
            )
    for index, track in enumerate(recent_track_snapshots):
        for artist_name in server.extract_artist_names(track):
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
    top_artist_names = unique_strings([*ranked_artist_names, *artist_hints], 6)
    listened_artist_names = unique_strings([*ranked_artist_names, *artist_hints], 12)
    top_album_names = album_hints[:6]
    repeat_intensity = min(1.0, len(top_track_ids) / 10.0)
    novelty_tolerance = max(
        0.25,
        min(0.75, 0.38 + (len(recent_queries) * 0.04) + (len(artist_hints) * 0.02)),
    )

    profile: UserStateSnapshot = {
        "profile_key": build_profile_key(server, legacy_req),
        "user_scope_id": trim_text(legacy_req.user_scope_id or "guest") or "guest",
        "session_id": trim_text(legacy_req.session_id or ""),
        "surface": trim_text(legacy_req.surface or "home_feed") or "home_feed",
        "recent_track_ids": recent_track_ids,
        "top_track_ids": top_track_ids,
        "recent_track_snapshots": recent_track_snapshots,
        "top_track_snapshots": top_track_snapshots,
        "anchor_track_snapshots": anchor_track_snapshots,
        "last_played_tracks": last_played_tracks,
        "recent_queries": recent_queries,
        "taste_queries": taste_queries,
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
        "novelty_preference": novelty_tolerance,
        "repeat_tolerance": max(0.0, 1.0 - repeat_intensity),
        "experiment_variant": server._recommendation_assignment_for_user(
            trim_text(legacy_req.user_scope_id or "guest") or "guest"
        ),
    }
    return hydrate_state_profile(
        server,
        profile,
        force_refresh=bool(getattr(legacy_req, "force_refresh", False)),
    )


def build_search_state(
    req: SearchV3Request | SuggestV2Request,
) -> Tuple[Any, UserStateSnapshot]:
    raw_recent_tracks = (
        getattr(req, "recent_tracks", None)
        or getattr(req, "recent_track_snapshots", None)
        or []
    )
    raw_top_tracks = (
        getattr(req, "top_tracks", None)
        or getattr(req, "top_track_snapshots", None)
        or []
    )
    raw_last_played_tracks = getattr(req, "last_played_tracks", None) or raw_recent_tracks
    recent_tracks = _normalize_track_snapshots(
        [
            *(raw_last_played_tracks or []),
            *(raw_recent_tracks or []),
        ],
        16,
    )
    top_tracks = _normalize_track_snapshots(raw_top_tracks, 12)
    last_played_tracks = _normalize_track_snapshots(
        raw_last_played_tracks or [],
        8,
    )
    recent_queries = _clean_query_values(
        [*(req.recent_queries or []), *(getattr(req, "taste_queries", []) or [])],
        limit=12,
    )
    legacy_req = build_search_request(
        query=req.query,
        limit=req.limit,
        user_scope_id=req.user_scope_id or "guest",
        session_id=req.session_id,
        surface=getattr(req, "context_surface", "search") or "search",
        force_refresh=bool(getattr(req, "force_refresh", False)),
        recent_queries=recent_queries,
        taste_queries=_clean_query_values(
            [*(getattr(req, "taste_queries", []) or []), *recent_queries],
            limit=8,
        ),
        recent_track_ids=list(getattr(req, "recent_track_ids", []) or []),
        top_track_ids=list(getattr(req, "top_track_ids", []) or []),
        recent_track_snapshots=recent_tracks,
        top_track_snapshots=top_tracks or recent_tracks[:8],
        last_played_tracks=last_played_tracks or recent_tracks[:8],
        artist_hints=list(getattr(req, "artist_hints", []) or []),
        album_hints=list(getattr(req, "album_hints", []) or []),
        playlist_names=list(getattr(req, "playlist_names", []) or []),
        library_track_ids=list(getattr(req, "library_track_ids", []) or []),
        offline_track_ids=list(getattr(req, "offline_track_ids", []) or []),
    )
    return legacy_req, _build_state_snapshot(legacy_req)


def build_home_state(
    req: RecommendationHomeV3Request,
) -> Tuple[Any, UserStateSnapshot]:
    raw_recent_tracks = (
        getattr(req, "recent_tracks", None)
        or getattr(req, "recent_track_snapshots", None)
        or []
    )
    raw_top_tracks = (
        getattr(req, "top_tracks", None)
        or getattr(req, "top_track_snapshots", None)
        or []
    )
    raw_last_played_tracks = getattr(req, "last_played_tracks", None) or raw_recent_tracks
    recent_tracks = _normalize_track_snapshots(raw_recent_tracks, 16)
    top_tracks = _normalize_track_snapshots(raw_top_tracks, 16)
    last_played_tracks = _normalize_track_snapshots(raw_last_played_tracks, 12)
    recent_queries = _clean_query_values(
        [*req.recent_queries, *req.taste_queries, req.query],
        limit=12,
    )
    taste_queries = _clean_query_values(
        [*req.taste_queries, *recent_queries],
        limit=8,
    )
    legacy_req = build_search_request(
        query="",
        limit=req.limit,
        user_scope_id=req.user_scope_id or "guest",
        session_id=req.session_id,
        surface="home_feed",
        force_refresh=req.force_refresh,
        recent_queries=recent_queries,
        taste_queries=taste_queries,
        artist_hints=list(req.artist_hints or []),
        album_hints=list(req.album_hints or []),
        avoid_ids=list(req.avoid_ids or []),
        recent_track_ids=list(req.recent_track_ids or []),
        top_track_ids=list(req.top_track_ids or []),
        recent_track_snapshots=recent_tracks,
        top_track_snapshots=top_tracks or recent_tracks[:8],
        last_played_tracks=last_played_tracks or recent_tracks[:8],
        playlist_names=list(req.playlist_names or []),
        library_track_ids=list(req.library_track_ids or []),
        offline_track_ids=list(req.offline_track_ids or []),
    )
    return legacy_req, _build_state_snapshot(legacy_req)


def build_similar_artists_state(
    req: SimilarArtistsV3Request,
) -> Tuple[Any, UserStateSnapshot]:
    server = get_server()
    recent_tracks = _normalize_track_snapshots(req.recent_tracks, 12)
    anchor_track_snapshots = _normalize_track_snapshots(
        [req.anchor_track_snapshot] if req.anchor_track_snapshot else [],
        4,
    )
    if not anchor_track_snapshots and req.anchor_track_id:
        fetched = server._recommendation_fetch_tracks_for_ids([req.anchor_track_id], limit=1)
        anchor_track_snapshots = _normalize_track_snapshots(fetched, 1)
    anchor_artist_hints: List[str] = []
    if req.anchor_artist_id:
        try:
            artist_payload = server._build_artist_details_payload(req.anchor_artist_id)
        except Exception:
            artist_payload = {}
        artist_name = trim_text((artist_payload.get("artist") or {}).get("name"))
        if artist_name:
            anchor_artist_hints.append(artist_name)
    cleaned_queries = _clean_query_values(req.recent_queries, limit=12)
    legacy_req = build_search_request(
        query=req.query or "",
        limit=req.limit,
        user_scope_id=req.user_scope_id or "guest",
        session_id=req.session_id,
        surface=req.surface or "search_results",
        recent_queries=cleaned_queries,
        taste_queries=cleaned_queries[:8],
        recent_track_snapshots=recent_tracks,
        top_track_snapshots=recent_tracks[:8],
        last_played_tracks=recent_tracks[:8],
        anchor_track_snapshots=anchor_track_snapshots,
        anchor_artist_hints=anchor_artist_hints,
    )
    return legacy_req, _build_state_snapshot(legacy_req)
