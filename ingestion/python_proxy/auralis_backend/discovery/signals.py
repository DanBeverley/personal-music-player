from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..domain.features import build_home_profile

try:
    from ..recommend.profile_signal import profile_signal_tier
except Exception:
    profile_signal_tier = None

from .schema import TasteProfile


def _trim(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> List[Any]:
    return list(value or []) if isinstance(value, (list, tuple)) else []


def _strings(values: Iterable[Any], limit: int = 24) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values or []:
        text = _trim(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _track_dicts(values: Iterable[Any], limit: int = 48) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for value in values or []:
        if isinstance(value, dict):
            output.append(dict(value))
        if len(output) >= limit:
            break
    return output


def _collaborative_track_ids(profile: Dict[str, Any]) -> List[str]:
    collaborative = profile.get("collaborative") or {}
    if not isinstance(collaborative, dict):
        return []
    raw_ids = (
        collaborative.get("candidate_track_ids")
        or collaborative.get("track_ids")
        or collaborative.get("neighbors")
        or []
    )
    if isinstance(raw_ids, dict):
        raw_ids = raw_ids.keys()
    output: List[str] = []
    for item in raw_ids or []:
        if isinstance(item, dict):
            track_id = _trim(item.get("track_id") or item.get("id") or item.get("videoId"))
        else:
            track_id = _trim(item)
        if track_id:
            output.append(track_id)
        if len(output) >= 64:
            break
    return _strings(output, 64)


def build_taste_profile(server: Any, req: Any) -> TasteProfile:
    _legacy_req, profile = build_home_profile(req)
    if not isinstance(profile, dict):
        profile = {}
    tier = _trim(getattr(req, "client_signal_tier", ""))
    profile_has_persisted_signals = bool(
        profile.get("history_track_snapshots")
        or profile.get("frequent_track_snapshots")
        or profile.get("recent_track_snapshots")
        or profile.get("top_track_snapshots")
        or profile.get("last_played_tracks")
    )
    if tier == "cold_start" and profile_has_persisted_signals:
        tier = "personalized"
    if not tier and profile_signal_tier is not None:
        try:
            tier = _trim(profile_signal_tier(profile))
        except Exception:
            tier = ""
    if not tier:
        tier = "known" if (
            profile.get("recent_track_snapshots")
            or profile.get("top_track_snapshots")
            or profile.get("last_played_tracks")
            or profile.get("artist_hints")
            or profile.get("taste_queries")
        ) else "cold_start"

    user_scope_id = _trim(profile.get("user_scope_id")) or _trim(getattr(req, "user_scope_id", "")) or "guest"
    from .preferences import load_recommendation_preferences

    preferences = load_recommendation_preferences(server, user_scope_id)
    taste_mode = _trim(preferences.get("effective_taste_mode")) or "neatie"
    listenbrainz_username = _trim(preferences.get("listenbrainz_username"))
    profile_key = _trim(profile.get("profile_key")) or user_scope_id
    profile_key = f"{profile_key}|taste:{taste_mode}:{listenbrainz_username.casefold()}"
    profile = {
        **profile,
        "recommendation_preferences": preferences,
    }
    return TasteProfile(
        user_scope_id=user_scope_id,
        profile_key=profile_key,
        signal_tier=tier,
        recent_tracks=_track_dicts(
            profile.get("recent_track_snapshots")
            or profile.get("recent_tracks")
            or getattr(req, "recent_tracks", []),
            48,
        ),
        top_tracks=_track_dicts(
            profile.get("top_track_snapshots")
            or profile.get("top_tracks")
            or getattr(req, "top_tracks", []),
            48,
        ),
        last_played_tracks=_track_dicts(
            profile.get("last_played_tracks") or getattr(req, "last_played_tracks", []),
            48,
        ),
        anchor_tracks=_track_dicts(
            profile.get("anchor_track_snapshots")
            or profile.get("anchor_tracks")
            or getattr(req, "anchor_track_snapshots", []),
            32,
        ),
        full_history_tracks=_track_dicts(
            profile.get("history_track_snapshots") or [],
            4096,
        ),
        frequent_tracks=_track_dicts(
            profile.get("frequent_track_snapshots") or [],
            256,
        ),
        artist_hints=_strings(
            _as_list(profile.get("artist_hints")) + _as_list(getattr(req, "artist_hints", [])),
            24,
        ),
        album_hints=_strings(
            _as_list(profile.get("album_hints")) + _as_list(getattr(req, "album_hints", [])),
            24,
        ),
        top_artists=_strings(profile.get("top_artists") or [], 24),
        listened_artists=_strings(profile.get("listened_artists") or [], 48),
        top_albums=_strings(profile.get("top_albums") or [], 24),
        recent_queries=_strings(
            _as_list(profile.get("recent_queries")) + _as_list(getattr(req, "recent_queries", [])),
            24,
        ),
        taste_queries=_strings(
            _as_list(profile.get("taste_queries")) + _as_list(getattr(req, "taste_queries", [])),
            24,
        ),
        collaborative_track_ids=_collaborative_track_ids(profile),
        avoid_ids=_strings(
            _as_list(getattr(req, "avoid_ids", []))
            + _as_list(profile.get("avoid_ids")),
            96,
        ),
        force_refresh=bool(
            getattr(req, "force_refresh", False)
            or getattr(req, "prefer_fresh_rows", False)
        ),
        refresh_token=_trim(getattr(req, "refresh_token", "")),
        source_profile=dict(profile),
        taste_mode=taste_mode,
        listenbrainz_username=listenbrainz_username,
    )
