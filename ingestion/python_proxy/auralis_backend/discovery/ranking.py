from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple
import hashlib
import re

from .admission import (
    DISCOVERY_ROWS,
    TRUSTED_AUTHORITIES,
    candidate_profile_compatibility,
)
from .allocation import allocate_home_rows
from .candidates import album_name, artist_name, item_signature, metadata_text, normalize_text, track_id
from .config import LANE_ORDER, LANE_RECIPES, ROW_RECIPES
from .schema import DiscoveryCandidate, DiscoveryRow, LaneRecipe, RowRecipe, TasteProfile


def _contains_token_phrase(text: str, phrase: Any) -> bool:
    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(
        re.escape(part) for part in normalized_phrase.split()
    ) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


SOURCE_WEIGHTS = {
    "history": 0.9,
    "profile_spine": 1.35,
    "similarity": 1.25,
    "artist_graph": 1.0,
    "genre_mood": 0.95,
    "album": 1.0,
    "freshness": 1.1,
    "ytmusic_home": 0.65,
    "popularity": 0.55,
    "collaborative": 0.25,
    "lane_chill": 1.15,
    "lane_workout": 1.15,
    "lane_focus": 1.15,
    "lane_mood": 1.15,
    "discovery_universe": 0.8,
}

ROW_INTENT_HINTS = {
    "daily_pick": ("favorite", "hit", "best", "classic", "official", "popular"),
    "personal_mix": ("favorite", "mix", "best", "hit", "popular"),
    "anchor_recommendation": ("similar", "classic", "radio", "official"),
    "artist_radio": ("radio", "artist", "similar", "classic", "official", "popular"),
    "taste_discovery": ("favorite", "classic", "official", "hit", "session", "live", "popular"),
}

REFRESH_MUTABLE_ROWS = {
    "todays_pick",
    "featured_new_albums",
    "made_for_you",
    "because_you_played",
    "popular_radio",
    "recommended_albums",
    "recommended_artists",
    "quiet_picks",
    "home_lane",
    "personal_mix_slice",
}


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _negative_feedback_by_type(taste: TasteProfile) -> Dict[str, Dict[str, float]]:
    profile = dict(taste.source_profile or {})
    raw = profile.get("negative_feedback") or {}
    if isinstance(raw, dict) and isinstance(raw.get("by_type"), dict):
        raw = raw.get("by_type") or {}
    output: Dict[str, Dict[str, float]] = {}
    if not isinstance(raw, dict):
        return output
    for feedback_type, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        typed: Dict[str, float] = {}
        for key, value in entries.items():
            normalized = normalize_text(key)
            if not normalized:
                continue
            typed[normalized] = _number(value)
        if typed:
            output[str(feedback_type or "")] = typed
    return output


def _item_feedback_keys(item: Dict[str, Any]) -> Dict[str, Set[str]]:
    title_key = normalize_text(item.get("title_key") or item.get("title") or item.get("name"))
    artist_key = normalize_text(item.get("artist_key") or artist_name(item))
    album_key = normalize_text(album_name(item))
    primary_genre = normalize_text(item.get("primary_genre") or item.get("genre"))
    subgenre = normalize_text(item.get("subgenre"))
    language = normalize_text(item.get("language"))
    script = normalize_text(item.get("script"))
    scene_clusters = {
        normalize_text(value)
        for value in item.get("scene_cluster_ids") or []
        if normalize_text(value)
    }
    duplicate = f"{title_key}|{artist_key}" if title_key and artist_key else ""
    return {
        "exact_track": {normalize_text(track_id(item))},
        "duplicate_track": {duplicate},
        "artist_cluster": {artist_key},
        "genre_cluster": {primary_genre},
        "subgenre_cluster": {subgenre},
        "language_cluster": {language},
        "script_cluster": {script},
        "scene_cluster": scene_clusters,
        "album_cluster": {album_key},
    }


def _negative_feedback_penalty(item: Dict[str, Any], taste: TasteProfile) -> Tuple[float, bool]:
    feedback = _negative_feedback_by_type(taste)
    if not feedback:
        return 0.0, False
    weights = {
        "exact_track": 3.4,
        "duplicate_track": 2.2,
        "artist_cluster": 1.05,
        "album_cluster": 0.8,
        "genre_cluster": 0.82,
        "subgenre_cluster": 0.62,
        "language_cluster": 0.75,
        "script_cluster": 0.45,
        "scene_cluster": 0.7,
    }
    penalty = 0.0
    hard_suppressed = False
    for feedback_type, keys in _item_feedback_keys(item).items():
        keys.discard("")
        entries = feedback.get(feedback_type) or {}
        for key in keys:
            strength = _number(entries.get(key))
            if strength <= 0:
                continue
            penalty += strength * weights.get(feedback_type, 0.5)
            if feedback_type in {"exact_track", "duplicate_track"} and strength >= 0.85:
                hard_suppressed = True
    return penalty, hard_suppressed


def _avoid_ids(taste: TasteProfile) -> Set[str]:
    output: Set[str] = set()
    for value in taste.avoid_ids or []:
        raw = str(value or "").strip()
        if not raw:
            continue
        output.add(raw)
        normalized = normalize_text(raw)
        if normalized:
            output.add(normalized)
    return output


