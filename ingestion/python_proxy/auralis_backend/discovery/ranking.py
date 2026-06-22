from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple
import hashlib

from .candidates import album_name, artist_name, item_signature, metadata_text, normalize_text, track_id
from .config import LANE_ORDER, LANE_RECIPES, ROW_RECIPES
from .schema import DiscoveryCandidate, DiscoveryRow, LaneRecipe, RowRecipe, TasteProfile


SOURCE_WEIGHTS = {
    "history": 0.9,
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
    "genre_discovery": ("rock", "pop", "soul", "dance", "blues", "metal", "jazz"),
    "taste_discovery": ("favorite", "classic", "official", "hit", "session", "live", "popular"),
    "novelty_discovery": ("indie", "deep", "rare", "hidden", "cover", "live", "session"),
}

DISCOVERY_ROWS = {
    "todays_pick",
    "made_for_you",
    "because_you_played",
    "trending_by_genre",
    "quiet_picks",
    "hidden_gems",
}

EXPLORATORY_SOURCES = {
    "ytmusic_home",
    "popularity",
    "collaborative",
    "discovery_universe",
    "genre_mood",
    "lane_chill",
    "lane_workout",
    "lane_focus",
    "lane_mood",
}

GLOBAL_REGION_KEYS = {"global", "world", "international"}
REFRESH_MUTABLE_ROWS = {
    "todays_pick",
    "featured_new_albums",
    "made_for_you",
    "because_you_played",
    "trending_by_genre",
    "recommended_albums",
    "recommended_artists",
    "quiet_picks",
    "hidden_gems",
    "home_lane",
    "personal_mix_slice",
}


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
        for key in ("genre", "subgenre"):
            value = normalize_text(track.get(key))
            if value:
                genres.add(value)
        for value in track.get("genres") or []:
            normalized = normalize_text(value)
            if normalized:
                genres.add(normalized)
    for cluster in _profile_string_set(taste, "scene_cluster_scores", "scene_cluster_ids"):
        if cluster.startswith("genre:"):
            genres.add(cluster.split(":", 1)[1])
    return genres


def _taste_language_keys(taste: TasteProfile) -> Set[str]:
    values = _profile_string_set(
        taste,
        "supported_languages",
        "dominant_language",
        "languages",
    )
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        value = normalize_text(track.get("language"))
        if value and value != "unknown":
            values.add(value)
    return values


def _taste_region_keys(taste: TasteProfile) -> Set[str]:
    values = _profile_string_set(
        taste,
        "supported_regions",
        "regions",
        "dominant_region",
        "region",
    )
    for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks:
        value = normalize_text(track.get("region"))
        if value and value != "unknown":
            values.add(value)
    return values


def _candidate_strong_personal_match(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
) -> bool:
    artist_key = normalize_text(artist_name(candidate.item))
    if artist_key and artist_key in _taste_artist_keys(taste):
        return True
    if candidate.item.get("artist_neighborhood") is True:
        return True
    item_id = track_id(candidate.item)
    return bool(item_id and item_id in _history_track_ids(taste))


def _candidate_language_compatible(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
) -> bool:
    supported_languages = _taste_language_keys(taste)
    language = normalize_text(candidate.item.get("language"))
    if not supported_languages or not language or language == "unknown":
        return True
    return language in supported_languages


def _candidate_region_compatible(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
) -> bool:
    supported_regions = _taste_region_keys(taste)
    region = normalize_text(candidate.item.get("region"))
    if not supported_regions or not region or region == "unknown":
        return True
    if region in supported_regions:
        return True
    return bool(region in GLOBAL_REGION_KEYS)


def _candidate_matches_taste(candidate: DiscoveryCandidate, taste: TasteProfile) -> bool:
    artist_key = normalize_text(artist_name(candidate.item))
    if artist_key and artist_key in _taste_artist_keys(taste):
        return True
    if candidate.item.get("artist_neighborhood") is True:
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
    }
    candidate_genres.discard("")
    return bool(candidate_genres & _taste_genre_keys(taste))