def _refresh_jitter(taste: TasteProfile, identity: Any) -> float:
    token = str(taste.refresh_token or "").strip()
    key = str(identity or "").strip()
    if not token or not key:
        return 0.0
    digest = hashlib.sha256(f"{token}:{key}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:2], "big") / 65535.0) - 0.5


def _album_identity_keys(item: Dict[str, Any]) -> Set[str]:
    title = normalize_text(item.get("title") or item.get("album"))
    artist = normalize_text(item.get("artist") or item.get("artist_name") or item.get("channel"))
    keys = {
        normalize_text(item.get("id")),
        normalize_text(item.get("album_id")),
        normalize_text(item.get("albumId")),
        normalize_text(item.get("canonical_album_identity")),
        normalize_text(item.get("canonical_source_identity")),
        normalize_text(f"{title}|{artist}"),
    }
    keys.discard("")
    return keys


def _candidate_sources(candidates: Iterable[DiscoveryCandidate]) -> Dict[str, DiscoveryCandidate]:
    merged: Dict[str, DiscoveryCandidate] = {}
    for candidate in candidates or []:
        signature = item_signature(candidate.item)
        if not signature:
            continue
        current = merged.get(signature)
        if current is None:
            merged[signature] = DiscoveryCandidate(
                item=dict(candidate.item),
                source=candidate.source,
                score=float(candidate.score),
                reasons=list(candidate.reasons or []),
                item_type=candidate.item_type,
            )
            continue
        current.score = max(current.score, candidate.score) + 0.08
        if candidate.source not in current.source.split("+"):
            current.source = f"{current.source}+{candidate.source}"
        for reason in candidate.reasons or []:
            if reason not in current.reasons:
                current.reasons.append(reason)
        merged_genres = list(current.item.get("discovery_genres") or [])
        for genre in candidate.item.get("discovery_genres") or []:
            if genre not in merged_genres:
                merged_genres.append(genre)
        if merged_genres:
            current.item["discovery_genres"] = merged_genres
    return merged


def _collect_candidates(
    pools: Dict[str, List[DiscoveryCandidate]],
    sources: Sequence[str],
    *,
    item_type: str = "track",
) -> List[DiscoveryCandidate]:
    candidates: List[DiscoveryCandidate] = []
    for source in sources or ():
        for candidate in pools.get(source, []) or []:
            if candidate.item_type == item_type:
                item = dict(candidate.item)
                item.setdefault("candidate_pool_source", source)
                candidates.append(
                    DiscoveryCandidate(
                        item=item,
                        source=candidate.source,
                        score=candidate.score,
                        reasons=list(candidate.reasons or []),
                        item_type=candidate.item_type,
                    )
                )
    return list(_candidate_sources(candidates).values())


def _taste_artist_keys(taste: TasteProfile) -> Set[str]:
    artists = set()
    for artist in (taste.artist_hints + taste.top_artists + taste.listened_artists):
        key = normalize_text(artist)
        if key:
            artists.add(key)
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        key = normalize_text(artist_name(track))
        if key:
            artists.add(key)
    return artists


def _history_track_ids(taste: TasteProfile) -> Set[str]:
    ids = set()
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        item_id = track_id(track)
        if item_id:
            ids.add(item_id)
    return ids


def _history_track_identity_keys(taste: TasteProfile) -> Set[str]:
    keys: Set[str] = set()
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks + taste.anchor_tracks:
        item_id = track_id(track)
        title = normalize_text(track.get("title") or track.get("name"))
        artist = normalize_text(artist_name(track))
        album = normalize_text(album_name(track))
        for value in (
            item_id,
            track.get("canonical_track_identity"),
            track.get("canonical_source_identity"),
            f"{title}|{artist}",
        ):
            normalized = normalize_text(value)
            if normalized:
                keys.add(normalized)
        if album and artist:
            keys.add(f"album:{album}|{artist}")
    return keys


def _listened_album_keys(taste: TasteProfile) -> Tuple[Set[str], Set[str]]:
    titles = {
        normalize_text(value)
        for value in [*taste.album_hints, *taste.top_albums]
        if normalize_text(value)
    }
    exact: Set[str] = set()
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks + taste.anchor_tracks:
        title = normalize_text(album_name(track))
        artist = normalize_text(artist_name(track))
        if title:
            titles.add(title)
            exact.add(f"{title}|{artist}")
    return titles, exact


def _profile_string_set(taste: TasteProfile, *keys: str) -> Set[str]:
    output: Set[str] = set()
    pending: List[Any] = [taste.source_profile]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys:
                    if isinstance(child, dict):
                        child = child.keys()
                    if isinstance(child, (list, tuple, set)):
                        output.update(normalize_text(item) for item in child if normalize_text(item))
                    else:
                        normalized = normalize_text(child)
                        if normalized:
                            output.add(normalized)
                elif isinstance(child, dict):
                    pending.append(child)
    return output


def _taste_genre_keys(taste: TasteProfile) -> Set[str]:
    genres: Set[str] = set()
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        for key in ("genre", "subgenre", "primary_genre"):
            value = normalize_text(track.get(key))
            if value:
                genres.add(value)
        for key in ("genres", "discovery_genres", "styles", "tags", "track_type_tags"):
            values = track.get(key) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                normalized = normalize_text(value)
                if normalized:
                    genres.add(normalized)
    for value in _profile_string_set(
        taste,
        "top_genres",
        "genres",
        "genre",
        "dominant_genres",
        "supported_genres",
        "discovery_genres",
        "styles",
        "tags",
    ):
        normalized = normalize_text(value)
        if normalized:
            genres.add(normalized)
    for cluster in _profile_string_set(taste, "scene_cluster_scores", "scene_cluster_ids"):
        if cluster.startswith("genre:"):
            genres.add(cluster.split(":", 1)[1])
    return genres


def _candidate_strong_personal_match(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
) -> bool:
    artist_key = normalize_text(artist_name(candidate.item))
    if artist_key and artist_key in _taste_artist_keys(taste):
        return True
    item_id = track_id(candidate.item)
    return bool(item_id and item_id in _history_track_ids(taste))


def _candidate_recommendation_path(candidate: DiscoveryCandidate) -> str:
    path = normalize_text(candidate.item.get("recommendation_path"))
    if path:
        return path
    source = normalize_text(candidate.source)
    origin = normalize_text(candidate.item.get("discovery_origin"))
    if source == "discovery_universe" and origin:
        return {
            "history": "direct_history",
            "profile_spine": "profile_history_anchor",
            "similarity": "track_radio",
            "artist_graph": "artist_neighbor",
            "collaborative": "collaborative_neighbor",
            "genre_mood": "structured_tag",
            "ytmusic_home": "broad_global",
            "popularity": "broad_global",
        }.get(origin, "unproven")
    if source.startswith("lane_"):
        return "structured_tag"
    return {
        "history": "direct_history",
        "profile_spine": "profile_history_anchor",
        "similarity": "track_radio",
        "artist_graph": "artist_neighbor",
        "collaborative": "collaborative_neighbor",
        "genre_mood": "structured_tag",
        "ytmusic_home": "broad_global",
        "popularity": "broad_global",
    }.get(source, "unproven")


def _candidate_matches_taste(candidate: DiscoveryCandidate, taste: TasteProfile) -> bool:
    artist_key = normalize_text(artist_name(candidate.item))
    if artist_key and artist_key in _taste_artist_keys(taste):
        return True
    related_artist_keys = _profile_string_set(
        taste,
        "artist_neighborhood_preferences",
        "peer_scene_keys",
        "peer_artist_keys",
    )
    if artist_key and artist_key in related_artist_keys:
        return True
    item_artist_graph = {
        normalize_text(value)
        for value in candidate.item.get("artist_graph_keys") or []
        if normalize_text(value)
    }
    if item_artist_graph & (_taste_artist_keys(taste) | related_artist_keys):
        return True
    candidate_genres = {
        normalize_text(candidate.item.get("genre")),
        normalize_text(candidate.item.get("subgenre")),
        *{
            normalize_text(value)
            for value in candidate.item.get("genres") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in candidate.item.get("discovery_genres") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in candidate.item.get("styles") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in candidate.item.get("tags") or []
            if normalize_text(value)
        },
    }
    candidate_genres.discard("")
    return bool(candidate_genres & _taste_genre_keys(taste))


def _because_you_played_relation(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
) -> tuple[bool, str]:
    """Validate anchor rows by entity relation, never title text alone."""

    item = dict(candidate.item or {})
    artist_key = normalize_text(artist_name(item))
    taste_artists = _taste_artist_keys(taste)
    related_artist_keys = _profile_string_set(
        taste,
        "artist_neighborhood_preferences",
        "peer_scene_keys",
        "peer_artist_keys",
    )
    if artist_key and artist_key in taste_artists:
        return True, "same_artist"
    if artist_key and artist_key in related_artist_keys:
        return True, "artist_neighbor"

    item_artist_graph = {
        normalize_text(value)
        for value in item.get("artist_graph_keys") or []
        if normalize_text(value)
    }
    if item_artist_graph & (taste_artists | related_artist_keys):
        return True, "artist_neighbor"

    item_id = track_id(item)
    title = normalize_text(item.get("title") or item.get("name"))
    album = normalize_text(album_name(item))
    item_keys = {
        normalize_text(item_id),
        normalize_text(item.get("canonical_track_identity")),
        normalize_text(item.get("canonical_source_identity")),
        normalize_text(f"{title}|{artist_key}"),
    }
    if album and artist_key:
        item_keys.add(f"album:{album}|{artist_key}")
    item_keys.discard("")
    history_keys = _history_track_identity_keys(taste)
    related_track_id = str(item.get("related_to_track") or "").strip()
    if (
        _candidate_recommendation_path(candidate) == "track_radio"
        and related_track_id
        and related_track_id in _history_track_ids(taste)
    ):
        return True, "track_radio"
    if item_keys & history_keys:
        return True, "same_album" if any(key.startswith("album:") for key in item_keys & history_keys) else "track_radio"

    candidate_genres = _item_genre_keys(item)
    if candidate_genres & _taste_genre_keys(taste):
        authority = normalize_text(item.get("source_authority"))
        sources = set(str(candidate.source or "").split("+"))
        graph_bridge = bool(
            item_artist_graph & (taste_artists | related_artist_keys)
            or sources & {"artist_graph"}
        )
        if graph_bridge and authority in TRUSTED_AUTHORITIES:
            return True, "same_genre_family"

    compatibility = candidate_profile_compatibility(
        candidate,
        taste,
        row_kind="because_you_played",
        relation_context="trusted_popular_neighbor",
        taste_match=_candidate_matches_taste(candidate, taste),
        strong_personal_match=_candidate_strong_personal_match(candidate, taste),
    )
    try:
        relation_strength = float(item.get("relation_strength") or 0.0)
    except (TypeError, ValueError):
        relation_strength = 0.0
    if (
        normalize_text(item.get("source_authority")) in TRUSTED_AUTHORITIES
        and compatibility.allowed
        and relation_strength >= 0.6
    ):
        item.update(compatibility.diagnostics())
        return True, "trusted_popular_neighbor"

    return False, "title_only_or_unrelated"


def _item_genre_keys(item: Dict[str, Any]) -> Set[str]:
    genres = {
        normalize_text(item.get("genre")),
        normalize_text(item.get("subgenre")),
        *{
            normalize_text(value)
            for value in item.get("genres") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in item.get("discovery_genres") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in item.get("styles") or []
            if normalize_text(value)
        },
        *{
            normalize_text(value)
            for value in item.get("tags") or []
            if normalize_text(value)
        },
    }
    genres.discard("")
    return genres


def _album_taste_relation(
    candidate: DiscoveryCandidate,
    item: Dict[str, Any],
    taste: TasteProfile,
    *,
    history_ids: Set[str],
    preferred_track_ids: Set[str] | None = None,
) -> Tuple[bool, str, float]:
    """Return whether an album is connected enough for home album rows.

    Album discovery can be broader than track rows, but it still needs a clear
    bridge to the listener. Editorial/popularity-only albums without artist,
    genre, or source-track evidence belong in explicit explore surfaces, not
    Albums For You.
    """

    artist_key = normalize_text(artist_name(item))
    taste_artists = _taste_artist_keys(taste)
    related_artist_keys = _profile_string_set(
        taste,
        "artist_neighborhood_preferences",
        "peer_scene_keys",
        "peer_artist_keys",
    )
    source_track_id = str(item.get("source_track_id") or "").strip()
    source_authority = normalize_text(item.get("source_authority"))
    album_segment = normalize_text(item.get("album_segment"))
    pool_source = normalize_text(item.get("candidate_pool_source"))
    album_source = normalize_text(item.get("album_source"))
    sources = set(str(candidate.source or "").split("+"))
    candidate_genres = _item_genre_keys(item)
    genre_match = bool(candidate_genres & _taste_genre_keys(taste))
    preferred_track_ids = set(preferred_track_ids or set())

    if artist_key and artist_key in taste_artists:
        return True, "known_artist", 1.6
    if source_track_id and source_track_id in history_ids:
        return True, "history_track_album", 1.25
    if source_track_id and source_track_id in preferred_track_ids:
        return True, "lane_track_album", 1.15
    if (
        item.get("artist_neighborhood") is True
        and (
            normalize_text(candidate.source) == "artist_graph"
            or _candidate_recommendation_path(candidate) == "artist_neighbor"
        )
    ):
        return True, "artist_neighborhood", 1.15
    if artist_key and artist_key in related_artist_keys:
        return True, "related_artist", 1.05
    item_artist_graph = {
        normalize_text(value)
        for value in item.get("artist_graph_keys") or []
        if normalize_text(value)
    }
    if item_artist_graph & (taste_artists | related_artist_keys):
        return True, "artist_graph", 1.0
    compatibility = candidate_profile_compatibility(
        candidate,
        taste,
        row_kind="recommended_albums",
        relation_context="same_genre_family",
        taste_match=genre_match,
        strong_personal_match=False,
    )
    try:
        relation_strength = float(item.get("relation_strength") or 0.0)
    except (TypeError, ValueError):
        relation_strength = 0.0
    if (
        album_segment in {"known_artist_albums", "adjacent_artist_albums", "classic_neighbor_albums"}
        or pool_source in {"known_artist_albums", "adjacent_artist_albums", "classic_neighbor_albums"}
    ) and compatibility.allowed and (genre_match or relation_strength >= 0.6):
        item.update(compatibility.diagnostics())
        return True, album_segment or pool_source, 0.5
    if (
        (album_segment == "genre_album_discovery" or pool_source == "genre_album_discovery")
        and compatibility.allowed
        and genre_match
    ):
        item.update(compatibility.diagnostics())
        return True, "genre_album_discovery", 0.45
    if (
        pool_source == "fresh_or_recent_albums"
        and compatibility.allowed
        and genre_match
        and relation_strength >= 0.55
    ):
        item.update(compatibility.diagnostics())
        return True, "fresh_genre_match", 0.45
    if album_source == "artist_catalog" and artist_key:
        return False, "artist_catalog_not_in_taste_graph", -1.75
    return False, "off_profile_album", -2.25


def _candidate_is_admitted(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
    *,
    row_kind: str,
) -> bool:
    _penalty, hard_suppressed = _negative_feedback_penalty(candidate.item, taste)
    recommendation_path = _candidate_recommendation_path(candidate)
    candidate.item["recommendation_path"] = recommendation_path
    strong_personal_match = _candidate_strong_personal_match(candidate, taste)
    taste_match = _candidate_matches_taste(candidate, taste)
    compatibility = candidate_profile_compatibility(
        candidate,
        taste,
        row_kind=row_kind,
        relation_context=recommendation_path,
        taste_match=taste_match,
        strong_personal_match=strong_personal_match,
        negative_suppressed=hard_suppressed,
    )
    candidate.item.update(compatibility.diagnostics())
    return compatibility.allowed


def _hint_score(text: str, positive: Sequence[str], negative: Sequence[str]) -> float:
    score = 0.0
    for hint in positive or ():
        if _contains_token_phrase(text, hint):
            score += 0.55
    for hint in negative or ():
        if _contains_token_phrase(text, hint):
            score -= 1.15
    return score


def _lane_score(
    candidate: DiscoveryCandidate,
    lane: LaneRecipe | None,
    taste: TasteProfile,
) -> Tuple[float, bool]:
    if lane is None or lane.lane_id == "all":
        return 0.0, True
    text = metadata_text(candidate.item)
    score = _hint_score(text, lane.positive_hints, lane.negative_hints)
    strong_history = candidate.source.startswith("history") or track_id(candidate.item) in _history_track_ids(taste)
    mood_axes = candidate.item.get("mood_axes") or candidate.item.get("audio_traits")
    mood_axes = mood_axes if isinstance(mood_axes, dict) else {}
    energy = _number(mood_axes.get("energy"))
    drive = _number(mood_axes.get("drive"))
    calmness = _number(mood_axes.get("calmness"))
    softness = _number(mood_axes.get("softness"))
    if lane.lane_id == "workout":
        has_negative = any(_contains_token_phrase(text, hint) for hint in lane.negative_hints)
        has_positive = any(_contains_token_phrase(text, hint) for hint in lane.positive_hints)
        if has_negative and not (strong_history and has_positive):
            return score - 4.0, False
        if energy and energy < 0.45 and not (strong_history and has_positive):
            return score - 3.0, False
        if not has_positive and any(_contains_token_phrase(text, term) for term in ("ballad", "slow", "ambient", "sleep")):
            return score - 2.0, False
        score += (energy + drive) * 0.8
    if lane.lane_id == "focus" and any(_contains_token_phrase(text, term) for term in ("party", "hardcore", "thrash")):
        return score - 2.0, False
    if lane.lane_id == "focus":
        score += calmness * 0.55
        score -= energy * 0.2
    if lane.lane_id == "chill":
        score += (calmness + softness) * 0.45
    return score, True


def _lane_positive_match(candidate: DiscoveryCandidate, lane_id: str) -> bool:
    lane = LANE_RECIPES.get(lane_id)
    if lane is None or lane_id == "all":
        return True
    text = metadata_text(candidate.item)
    return any(_contains_token_phrase(text, hint) for hint in lane.positive_hints)


def _item_lane_positive_match(item: Dict[str, Any], lane_id: str) -> bool:
    lane = LANE_RECIPES.get(lane_id)
    if lane is None or lane_id == "all":
        return True
    text = metadata_text(item)
    return any(_contains_token_phrase(text, hint) for hint in lane.positive_hints)


def _score_candidate(
    candidate: DiscoveryCandidate,
    *,
    row: RowRecipe,
    taste: TasteProfile,
    lane: LaneRecipe | None = None,
) -> Tuple[float, bool]:
    sources = candidate.source.split("+")
    score = float(candidate.score)
    score += sum(SOURCE_WEIGHTS.get(source, 0.0) for source in sources)
    if "collaborative" in sources:
        score += {
            "listenbrainz_first": 1.15,
            "blended": 0.45,
            "neatie": 0.0,
        }.get(taste.taste_mode, 0.0)
    score -= _number(candidate.item.get("discovery_quality_penalty"))
    feedback_penalty, hard_suppressed = _negative_feedback_penalty(candidate.item, taste)
    if hard_suppressed:
        return score - 8.0, False
    score -= feedback_penalty
    if feedback_penalty > 0:
        candidate.item["negative_feedback_penalty"] = round(feedback_penalty, 4)
    score += _number(candidate.item.get("feature_confidence")) * 0.25
    score += _number(candidate.item.get("popularity")) * 0.12
    authority = normalize_text(candidate.item.get("source_authority"))
    score += {
        "official": 0.45,
        "canonical": 0.35,
        "verified_catalog": 0.18,
        "unknown": -0.2,
        "search_only": -4.0,
    }.get(authority, -0.1)
    if row.kind in DISCOVERY_ROWS:
        score += _number(candidate.item.get("freshness")) * 0.12
        compatibility = candidate_profile_compatibility(
            candidate,
            taste,
            row_kind=row.kind,
            relation_context=normalize_text(candidate.item.get("recommendation_relation")),
            taste_match=_candidate_matches_taste(candidate, taste),
            strong_personal_match=_candidate_strong_personal_match(candidate, taste),
        )
        candidate.item.update(compatibility.diagnostics())
        if not compatibility.allowed:
            return score - 5.0, False
        score += (compatibility.score - 0.5) * 1.15
    artist_key = normalize_text(artist_name(candidate.item))
    if artist_key and artist_key in _taste_artist_keys(taste):
        score += 0.7
    item_id = track_id(candidate.item)
    in_history = item_id and item_id in _history_track_ids(taste)
    if row.kind in DISCOVERY_ROWS and in_history:
        score -= 1.1
    if taste.force_refresh and row.kind in REFRESH_MUTABLE_ROWS:
        avoid = _avoid_ids(taste)
        if item_id and item_id in avoid:
            score -= 4.5
        identity = candidate.item.get("canonical_track_identity") or candidate.item.get("canonical_source_identity")
        score += _refresh_jitter(taste, identity or item_id or item_signature(candidate.item)) * 0.55
    if row.kind == "last_played" and "last_played" in candidate.reasons:
        score += 2.0
    if row.kind == "frequently_listened" and ("frequent" in candidate.reasons or candidate.item in taste.top_tracks):
        score += 1.8
    text = metadata_text(candidate.item)
    score += _hint_score(text, ROW_INTENT_HINTS.get(row.ranking_intent, ()), ())
    if row.kind == "quiet_picks":
        if candidate.source in {"similarity", "artist_graph"}:
            score += 1.25
        if artist_key and artist_key in _taste_artist_keys(taste):
            score += 0.55
        if not _candidate_matches_taste(candidate, taste):
            score -= 4.0
    lane_delta, lane_allowed = _lane_score(candidate, lane, taste)
    return score + lane_delta, lane_allowed


def _select_diverse(
    scored: Sequence[Tuple[float, DiscoveryCandidate]],
    *,
    limit: int,
    row_kind: str,
    exclude_ids: Set[str] | None = None,
) -> List[Dict[str, Any]]:
    exclude_ids = set(exclude_ids or set())
    selected: List[Dict[str, Any]] = []
    seen_signatures: Set[str] = set()
    artist_counts: Dict[str, int] = defaultdict(int)
    album_counts: Dict[str, int] = defaultdict(int)
    max_same_artist = 3 if row_kind in {"last_played", "frequently_listened"} else 2
    max_same_album = 2 if row_kind in {"last_played", "frequently_listened"} else 1
    if row_kind in {"made_for_you", "because_you_played", "popular_radio"}:
        max_same_artist = 1
    if row_kind == "because_you_played":
        max_same_album = 2
    if row_kind == "quiet_picks":
        max_same_artist = 4
        max_same_album = 2
    for _score, candidate in scored:
        item = dict(candidate.item)
        item_id = track_id(item)
        if item_id and item_id in exclude_ids:
            continue
        signature = item_signature(item)
        if not signature or signature in seen_signatures:
            continue
        artist_key = normalize_text(artist_name(item))
        album_key = normalize_text(album_name(item))
        if artist_key and artist_counts[artist_key] >= max_same_artist:
            continue
        if album_key and album_counts[album_key] >= max_same_album:
            continue
        if candidate.reasons:
            item.setdefault("recommendation_reason", candidate.reasons[0])
        selected.append(item)
        seen_signatures.add(signature)
        if artist_key:
            artist_counts[artist_key] += 1
        if album_key:
            album_counts[album_key] += 1
        if len(selected) >= limit:
            break
    if row_kind == "quiet_picks" and len(selected) < limit:
        for _score, candidate in scored:
            item = dict(candidate.item)
            item_id = track_id(item)
            signature = item_signature(item)
            if (
                (item_id and item_id in exclude_ids)
                or not signature
                or signature in seen_signatures
            ):
                continue
            if candidate.reasons:
                item.setdefault("recommendation_reason", candidate.reasons[0])
            selected.append(item)
            seen_signatures.add(signature)
            if len(selected) >= limit:
                break
    return selected


def rank_tracks(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
    row: RowRecipe,
    *,
    limit: int | None = None,
    exclude_ids: Set[str] | None = None,
    lane_id: str = "all",
) -> List[Dict[str, Any]]:
    lane = LANE_RECIPES.get(lane_id)
    candidate_sources = row.candidate_sources
    if lane is not None and lane_id != "all" and any(pools.get(source) for source in lane.candidate_sources):
        candidate_sources = lane.candidate_sources
    candidates = _collect_candidates(pools, candidate_sources, item_type="track")
    scored: List[Tuple[float, DiscoveryCandidate]] = []
    history_ids = _history_track_ids(taste)
    for candidate in candidates:
        if row.kind in DISCOVERY_ROWS and track_id(candidate.item) in history_ids:
            continue
        if not _candidate_is_admitted(candidate, taste, row_kind=row.kind):
            continue
        if row.kind == "because_you_played":
            related, relation_reason = _because_you_played_relation(candidate, taste)
            if not related:
                continue
            candidate = DiscoveryCandidate(
                item={**candidate.item, "recommendation_relation": relation_reason},
                source=candidate.source,
                score=candidate.score,
                reasons=[relation_reason, *[reason for reason in candidate.reasons if reason != relation_reason]],
                item_type=candidate.item_type,
            )
        if row.kind == "quiet_picks" and not _candidate_matches_taste(candidate, taste):
            if _candidate_recommendation_path(candidate) not in {
                "track_radio",
                "artist_neighbor",
                "collaborative_neighbor",
            }:
                continue
        score, allowed = _score_candidate(candidate, row=row, taste=taste, lane=lane)
        if not allowed:
            continue
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    requested_limit = min(int(limit or row.max_items), row.max_items)
    if taste.taste_mode in {"blended", "listenbrainz_first"}:
        ratio = 0.3 if taste.taste_mode == "blended" else 0.6
        collaborative_cap = max(1, int(requested_limit * ratio + 0.999))
        bounded: List[Tuple[float, DiscoveryCandidate]] = []
        collaborative_count = 0
        for entry in scored:
            evidence = normalize_text(
                entry[1].item.get("relationship_evidence")
                or entry[1].item.get("relation_type")
            )
            is_listenbrainz_personal = evidence == "personal_collaborative"
            if is_listenbrainz_personal:
                if collaborative_count >= collaborative_cap:
                    continue
                collaborative_count += 1
            bounded.append(entry)
        scored = bounded
    return _select_diverse(
        scored,
        limit=requested_limit,
        row_kind=row.kind,
        exclude_ids=exclude_ids,
    )


def _rank_lane_rescue_tracks(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
    lane_id: str,
    *,
    limit: int,
    exclude_ids: Set[str] | None = None,
) -> List[Dict[str, Any]]:
    lane = LANE_RECIPES.get(lane_id)
    row = replace(
        ROW_RECIPES["made_for_you"],
        kind="home_lane",
        item_type="track",
        max_items=48,
    )
    if lane is None:
        return []
    candidates = _collect_candidates(pools, row.candidate_sources, item_type="track")
    scored: List[Tuple[float, DiscoveryCandidate]] = []
    for candidate in candidates:
        base_score, _allowed = _score_candidate(candidate, row=row, taste=taste, lane=None)
        lane_delta, lane_allowed = _lane_score(candidate, lane, taste)
        text = metadata_text(candidate.item)
        if lane_id == "workout" and not lane_allowed:
            continue
        if lane_id == "workout" and not _lane_positive_match(candidate, lane_id):
            base_score -= 0.65
        elif lane_id != "all" and lane_delta <= 0:
            base_score -= 0.45
        scored.append((base_score + lane_delta, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return _select_diverse(
        scored,
        limit=limit,
        row_kind=row.kind,
        exclude_ids=exclude_ids,
    )


def rank_albums(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
    row: RowRecipe,
    *,
    lane_id: str = "all",
    preferred_track_ids: Set[str] | None = None,
    exclude_album_keys: Set[str] | None = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    candidates = _collect_candidates(pools, row.candidate_sources, item_type="album")
    taste_artists = _taste_artist_keys(taste)
    history_ids = _history_track_ids(taste)
    scored: List[Tuple[float, DiscoveryCandidate]] = []
    has_release_metadata = False
    seen = set()
    lane = LANE_RECIPES.get(lane_id)
    preferred_track_ids = set(preferred_track_ids or set())
    exclude_album_keys = set(exclude_album_keys or set())
    listened_album_titles, listened_album_exact = _listened_album_keys(taste)
    for candidate in candidates:
        item = dict(candidate.item)
        if normalize_text(item.get("source_authority")) == "search_only":
            continue
        title = normalize_text(item.get("title"))
        artist = normalize_text(item.get("artist"))
        signature = normalize_text(item.get("id") or f"{title}|{artist}")
        if not title or signature in seen:
            continue
        feedback_penalty, hard_suppressed = _negative_feedback_penalty(item, taste)
        if hard_suppressed:
            continue
        album_keys = {
            signature,
            normalize_text(f"{title}|{artist}"),
        }
        album_keys.discard("")
        if album_keys & exclude_album_keys:
            continue
        release = str(
            item.get("release_year")
            or item.get("release_date")
            or item.get("year")
            or ""
        ).strip()
        has_release_metadata = has_release_metadata or bool(release)
        score = candidate.score + SOURCE_WEIGHTS.get(candidate.source, 0.0)
        score -= _number(item.get("discovery_quality_penalty"))
        score -= feedback_penalty
        if feedback_penalty > 0:
            item["negative_feedback_penalty"] = round(feedback_penalty, 4)
        authority = normalize_text(item.get("source_authority"))
        score += {
            "official": 0.65,
            "canonical": 0.5,
            "verified_catalog": 0.25,
            "unknown": -0.5,
        }.get(authority, -0.15)
        album_key = f"{title}|{artist}"
        if album_key in listened_album_exact:
            score -= 5.0
        elif title in listened_album_titles:
            score -= 2.25
        source_track_id = str(item.get("source_track_id") or "").strip()
        if row.kind in {"featured_new_albums", "recommended_albums"}:
            admitted, relation_reason, relation_bonus = _album_taste_relation(
                candidate,
                item,
                taste,
                history_ids=history_ids,
                preferred_track_ids=preferred_track_ids,
            )
            item["album_relation_reason"] = relation_reason
            item["album_relation_score"] = round(relation_bonus, 4)
            if not admitted and not taste.is_cold_start:
                continue
            if row.kind == "featured_new_albums" and not taste.is_cold_start:
                strong_featured_relations = {
                    "known_artist",
                    "history_track_album",
                    "lane_track_album",
                    "artist_neighborhood",
                    "related_artist",
                    "artist_graph",
                    "genre_match",
                    "verified_genre_match",
                    "fresh_genre_match",
                    "known_artist_albums",
                    "adjacent_artist_albums",
                    "classic_neighbor_albums",
                }
                if str(relation_reason or "").strip() not in strong_featured_relations:
                    continue
            score += relation_bonus
            if item.get("album_source") == "artist_catalog":
                score += 1.5
            if artist and artist in taste_artists:
                score += 0.35
            if source_track_id and source_track_id in history_ids:
                score -= 0.35
            if candidate.source in {"freshness", "popularity"}:
                score += 0.5
        elif artist and artist in taste_artists:
            score += 0.45
        if release:
            score += 0.35
        if taste.force_refresh and row.kind in REFRESH_MUTABLE_ROWS:
            if _album_identity_keys(item) & _avoid_ids(taste):
                score -= 4.5
            score += _refresh_jitter(
                taste,
                item.get("canonical_album_identity")
                or item.get("canonical_source_identity")
                or signature,
            ) * 0.65
        if lane is not None and lane.lane_id != "all":
            lane_text = metadata_text(item)
            preview_track = item.get("preview_track")
            if isinstance(preview_track, dict):
                lane_text = f"{lane_text} {metadata_text(preview_track)}"
            lane_delta = _hint_score(lane_text, lane.positive_hints, lane.negative_hints)
            source_track_id = str(item.get("source_track_id") or "").strip()
            preferred_album = bool(source_track_id and source_track_id in preferred_track_ids)
            if preferred_album:
                lane_delta += 2.0
            if lane_delta <= 0 and not preferred_album:
                continue
            score += lane_delta
        seen.add(signature)
        scored.append((score, DiscoveryCandidate(item=item, source=candidate.source, score=score, item_type="album")))
    scored.sort(key=lambda item: item[0], reverse=True)
    if row.kind == "recommended_albums":
        fresh_scored: List[Tuple[float, DiscoveryCandidate]] = []
        listened_scored: List[Tuple[float, DiscoveryCandidate]] = []
        for score_value, candidate in scored:
            item = candidate.item
            title = normalize_text(item.get("title"))
            artist = normalize_text(item.get("artist"))
            exact_key = f"{title}|{artist}" if title else ""
            if exact_key in listened_album_exact or title in listened_album_titles:
                listened_scored.append((score_value, candidate))
            else:
                fresh_scored.append((score_value, candidate))
        if fresh_scored:
            scored = [*fresh_scored, *listened_scored]
    output: List[Dict[str, Any]] = []
    artist_counts: Dict[str, int] = defaultdict(int)
    max_per_artist = 1 if row.kind == "featured_new_albums" else 2
    for _score, candidate in scored:
        item = dict(candidate.item)
        artist_key = normalize_text(item.get("artist"))
        if artist_key and artist_counts[artist_key] >= max_per_artist:
            continue
        output.append(item)
        if artist_key:
            artist_counts[artist_key] += 1
        if len(output) >= row.max_items:
            break
    return output, has_release_metadata


def rank_artists(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
    row: RowRecipe,
) -> List[Dict[str, Any]]:
    explicit_artists = _collect_candidates(pools, ("artist_graph",), item_type="artist")
    candidates = _collect_candidates(pools, ("artist_graph", "similarity", "genre_mood", "popularity"), item_type="track")
    counts: Counter[str] = Counter()
    thumbnails: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    ids: Dict[str, str] = {}
    taste_artists = _taste_artist_keys(taste)
    for candidate in explicit_artists:
        name = str(candidate.item.get("name") or candidate.item.get("artist") or "").strip()
        key = normalize_text(name)
        if not key:
            continue
        counts[key] += candidate.score + 2.0
        labels[key] = name
        ids[key] = str(candidate.item.get("id") or key)
        thumbnails.setdefault(key, str(candidate.item.get("thumbnail") or ""))
    for candidate in candidates:
        name = artist_name(candidate.item)
        key = normalize_text(name)
        if not key:
            continue
        weight = candidate.score + SOURCE_WEIGHTS.get(candidate.source.split("+")[0], 0.0)
        if key in taste_artists:
            weight += 0.35
        counts[key] += weight
        labels[key] = name
        thumbnails.setdefault(key, str(candidate.item.get("thumbnail") or ""))
    output: List[Dict[str, Any]] = []
    for key, score in counts.most_common(row.max_items):
        output.append(
            {
                "id": ids.get(key) or key,
                "name": labels.get(key) or key.title(),
                "thumbnail": thumbnails.get(key) or "",
                "score": float(score),
            }
        )
    return output


def _artists_from_tracks(tracks: Iterable[Dict[str, Any]], *, limit: int = 12) -> List[Dict[str, Any]]:
    artists: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for track in tracks or []:
        name = artist_name(track)
        key = normalize_text(name)
        if not key or key in seen:
            continue
        seen.add(key)
        artists.append(
            {
                "id": key,
                "name": name,
                "thumbnail": str(track.get("thumbnail") or ""),
            }
        )
        if len(artists) >= limit:
            break
    return artists


def build_personal_mixes(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    base = ROW_RECIPES["made_for_you"]
    definitions = [
        ("daily_mix_1", "Daily Mix 1", "Favorites and close neighbors.", ("profile_spine", "similarity", "artist_graph", "discovery_universe"), True),
        ("daily_mix_2", "Daily Mix 2", "Taste-shaped genre discoveries.", ("genre_mood", "similarity", "profile_spine", "discovery_universe"), True),
        ("daily_mix_3", "Daily Mix 3", "Adjacent artists and less-played picks.", ("artist_graph", "similarity", "profile_spine", "collaborative", "discovery_universe"), True),
        ("picked_again", "Picked again", "Familiar tracks worth returning to.", ("history", "profile_spine", "similarity"), False),
        ("fresh_for_you", "Fresh for you", "A wider exploration of your music orbit.", ("genre_mood", "popularity", "collaborative", "similarity", "discovery_universe"), True),
    ]
    min_tracks = 8
    target_tracks = 24
    max_deep_overlap = max(1, int(target_tracks * 0.20))
    track_counts: Dict[str, int] = {}
    candidate_counts: Dict[str, int] = {}
    rejection_reasons: Dict[str, str] = {}
    overlap_counts: Dict[str, int] = defaultdict(int)

    discovery_recipe = replace(
        base,
        kind="personal_mix_slice",
        item_type="track",
        candidate_sources=base.candidate_sources,
        max_items=96,
    )
    replay_recipe = replace(
        base,
        kind="personal_replay_slice",
        item_type="track",
        candidate_sources=("history", "profile_spine", "similarity"),
        max_items=96,
    )
    global_discovery = rank_tracks(pools, taste, discovery_recipe, limit=96)
    global_replay = rank_tracks(pools, taste, replay_recipe, limit=96)
    history_recipe = replace(
        replay_recipe,
        candidate_sources=("history",),
    )
    replay_history = rank_tracks(pools, taste, history_recipe, limit=48)

    candidate_overrides: Dict[str, List[Dict[str, Any]]] = {}
    discovery_visible_budget = max(len(global_discovery) - (4 * min_tracks), 0)
    extra_mix_count = min(
        max(base.max_items - len(definitions), 0),
        discovery_visible_budget // min_tracks,
    )
    genre_candidates: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for track in global_discovery:
        structured_genres = {
            normalize_text(value)
            for key in ("genres", "discovery_genres")
            for value in (
                track.get(key)
                if isinstance(track.get(key), (list, tuple, set))
                else [track.get(key)]
            )
            if normalize_text(value)
        }
        for genre in structured_genres:
            genre_candidates[genre].append(track)

    used_genre_signatures: Set[Tuple[str, ...]] = set()
    dynamic_index = 1
    for genre, tracks in sorted(
        genre_candidates.items(),
        key=lambda row: (-len(row[1]), row[0]),
    ):
        if dynamic_index > extra_mix_count or len(tracks) < min_tracks:
            continue
        candidate_signature = tuple(
            sorted(
                signature
                for track in tracks
                if (signature := normalize_text(
                    track.get("canonical_entity_id")
                    or track.get("canonical_track_identity")
                    or track.get("track_key")
                    or track_id(track)
                ))
            )
        )
        if not candidate_signature or candidate_signature in used_genre_signatures:
            continue
        used_genre_signatures.add(candidate_signature)
        mix_id = f"taste_cluster_{dynamic_index}"
        definitions.append(
            (
                mix_id,
                f"{genre.title()} Mix",
                f"More from your {genre} orbit.",
                ("genre_mood", "similarity", "artist_graph", "discovery_universe"),
                True,
            )
        )
        candidate_overrides[mix_id] = [*tracks, *global_discovery]
        dynamic_index += 1

    while dynamic_index <= extra_mix_count:
        mix_id = f"discovery_mix_{dynamic_index}"
        offset = ((dynamic_index - 1) * min_tracks) % max(len(global_discovery), 1)
        rotated = [*global_discovery[offset:], *global_discovery[:offset]]
        definitions.append(
            (
                mix_id,
                f"Discovery Mix {dynamic_index}",
                "More unplayed tracks connected to your taste.",
                ("similarity", "artist_graph", "genre_mood", "collaborative", "discovery_universe"),
                True,
            )
        )
        candidate_overrides[mix_id] = rotated
        dynamic_index += 1

    def signature(track: Dict[str, Any]) -> str:
        canonical = normalize_text(
            track.get("canonical_entity_id")
            or track.get("canonical_track_identity")
            or track.get("canonical_source_identity")
            or track.get("track_key")
        )
        return canonical or item_signature(track)

    candidate_lists: Dict[str, List[Dict[str, Any]]] = {}
    selected: Dict[str, List[Dict[str, Any]]] = {
        mix_id: [] for mix_id, _title, _subtitle, _sources, _discovery in definitions
    }
    selected_signatures: Dict[str, Set[str]] = {
        mix_id: set() for mix_id in selected
    }
    for mix_id, _title, _subtitle, sources, discovery_mix in definitions:
        recipe = replace(
            base,
            kind="personal_mix_slice" if discovery_mix else "personal_replay_slice",
            item_type="track",
            candidate_sources=sources,
            max_items=96,
        )
        candidate_counts[mix_id] = len(_collect_candidates(pools, sources, item_type="track"))
        preferred_tracks = candidate_overrides.get(mix_id) or rank_tracks(
            pools,
            taste,
            recipe,
            limit=96,
        )
        fallback = (
            global_discovery
            if discovery_mix
            else [*replay_history, *global_replay, *global_discovery]
        )
        ranked_inputs = (
            [*preferred_tracks, *fallback]
            if discovery_mix
            else [*replay_history, *preferred_tracks, *fallback]
        )
        merged: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for track in ranked_inputs:
            track_signature = signature(track)
            if not track_signature or track_signature in seen:
                continue
            seen.add(track_signature)
            merged.append(track)
        candidate_lists[mix_id] = merged

    # Reserve the visible eight tracks for every card together. This prevents
    # the first mix from consuming the only viable tracks for later mixes.
    visible_used: Set[str] = set()
    for _slot in range(min_tracks):
        for mix_id, _title, _subtitle, _sources, _discovery in definitions:
            match = next(
                (
                    track
                    for track in candidate_lists[mix_id]
                    if (track_signature := signature(track))
                    and track_signature not in visible_used
                    and track_signature not in selected_signatures[mix_id]
                ),
                None,
            )
            if match is None:
                rejection_reasons[mix_id] = (
                    f"below_min_unique_tracks:{len(selected[mix_id])}/{min_tracks}"
                )
                continue
            track_signature = signature(match)
            selected[mix_id].append(match)
            selected_signatures[mix_id].add(track_signature)
            visible_used.add(track_signature)

    occurrence_counts: Counter[str] = Counter(visible_used)
    complete_mix_ids = [
        mix_id for mix_id, tracks in selected.items() if len(tracks) >= min_tracks
    ]

    # Grow deeper catalogues with unused tracks first, shared tracks second.
    # Shared deep tracks are bounded and no recording can occur in more than
    # two mixes.
    for mix_id in complete_mix_ids:
        for track in candidate_lists[mix_id]:
            if len(selected[mix_id]) >= target_tracks:
                break
            track_signature = signature(track)
            if (
                not track_signature
                or track_signature in selected_signatures[mix_id]
                or occurrence_counts[track_signature] > 0
            ):
                continue
            selected[mix_id].append(track)
            selected_signatures[mix_id].add(track_signature)
            occurrence_counts[track_signature] += 1
    for mix_id in complete_mix_ids:
        for track in candidate_lists[mix_id]:
            if (
                len(selected[mix_id]) >= target_tracks
                or overlap_counts[mix_id] >= max_deep_overlap
                or (overlap_counts[mix_id] + 1)
                / max(len(selected[mix_id]) + 1, 1)
                > 0.20
            ):
                break
            track_signature = signature(track)
            if (
                not track_signature
                or track_signature in selected_signatures[mix_id]
                or occurrence_counts[track_signature] <= 0
                or occurrence_counts[track_signature] >= 2
            ):
                continue
            selected[mix_id].append(track)
            selected_signatures[mix_id].add(track_signature)
            occurrence_counts[track_signature] += 1
            overlap_counts[mix_id] += 1

    mixes: List[Dict[str, Any]] = []
    history_ids = _history_track_ids(taste)
    discovery_track_total = 0
    discovery_unplayed_total = 0
    for mix_id, title, subtitle, _sources, discovery_mix in definitions:
        tracks = selected[mix_id]
        if len(tracks) < min_tracks:
            rejection_reasons.setdefault(
                mix_id,
                f"below_min_tracks:{len(tracks)}/{min_tracks}",
            )
            continue
        first = tracks[0]
        mixes.append(
            {
                "id": mix_id,
                "title": title,
                "subtitle": subtitle,
                "description": subtitle,
                "thumbnail": str(first.get("thumbnail") or first.get("image") or ""),
                "tracks": tracks,
                "items": tracks,
                "track_count": len(tracks),
            }
        )
        track_counts[mix_id] = len(tracks)
        if discovery_mix:
            discovery_track_total += len(tracks)
            discovery_unplayed_total += sum(
                1 for track in tracks if track_id(track) not in history_ids
            )
    return mixes, {
        "mix_count": len(mixes),
        "dynamic_mix_count": sum(
            1 for mix in mixes if str(mix.get("id") or "") in candidate_overrides
        ),
        "requested_mix_count": len(definitions),
        "global_candidate_count": len(_collect_candidates(pools, base.candidate_sources, item_type="track")),
        "global_ranked_track_count": len(global_discovery),
        "mix_candidate_counts": candidate_counts,
        "mix_track_counts": track_counts,
        "mix_deep_overlap_counts": dict(overlap_counts),
        "mix_rejection_reasons": rejection_reasons,
        "unique_visible_tracks": len(visible_used),
        "unique_mix_tracks": len(occurrence_counts),
        "discovery_mix_unplayed_ratio": round(
            discovery_unplayed_total / max(discovery_track_total, 1),
            4,
        ),
    }


def build_popular_radio_cards(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    del taste
    row = ROW_RECIPES["popular_radio"]
    cards = [
        dict(candidate.item or {})
        for candidate in pools.get("popular_radio_cards", []) or []
        if candidate.item_type == "radio"
    ][: row.max_items]
    return cards, {
        "source": "artist_radio_inventory",
        "card_count": len(cards),
        "direct_card_count": sum(
            1 for card in cards if card.get("seed_affinity") == "direct"
        ),
        "similar_card_count": sum(
            1 for card in cards if card.get("seed_affinity") == "similar"
        ),
    }
def build_home_lanes(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    lane_row = replace(
        ROW_RECIPES["made_for_you"],
        kind="home_lane",
        item_type="track",
        max_items=48,
    )
    lanes: Dict[str, Dict[str, Any]] = {}
    rejection_reasons: List[str] = []
    lane_item_counts: Dict[str, int] = {}
    prior_visible_lanes: Dict[str, List[str]] = {}
    lane_pool_counts: Dict[str, int] = {}
    lane_pool_selected_counts: Dict[str, int] = {}
    used_non_all_ids: Set[str] = set()
    for lane_id in LANE_ORDER:
        exclude: Set[str] = set()
        if lane_id != "all":
            exclude.update(used_non_all_ids)
        tracks = rank_tracks(
            pools,
            taste,
            lane_row,
            limit=LANE_RECIPES[lane_id].target_items,
            exclude_ids=exclude,
            lane_id=lane_id,
        )
        lane_source = f"lane_{lane_id}"
        lane_pool_counts[lane_id] = len(pools.get(lane_source, []) or [])
        lane_pool_ids = {
            track_id(candidate.item)
            for candidate in pools.get(lane_source, []) or []
            if track_id(candidate.item)
        }
        if len(tracks) < LANE_RECIPES[lane_id].min_items:
            tracks = _rank_lane_rescue_tracks(
                pools,
                taste,
                lane_id,
                limit=LANE_RECIPES[lane_id].target_items,
                exclude_ids=exclude,
            )
        lane_pool_selected_counts[lane_id] = sum(
            1 for track in tracks if track_id(track) in lane_pool_ids
        )
        if lane_id != "all" and len(tracks) >= LANE_RECIPES[lane_id].min_items:
            positive_tracks = [track for track in tracks if _item_lane_positive_match(track, lane_id)]
            if len(positive_tracks) >= max(4, LANE_RECIPES[lane_id].min_items // 2):
                positive_ids = {track_id(track) for track in positive_tracks if track_id(track)}
                tracks = [
                    *positive_tracks,
                    *[track for track in tracks if track_id(track) not in positive_ids],
                ][: LANE_RECIPES[lane_id].target_items]
        visible_track_ids = {track_id(track) for track in tracks if track_id(track)}
        lane_albums, _has_release_metadata = rank_albums(
            pools,
            taste,
            ROW_RECIPES["recommended_albums"],
            lane_id=lane_id,
            preferred_track_ids=visible_track_ids,
        )
        discovery_start = min(max(LANE_RECIPES[lane_id].min_items // 2, 6), len(tracks))
        lanes[lane_id] = {
            "tracks": tracks,
            "discoveries": tracks[discovery_start:],
            "albums": lane_albums[:12],
            "artists": _artists_from_tracks(tracks, limit=12),
        }
        lane_item_counts[lane_id] = len(tracks)
        prior_visible_lanes[lane_id] = [track_id(track) for track in tracks[:12] if track_id(track)]
        if lane_id != "all":
            used_non_all_ids.update(prior_visible_lanes[lane_id][:12])
        if len(tracks) < LANE_RECIPES[lane_id].min_items:
            rejection_reasons.append(f"{lane_id}:below_min_items")

    for left, right in (("chill", "workout"), ("chill", "focus"), ("workout", "focus")):
        left_ids = prior_visible_lanes.get(left, [])
        right_ids = prior_visible_lanes.get(right, [])
        visible = max(min(len(left_ids), len(right_ids), 12), 1)
        overlap = len(set(left_ids[:12]) & set(right_ids[:12]))
        if (visible - overlap) / visible < 0.60:
            rejection_reasons.append(f"{left}_{right}:too_similar")

    accepted = not rejection_reasons and all(lane_id in lanes for lane_id in LANE_ORDER)
    diagnostics = {
        "accepted": accepted,
        "min_count": 12,
        "lane_item_counts": lane_item_counts,
        "lane_pool_counts": lane_pool_counts,
        "lane_pool_selected_counts": lane_pool_selected_counts,
        "rejection_reasons": rejection_reasons,
    }
    return lanes, diagnostics


def _finalize_allocated_rows(
    rows: List[DiscoveryRow],
    row_status: Dict[str, Dict[str, Any]],
) -> Tuple[List[DiscoveryRow], Dict[str, Dict[str, Any]]]:
    final_rows: List[DiscoveryRow] = []
    for row in rows:
        if row.item_type in {"mix", "radio"}:
            populated = []
            for container in row.items or []:
                nested = container.get("tracks") or container.get("items")
                if not isinstance(nested, list) or nested:
                    populated.append(container)
            row.items = populated
        if not row.items:
            previous = dict(row_status.get(row.kind) or {})
            row_status[row.kind] = {
                **previous,
                "status": "filtered_out",
                "reason": "empty_after_allocation",
                "count": 0,
                "warnings": list((row.meta or {}).get("quality_warnings") or []),
            }
            continue
        warnings = list((row.meta or {}).get("quality_warnings") or [])
        if len(row.items) < ROW_RECIPES[row.kind].min_items:
            warnings.append("below_min_items_after_allocation")
        previous = dict(row_status.get(row.kind) or {})
        row_status[row.kind] = {
            **previous,
            "status": "emitted",
            "count": len(row.items),
            "warnings": list(dict.fromkeys(warnings)),
        }
        final_rows.append(row)
    return final_rows, row_status


def build_rows_from_pools(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
) -> Tuple[List[DiscoveryRow], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    rows: List[DiscoveryRow] = []
    row_status: Dict[str, Dict[str, Any]] = {}
    selected_recent_ids: Set[str] = set()
    featured_album_keys: Set[str] = set()

    home_lanes, home_tab_diagnostics = build_home_lanes(pools, taste)

    for row_kind, row in ROW_RECIPES.items():
        if row.kind == "made_for_you":
            items, mix_diagnostics = build_personal_mixes(pools, taste)
            if not items:
                row_status[row_kind] = {
                    "status": "filtered_out",
                    "reason": "empty",
                    "count": len(items),
                    "diagnostics": mix_diagnostics,
                }
                continue
            warnings = ["below_target_mixes"] if len(items) < row.min_items else []
            rows.append(_row_payload(row, items, meta={"mix_diagnostics": mix_diagnostics, "quality_warnings": warnings}))
            row_status[row_kind] = {
                "status": "emitted",
                "count": len(items),
                "diagnostics": mix_diagnostics,
                "warnings": warnings,
            }
            continue
        if row.item_type == "album":
            excluded_album_keys = (
                featured_album_keys if row.kind == "recommended_albums" else set()
            )
            items, has_release_metadata = rank_albums(
                pools,
                taste,
                row,
                exclude_album_keys=excluded_album_keys,
            )
            title = "Featured new albums" if row.kind == "featured_new_albums" and has_release_metadata else row.title
            if len(items) < row.min_items:
                row_status[row_kind] = {"status": "filtered_out", "reason": "below_min_items", "count": len(items)}
                continue
            if row.kind == "featured_new_albums":
                for item in items:
                    normalized_title = normalize_text(item.get("title"))
                    normalized_artist = normalize_text(item.get("artist"))
                    featured_album_keys.update(
                        {
                            normalize_text(item.get("id")),
                            normalize_text(f"{normalized_title}|{normalized_artist}"),
                        }
                    )
                featured_album_keys.discard("")
            meta = {
                "has_release_metadata": has_release_metadata,
                "excluded_featured_album_count": (
                    len(featured_album_keys)
                    if row.kind == "recommended_albums"
                    else 0
                ),
            }
            rows.append(_row_payload(row, items, title=title, meta=meta))
            row_status[row_kind] = {"status": "emitted", "count": len(items)}
            continue

        if row.item_type == "artist":
            items = rank_artists(pools, taste, row)
            if len(items) < row.min_items:
                row_status[row_kind] = {"status": "filtered_out", "reason": "below_min_items", "count": len(items)}
                continue
            rows.append(_row_payload(row, items))
            row_status[row_kind] = {"status": "emitted", "count": len(items)}
            continue

        if row.item_type == "radio":
            items, radio_diagnostics = build_popular_radio_cards(pools, taste)
            if len(items) < row.min_items:
                row_status[row_kind] = {
                    "status": "filtered_out",
                    "reason": "below_min_items",
                    "count": len(items),
                    "diagnostics": radio_diagnostics,
                }
                continue
            warnings = ["popular_radio_below_target"] if len(items) < row.min_items else []
            rows.append(_row_payload(row, items, meta={"radio_diagnostics": radio_diagnostics, "quality_warnings": warnings}))
            row_status[row_kind] = {
                "status": "emitted",
                "count": len(items),
                "diagnostics": radio_diagnostics,
                "warnings": warnings,
            }
            continue

        if row.kind == "last_played":
            if len(taste.last_played_tracks) < row.min_items:
                row_status[row_kind] = {
                    "status": "not_applicable",
                    "reason": "insufficient_history",
                    "count": len(taste.last_played_tracks),
                }
                continue
            items = rank_tracks(pools, taste, row, limit=row.max_items)
        elif row.kind == "because_you_played" and not (
            taste.full_history_tracks or taste.recent_tracks or taste.anchor_tracks
        ):
            row_status[row_kind] = {
                "status": "not_applicable",
                "reason": "no_play_anchor",
                "count": 0,
            }
            continue
        elif row.kind == "frequently_listened":
            if len(taste.frequent_tracks) < row.min_items:
                row_status[row_kind] = {
                    "status": "not_applicable",
                    "reason": "insufficient_qualified_plays",
                    "count": len(taste.frequent_tracks),
                }
                continue
            frequent_seen: Set[str] = set()
            items = []
            for track in sorted(
                [dict(track) for track in taste.frequent_tracks],
                key=lambda track: (
                    int(track.get("play_count") or 0),
                    float(track.get("last_played_at") or 0.0),
                ),
                reverse=True,
            ):
                identity = normalize_text(
                    track.get("canonical_entity_id")
                    or track.get("canonical_track_identity")
                    or item_signature(track)
                )
                if not identity or identity in frequent_seen:
                    continue
                frequent_seen.add(identity)
                items.append(track)
                if len(items) >= row.max_items:
                    break
        elif row.kind == "quiet_picks":
            items = rank_tracks(
                pools,
                taste,
                row,
                limit=row.max_items,
                exclude_ids=selected_recent_ids,
            )
        else:
            items = rank_tracks(pools, taste, row, limit=row.max_items, exclude_ids=selected_recent_ids)

        if not items:
            row_status[row_kind] = {"status": "filtered_out", "reason": "below_min_items", "count": len(items)}
            continue
        warnings = ["below_target_items"] if len(items) < row.min_items else []
        rows.append(_row_payload(row, items, meta={"quality_warnings": warnings}))
        row_status[row_kind] = {"status": "emitted", "count": len(items), "warnings": warnings}
        if row.kind in {"todays_pick", "made_for_you", "because_you_played"}:
            for item in items[:8]:
                item_id = track_id(item)
                if item_id:
                    selected_recent_ids.add(item_id)
    rows, allocation_diagnostics = allocate_home_rows(rows, taste)
    rows, row_status = _finalize_allocated_rows(rows, row_status)
    home_tab_diagnostics = {
        **dict(home_tab_diagnostics or {}),
        "allocation": allocation_diagnostics,
    }
    return rows, row_status, home_lanes, home_tab_diagnostics


def _row_payload(
    recipe: RowRecipe,
    items: List[Dict[str, Any]],
    *,
    title: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> DiscoveryRow:
    visible = items[: recipe.max_items]
    row_meta = dict(meta or {})
    row_meta.update(
        {
            "page_size": recipe.page_size,
            "prepared_count": len(visible),
            "reserve_count": max(len(visible) - recipe.page_size, 0) if recipe.can_page else 0,
        }
    )
    has_more = bool(recipe.can_page and len(visible) > recipe.page_size)
    return DiscoveryRow(
        id=recipe.kind,
        title=title or recipe.title,
        kind=recipe.kind,
        item_type=recipe.item_type,
        row_style=recipe.row_style,
        items=visible,
        meta=row_meta,
        next_offset=min(recipe.page_size, len(visible)),
        has_more=has_more,
    )


def _dedupe_track_dicts(items: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen = set()
    for item in items or []:
        signature = item_signature(item)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        output.append(dict(item))
        if len(output) >= limit:
            break
    return output