def _item_genre_keys(item: Dict[str, Any]) -> Set[str]:
    genres = {
        normalize_text(item.get("genre")),
        normalize_text(item.get("subgenre")),
        *{
            normalize_text(value)
            for value in item.get("genres") or []
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
    if item.get("artist_neighborhood") is True:
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
    if genre_match and _candidate_language_compatible(candidate, taste) and _candidate_region_compatible(candidate, taste):
        return True, "genre_match", 0.65
    if genre_match and source_authority in {"official", "canonical", "verified_catalog"}:
        return True, "verified_genre_match", 0.85
    if (
        album_segment in {"known_artist_albums", "adjacent_artist_albums", "classic_neighbor_albums"}
        or pool_source in {"known_artist_albums", "adjacent_artist_albums", "classic_neighbor_albums"}
    ) and (
        genre_match
        or source_authority in {"official", "canonical", "verified_catalog"}
        or pool_source == "adjacent_artist_albums"
    ):
        return True, album_segment or pool_source, 0.5
    if (
        pool_source == "fresh_or_recent_albums"
        and (genre_match or source_authority in {"official", "canonical", "verified_catalog"})
    ):
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
    if row_kind not in DISCOVERY_ROWS:
        return True
    if normalize_text(candidate.item.get("source_authority")) == "search_only":
        return False
    sources = set(candidate.source.split("+"))
    exploratory = bool(sources & EXPLORATORY_SOURCES)
    if exploratory and not taste.is_cold_start:
        source_authority = normalize_text(candidate.item.get("source_authority"))
        if (
            source_authority in {"", "unknown"}
            and not _candidate_strong_personal_match(candidate, taste)
            and not _candidate_matches_taste(candidate, taste)
        ):
            return False
        if (
            not _candidate_language_compatible(candidate, taste)
            or not _candidate_region_compatible(candidate, taste)
        ) and not _candidate_strong_personal_match(candidate, taste):
            return False
    universe_origin = normalize_text(candidate.item.get("discovery_origin"))
    weak_only = bool(sources) and sources.issubset(
        {"ytmusic_home", "popularity", "collaborative", "discovery_universe"}
    )
    if "discovery_universe" in sources and universe_origin:
        weak_only = universe_origin in {"ytmusic_home", "popularity", "collaborative"}
    if not weak_only:
        return True
    if not (
        taste.recent_tracks
        or taste.top_tracks
        or taste.anchor_tracks
        or taste.artist_hints
        or taste.top_artists
        or taste.listened_artists
        or taste.taste_queries
    ):
        return True
    return _candidate_matches_taste(candidate, taste)


def _hint_score(text: str, positive: Sequence[str], negative: Sequence[str]) -> float:
    score = 0.0
    for hint in positive or ():
        if normalize_text(hint) in text:
            score += 0.55
    for hint in negative or ():
        if normalize_text(hint) in text:
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
        has_negative = any(normalize_text(hint) in text for hint in lane.negative_hints)
        has_positive = any(normalize_text(hint) in text for hint in lane.positive_hints)
        if has_negative and not (strong_history and has_positive):
            return score - 4.0, False
        if energy and energy < 0.45 and not (strong_history and has_positive):
            return score - 3.0, False
        if not has_positive and any(term in text for term in ("ballad", "slow", "ambient", "sleep")):
            return score - 2.0, False
        score += (energy + drive) * 0.8
    if lane.lane_id == "focus" and any(term in text for term in ("party", "hardcore", "thrash")):
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
    return any(normalize_text(hint) in text for hint in lane.positive_hints)


def _item_lane_positive_match(item: Dict[str, Any], lane_id: str) -> bool:
    lane = LANE_RECIPES.get(lane_id)
    if lane is None or lane_id == "all":
        return True
    text = metadata_text(item)
    return any(normalize_text(hint) in text for hint in lane.positive_hints)


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
    score -= _number(candidate.item.get("discovery_quality_penalty"))
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
        language = normalize_text(candidate.item.get("language"))
        supported_languages = _taste_language_keys(taste)
        if language and language != "unknown" and supported_languages:
            score += 0.3 if language in supported_languages else -1.8
        region = normalize_text(candidate.item.get("region"))
        supported_regions = _taste_region_keys(taste)
        if region and region != "unknown" and supported_regions:
            score += 0.18 if region in supported_regions else -0.35
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
    if row.kind == "hidden_gems":
        if candidate.source == "popularity":
            score += 0.2
        if "live" in text or "session" in text or "cover" in text:
            score += 0.7
        if artist_key and artist_key in _taste_artist_keys(taste):
            score -= 0.45
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
    if row_kind in {"made_for_you", "because_you_played", "hidden_gems", "trending_by_genre"}:
        max_same_artist = 1
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
    for candidate in candidates:
        if not _candidate_is_admitted(candidate, taste, row_kind=row.kind):
            continue
        if row.kind == "quiet_picks" and not _candidate_matches_taste(candidate, taste):
            continue
        score, allowed = _score_candidate(candidate, row=row, taste=taste, lane=lane)
        if not allowed:
            continue
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return _select_diverse(
        scored,
        limit=min(int(limit or row.max_items), row.max_items),
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
        album_keys = {
            signature,
            normalize_text(f"{title}|{artist}"),
        }
        album_keys.discard("")
        if album_keys & exclude_album_keys:
            continue
        release = str(item.get("release_date") or item.get("year") or "").strip()
        has_release_metadata = has_release_metadata or bool(release)
        score = candidate.score + SOURCE_WEIGHTS.get(candidate.source, 0.0)
        score -= _number(item.get("discovery_quality_penalty"))
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
    definitions = (
        ("daily_mix_1", "Daily Mix 1", "Favorites and close neighbors.", ("similarity", "artist_graph", "discovery_universe")),
        ("daily_mix_2", "Daily Mix 2", "Taste-shaped genre discoveries.", ("genre_mood", "similarity", "discovery_universe")),
        ("daily_mix_3", "Daily Mix 3", "Adjacent artists and less-played picks.", ("artist_graph", "collaborative", "popularity", "discovery_universe")),
        ("picked_again", "Picked again", "Familiar tracks with related discoveries.", ("history", "similarity", "artist_graph")),
        ("fresh_for_you", "Fresh for you", "A wider exploration of your music orbit.", ("genre_mood", "popularity", "collaborative", "discovery_universe")),
    )
    mixes: List[Dict[str, Any]] = []
    used_ids: Set[str] = set()
    track_counts: Dict[str, int] = {}
    overlap_counts: Dict[str, int] = {}
    for mix_id, title, subtitle, sources in definitions:
        recipe = replace(
            base,
            kind="personal_mix_slice",
            item_type="track",
            candidate_sources=sources,
            max_items=32,
        )
        tracks = rank_tracks(
            pools,
            taste,
            recipe,
            limit=32,
            exclude_ids=used_ids,
        )
        if len(tracks) < 8:
            tracks = rank_tracks(pools, taste, recipe, limit=32)
        if len(tracks) < 8:
            continue
        ids = {track_id(track) for track in tracks if track_id(track)}
        prior_overlap = len(ids & used_ids)
        if used_ids and prior_overlap / max(len(ids), 1) > 0.40:
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
        overlap_counts[mix_id] = prior_overlap
        used_ids.update(ids)
        if len(mixes) >= base.max_items:
            break
    return mixes, {
        "mix_count": len(mixes),
        "mix_track_counts": track_counts,
        "mix_prior_overlap_counts": overlap_counts,
        "unique_mix_tracks": len(used_ids),
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


def _pages(items: List[Dict[str, Any]], size: int = 3) -> List[List[Dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_trending_genre_tabs(
    pools: Dict[str, List[DiscoveryCandidate]],
    taste: TasteProfile,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    genre_row = ROW_RECIPES["trending_by_genre"]
    genre_hints = {
        "rock": ("rock", "guitar", "classic rock", "alternative", "metal"),
        "pop": ("pop", "dance", "synth", "hit"),
        "soul": ("soul", "rnb", "r&b", "funk"),
        "blues": ("blues", "muddy", "clapton", "bb king"),
        "electronic": ("electronic", "edm", "house", "techno", "dance"),
        "jazz": ("jazz", "swing", "bebop"),
    }
    all_candidates = _collect_candidates(pools, genre_row.candidate_sources, item_type="track")
    scored_genres: List[Tuple[int, str]] = []
    taste_text = " ".join(
        metadata_text(track) for track in taste.recent_tracks + taste.top_tracks + taste.last_played_tracks
    )
    for genre, hints in genre_hints.items():
        provenance_count = sum(
            1
            for candidate in all_candidates
            if genre in {normalize_text(value) for value in candidate.item.get("discovery_genres") or []}
        )
        score = sum(1 for hint in hints if hint in taste_text) + provenance_count
        scored_genres.append((score, genre))
    scored_genres.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ordered_genres = [genre for score, genre in scored_genres if score > 0][:5]

    tabs: List[Dict[str, Any]] = []
    used_ids: Set[str] = set()
    for genre in ordered_genres[:5]:
        hints = genre_hints.get(genre, ())
        scored: List[Tuple[float, DiscoveryCandidate]] = []
        for candidate in all_candidates:
            text = _native_genre_text(candidate.item)
            provenance = {
                normalize_text(value)
                for value in candidate.item.get("discovery_genres") or []
                if normalize_text(value)
            }
            genre_score = _hint_score_for_genre(text, hints)
            if genre_score <= 0:
                continue
            if genre in provenance:
                genre_score += 2.5
            row_score, allowed = _score_candidate(candidate, row=genre_row, taste=taste)
            if not allowed:
                continue
            scored.append((row_score + genre_score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        tracks = _select_diverse(
            scored,
            limit=genre_row.target_items,
            row_kind=genre_row.kind,
            exclude_ids=used_ids,
        )
        for track in tracks[:12]:
            item_id = track_id(track)
            if item_id:
                used_ids.add(item_id)
        if len(tracks) < genre_row.min_items:
            continue
        tabs.append(
            {
                "id": genre,
                "label": genre.replace("_", " ").title(),
                "tracks": tracks,
                "pages": _pages(tracks, 3),
                "status": "ready",
                "has_more": False,
            }
        )
        if len(tabs) >= 4:
            break
    diagnostics = {
        "accepted": len(tabs) >= 2,
        "tab_count": len(tabs),
        "tab_item_counts": {str(tab["id"]): len(tab.get("tracks") or []) for tab in tabs},
        "native_evidence_counts": {
            genre: sum(
                1
                for candidate in all_candidates
                if _hint_score_for_genre(_native_genre_text(candidate.item), hints) > 0
            )
            for genre, hints in genre_hints.items()
        },
        "rejection_reasons": [] if len(tabs) >= 2 else ["below_min_tabs"],
    }
    return tabs, diagnostics


def _hint_score_for_genre(text: str, hints: Sequence[str]) -> float:
    return float(sum(0.75 for hint in hints if normalize_text(hint) in text))


def _native_genre_text(item: Dict[str, Any]) -> str:
    native = dict(item or {})
    native.pop("discovery_genres", None)
    native.pop("discovery_query", None)
    native.pop("recommendation_reason", None)
    return metadata_text(native)


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
            if len(items) < row.min_items:
                row_status[row_kind] = {
                    "status": "filtered_out",
                    "reason": "below_min_mixes",
                    "count": len(items),
                    "diagnostics": mix_diagnostics,
                }
                continue
            rows.append(_row_payload(row, items, meta={"mix_diagnostics": mix_diagnostics}))
            row_status[row_kind] = {
                "status": "emitted",
                "count": len(items),
                "diagnostics": mix_diagnostics,
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
            if row.kind == "recommended_albums" and not items:
                row_status[row_kind] = {"status": "filtered_out", "reason": "empty"}
                continue
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

        if row.kind == "last_played":
            items = rank_tracks(pools, taste, row, limit=row.max_items)
        elif row.kind == "frequently_listened":
            items = rank_tracks(pools, taste, row, limit=row.max_items)
        elif row.kind == "trending_by_genre":
            tabs, tab_diagnostics = build_trending_genre_tabs(pools, taste)
            if not tab_diagnostics.get("accepted"):
                row_status[row_kind] = {"status": "filtered_out", "reason": "below_min_tabs", "diagnostics": tab_diagnostics}
                continue
            items = [track for tab in tabs for track in list(tab.get("tracks") or [])[: row.page_size]]
            items = _dedupe_track_dicts(items, row.max_items)
            meta = {
                "tabs": tabs,
                "active_tab_id": str((tabs[0] or {}).get("id") or ""),
                "tab_diagnostics": tab_diagnostics,
            }
            rows.append(_row_payload(row, items, meta=meta))
            row_status[row_kind] = {"status": "emitted", "count": len(items), "tab_count": len(tabs)}
            continue
        elif row.kind == "quiet_picks":
            items = rank_tracks(pools, taste, row, limit=row.max_items)
        else:
            items = rank_tracks(pools, taste, row, limit=row.max_items, exclude_ids=selected_recent_ids)

        if len(items) < row.min_items:
            row_status[row_kind] = {"status": "filtered_out", "reason": "below_min_items", "count": len(items)}
            continue
        rows.append(_row_payload(row, items))
        row_status[row_kind] = {"status": "emitted", "count": len(items)}
        if row.kind in {"todays_pick", "made_for_you", "because_you_played", "hidden_gems"}:
            for item in items[:8]:
                item_id = track_id(item)
                if item_id:
                    selected_recent_ids.add(item_id)
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
