from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Tuple
import re
import time

from ..domain.catalog import (
    catalog_source_authority,
    is_unofficial_catalog_item,
    normalized_album_payload,
    normalized_track_payload,
)
from .config import DISCOVERY_UNIVERSE_TARGET, LANE_RECIPES, TRACK_POOL_QUOTAS
from .schema import DiscoveryCandidate, TasteProfile

if TYPE_CHECKING:
    from .enrichment import MaterializedCandidateSupply


_STRICT_RECOMMENDATION_SOURCES = {
    "collaborative",
    "genre_mood",
    "popularity",
    "ytmusic_home",
}
_PROFILE_EVIDENCE_SOURCES = {"history", "profile_spine"}

_SOURCE_RECOMMENDATION_PATHS = {
    "history": "direct_history",
    "profile_spine": "profile_history_anchor",
    "similarity": "track_radio",
    "artist_graph": "artist_neighbor",
    "genre_mood": "structured_tag",
    "collaborative": "collaborative_neighbor",
    "popularity": "broad_global",
    "ytmusic_home": "broad_global",
    "discovery_universe": "broad_global",
}

_RECOMMENDATION_PATH_CONFIDENCE = {
    "direct_history": 1.0,
    "profile_history_anchor": 0.95,
    "same_artist_catalog": 0.92,
    "track_radio": 0.84,
    "artist_neighbor": 0.78,
    "collaborative_neighbor": 0.72,
    "structured_tag": 0.58,
    "broad_global": 0.12,
    "unproven": 0.0,
}


def trim(server: Any, value: Any) -> str:
    try:
        return server._recommendation_trim_text(value)
    except Exception:
        return str(value or "").strip()


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _contains_token_phrase(text: str, phrase: Any) -> bool:
    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(
        re.escape(part) for part in normalized_phrase.split()
    ) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def track_id(track: Dict[str, Any] | None) -> str:
    if not isinstance(track, dict):
        return ""
    for key in ("id", "track_id", "videoId", "video_id", "ytid"):
        value = str(track.get(key) or "").strip()
        if value:
            return value
    return ""


def artist_name(item: Dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("artist", "artist_name", "artistName", "channel", "author", "subtitle"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    artists = item.get("artists")
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict):
                value = str(artist.get("name") or artist.get("title") or "").strip()
            else:
                value = str(artist or "").strip()
            if value:
                return value
    return ""


def album_name(item: Dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    album = item.get("album")
    if isinstance(album, dict):
        value = str(album.get("name") or album.get("title") or "").strip()
        if value:
            return value
    for key in ("album", "album_name", "albumName", "collection"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def item_signature(item: Dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    item_id = track_id(item)
    if item_id:
        return f"id:{item_id}"
    title = normalize_text(item.get("title") or item.get("name"))
    artist = normalize_text(artist_name(item))
    return f"text:{title}|{artist}" if title or artist else ""


def metadata_text(item: Dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    pieces: List[str] = []
    for key in (
        "title",
        "name",
        "artist",
        "artist_name",
        "channel",
        "author",
        "album",
        "album_name",
        "genre",
        "genres",
        "mood",
        "mood_axes",
        "track_type_tags",
        "scene_cluster_ids",
        "era_bucket",
        "release_year",
        "description",
        "subtitle",
        "discovery_genres",
        "discovery_query",
    ):
        value = item.get(key)
        if isinstance(value, list):
            pieces.extend(str(entry or "") for entry in value)
        elif isinstance(value, dict):
            pieces.extend(str(entry or "") for entry in value.values())
        else:
            pieces.append(str(value or ""))
    return normalize_text(" ".join(pieces))


def _quality_penalty(item: Dict[str, Any]) -> float:
    text = metadata_text(item)
    penalty = 0.0
    if is_unofficial_catalog_item(item):
        penalty += 2.8
    if "various artists" in text:
        penalty += 1.5
    if any(token in text for token in ("lyrics video", "hour mix", "hours mix", "compilation")):
        penalty += 0.7
    return penalty


def _source_authority(item: Dict[str, Any]) -> str:
    return catalog_source_authority(item)


def _mark_profile_evidence(
    item: Dict[str, Any],
    *,
    source: str,
    reason: str = "",
) -> Dict[str, Any]:
    """Preserve user-history provenance without pretending metadata is richer.

    A user-played/profile-spine track with weak language/source metadata should
    still be trusted as personal evidence. Broad discovery candidates must keep
    proving themselves through canonical metadata/source authority.
    """

    if source not in _PROFILE_EVIDENCE_SOURCES and item.get("profile_spine") is not True:
        return item
    if not (track_id(item) or (str(item.get("title") or "").strip() and artist_name(item))):
        return item
    item["profile_evidence"] = True
    item["profile_evidence_source"] = source
    if reason:
        item["profile_evidence_reason"] = reason
    authority = normalize_text(item.get("source_authority"))
    if authority in {"", "unknown"}:
        item["source_authority"] = "profile_verified"
        item["source_authority_source"] = "profile_evidence"
    return item


def _recommendation_path_for(
    source: str,
    reason: str,
    item_metadata: Dict[str, Any] | None,
    item: Dict[str, Any],
) -> str:
    metadata = item_metadata or {}
    explicit = normalize_text(
        metadata.get("recommendation_path") or item.get("recommendation_path")
    )
    if explicit:
        return explicit
    source_key = normalize_text(source)
    reason_key = normalize_text(reason)
    if source_key.startswith("lane_"):
        return "structured_tag"
    if source_key == "profile_spine" and metadata.get("profile_seed_artist"):
        return "same_artist_catalog"
    if source_key == "artist_graph" or item.get("artist_neighborhood") is True:
        return "artist_neighbor"
    if source_key == "similarity":
        return "track_radio"
    if source_key == "collaborative":
        return "collaborative_neighbor"
    if reason_key in {"repair_radio_neighbors", "repair_made_for_you_neighbors"}:
        return "artist_neighbor"
    if reason_key in {"repair_radio_known_works", "repair_quiet_artist_depth"}:
        return "track_radio"
    return _SOURCE_RECOMMENDATION_PATHS.get(source_key, "unproven")


def _set_recommendation_path(
    item: Dict[str, Any],
    *,
    source: str,
    reason: str,
    item_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    path = _recommendation_path_for(source, reason, item_metadata, item)
    item["recommendation_path"] = path
    item["recommendation_path_source"] = normalize_text(source)
    item["recommendation_path_reason"] = normalize_text(reason)
    item["recommendation_path_confidence"] = _RECOMMENDATION_PATH_CONFIDENCE.get(
        path,
        0.0,
    )
    return item


def _recommendation_eligible(item: Dict[str, Any], *, allow_unknown: bool = True) -> bool:
    authority = str(item.get("source_authority") or _source_authority(item))
    if authority == "search_only":
        return False
    return allow_unknown or authority != "unknown"


def _merge_source_metadata(output: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(output)
    for key, value in raw.items():
        if key == "track" or value in (None, "", [], {}):
            continue
        if key not in merged or merged.get(key) in (None, "", 0, [], {}):
            merged[key] = value
    return merged


def _enrich_track_features(server: Any, track: Dict[str, Any]) -> Dict[str, Any]:
    enriched = normalized_track_payload(track)
    try:
        from ..recommend.feature_store import derive_track_feature

        feature = derive_track_feature(server, enriched)
    except Exception:
        feature = {}
    primary_genre = str(feature.get("primary_genre") or "").strip()
    secondary_genres = [
        str(value or "").strip()
        for value in feature.get("secondary_genres") or []
        if str(value or "").strip()
    ]
    if primary_genre:
        enriched.setdefault("genre", primary_genre)
    if primary_genre or secondary_genres:
        enriched["genres"] = list(
            dict.fromkeys([
                *(enriched.get("genres") or []),
                primary_genre,
                *secondary_genres,
            ])
        )
        enriched["discovery_genres"] = list(
            dict.fromkeys([
                *(enriched.get("discovery_genres") or []),
                primary_genre,
                *secondary_genres,
            ])
        )
    for source_key, target_key in (
        ("subgenre", "subgenre"),
        ("scene_cluster_ids", "scene_cluster_ids"),
        ("peer_artist_ids", "peer_artist_ids"),
        ("track_type_tags", "track_type_tags"),
        ("mood_axes", "mood_axes"),
        ("confidence", "feature_confidence"),
        ("feature_version", "catalog_feature_version"),
        ("scene_graph_version", "scene_graph_version"),
        ("release_year", "release_year"),
        ("era_bucket", "era_bucket"),
        ("language", "language"),
        ("language_confidence", "language_confidence"),
        ("language_source", "language_source"),
        ("region", "region"),
        ("region_confidence", "region_confidence"),
        ("region_source", "region_source"),
        ("popularity", "popularity"),
        ("freshness", "freshness"),
        ("mood_axes", "audio_traits"),
    ):
        value = feature.get(source_key)
        if value not in (None, "", [], {}):
            enriched[target_key] = value
    enriched["feature_source"] = str(feature.get("source_kind") or "derived_metadata")
    enriched["discovery_quality_penalty"] = _quality_penalty(enriched)
    enriched["source_authority"] = _source_authority(enriched)
    return enriched


def _enrich_album_features(server: Any, album: Dict[str, Any]) -> Dict[str, Any]:
    enriched = normalized_album_payload(album)
    try:
        from ..recommend.feature_store import derive_album_feature

        feature = derive_album_feature(server, enriched)
    except Exception:
        feature = {}
    primary_genre = str(feature.get("primary_genre") or "").strip()
    secondary_genres = [
        str(value or "").strip()
        for value in feature.get("secondary_genres") or []
        if str(value or "").strip()
    ]
    if primary_genre:
        enriched.setdefault("genre", primary_genre)
    if primary_genre or secondary_genres:
        enriched["genres"] = list(
            dict.fromkeys([
                *(enriched.get("genres") or []),
                primary_genre,
                *secondary_genres,
            ])
        )
        enriched["discovery_genres"] = list(
            dict.fromkeys([
                *(enriched.get("discovery_genres") or []),
                primary_genre,
                *secondary_genres,
            ])
        )
    for source_key, target_key in (
        ("subgenre", "subgenre"),
        ("scene_cluster_ids", "scene_cluster_ids"),
        ("confidence", "feature_confidence"),
        ("feature_version", "catalog_feature_version"),
        ("scene_graph_version", "scene_graph_version"),
        ("release_year", "release_year"),
        ("era_bucket", "era_bucket"),
        ("language", "language"),
        ("language_confidence", "language_confidence"),
        ("language_source", "language_source"),
        ("region", "region"),
        ("region_confidence", "region_confidence"),
        ("region_source", "region_source"),
        ("popularity", "popularity"),
        ("freshness", "freshness"),
    ):
        value = feature.get(source_key)
        if value not in (None, "", [], {}):
            enriched[target_key] = value
    enriched["feature_source"] = str(feature.get("source_kind") or "derived_metadata")
    enriched["discovery_quality_penalty"] = _quality_penalty(enriched)
    enriched["source_authority"] = _source_authority(enriched)
    if (
        enriched["source_authority"] == "unknown"
        and str(enriched.get("id") or "").strip()
        and str(enriched.get("title") or "").strip()
        and artist_name(enriched)
    ):
        # Album providers and track-derived albums are already entity-scoped.
        enriched["source_authority"] = "verified_catalog"
    return enriched


def normalize_track(server: Any, raw: Any) -> Dict[str, Any] | None:
    if isinstance(raw, tuple) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    source_metadata = dict(raw)
    if isinstance(raw.get("track"), dict):
        raw = dict(raw["track"])
    try:
        normalized = server.normalize_recommendation_track(raw)
    except Exception:
        normalized = None
    if isinstance(normalized, dict):
        output = _merge_source_metadata(dict(normalized), raw)
        output = _merge_source_metadata(output, source_metadata)
        output.setdefault("artist", artist_name(raw) or artist_name(output))
        return _enrich_track_features(server, output)
    title = trim(server, raw.get("title") or raw.get("name"))
    if not title:
        return None
    output = dict(raw)
    output["title"] = title
    output.setdefault("artist", artist_name(raw))
    return _enrich_track_features(server, output)




def _add_candidate(
    server: Any,
    pool: List[DiscoveryCandidate],
    seen: set[str],
    raw: Any,
    *,
    source: str,
    score: float,
    reason: str,
    item_metadata: Dict[str, Any] | None = None,
) -> None:
    track = normalize_track(server, raw)
    if track and item_metadata:
        track.update(item_metadata)
    if track:
        track = _mark_profile_evidence(track, source=source, reason=reason)
        track = _set_recommendation_path(
            track,
            source=source,
            reason=reason,
            item_metadata=item_metadata,
        )
    requires_authority = source in _STRICT_RECOMMENDATION_SOURCES or source.startswith(
        "lane_"
    )
    if (
        track
        and source != "history"
        and (
            not _recommendation_eligible(track, allow_unknown=not requires_authority)
            or float(track.get("discovery_quality_penalty") or 0.0) >= 2.5
        )
    ):
        return
    signature = item_signature(track)
    if not track or not signature or signature in seen:
        return
    seen.add(signature)
    pool.append(
        DiscoveryCandidate(
            item=track,
            source=source,
            score=float(score),
            reasons=[reason] if reason else [],
        )
    )


def _history_pool(server: Any, taste: TasteProfile) -> List[DiscoveryCandidate]:
    pool: List[DiscoveryCandidate] = []
    seen: set[str] = set()
    weighted_sources = (
        (taste.last_played_tracks, 3.5, "last_played"),
        (taste.frequent_tracks, 3.4, "frequent"),
        (taste.recent_tracks, 2.8, "recent"),
        (taste.top_tracks, 3.2, "frequent"),
        (taste.anchor_tracks, 2.6, "anchor"),
    )
    for tracks, base_score, reason in weighted_sources:
        for index, track in enumerate(tracks or []):
            _add_candidate(
                server,
                pool,
                seen,
                track,
                source="history",
                score=max(base_score - (index * 0.035), 0.5),
                reason=reason,
            )
    return pool


def _profile_spine_pool(server: Any, taste: TasteProfile) -> List[DiscoveryCandidate]:
    """Stable long-term candidate backbone.

    Cold launch should not be controlled by only the latest seed track. This
    pool is intentionally user-driven: it starts from persisted history/top
    tracks and expands only through local catalog memories for known artists.
    It does not run live broad searches, so it stays fast enough for launch.
    """

    pool: List[DiscoveryCandidate] = []
    seen: set[str] = set()
    weighted_sources = (
        (taste.top_tracks, 4.0, "long_term_top_track"),
        (taste.last_played_tracks, 3.4, "long_term_last_played"),
        (taste.anchor_tracks, 3.1, "long_term_anchor"),
        (taste.recent_tracks, 2.5, "short_term_recent"),
    )
    for tracks, base_score, reason in weighted_sources:
        for index, track in enumerate(tracks or []):
            _add_candidate(
                server,
                pool,
                seen,
                track,
                source="profile_spine",
                score=max(base_score - (index * 0.025), 0.8),
                reason=reason,
                item_metadata={"profile_spine": True},
            )
            if len(pool) >= TRACK_POOL_QUOTAS["profile_spine"] // 2:
                break

    try:
        from ..search.catalog_pipeline import catalog_playable_tracks_for_artist
    except Exception:
        catalog_playable_tracks_for_artist = None
        catalog_playable_tracks_for_artist = None

    if catalog_playable_tracks_for_artist is not None:
        for artist in _taste_artist_seed_names(taste, limit=14):
            if len(pool) >= TRACK_POOL_QUOTAS["profile_spine"]:
                break
            try:
                results = catalog_playable_tracks_for_artist(
                    server,
                    user_scope_id=taste.user_scope_id,
                    artist=artist,
                    limit=40,
                )
            except Exception:
                results = []
            for index, track in enumerate(results or []):
                _add_candidate(
                    server,
                    pool,
                    seen,
                    track,
                    source="profile_spine",
                    score=max(3.2 - (index * 0.04), 0.8),
                    reason="long_term_artist_catalog",
                    item_metadata={
                        "profile_spine": True,
                        "profile_seed_artist": artist,
                    },
                )
                if len(pool) >= TRACK_POOL_QUOTAS["profile_spine"]:
                    break
    return pool




def _artist_queries(taste: TasteProfile) -> List[str]:
    artists = (
        list(taste.artist_hints or [])
        + list(taste.top_artists or [])
        + list(taste.listened_artists or [])
        + [artist_name(track) for track in taste.recent_tracks[:12]]
        + [artist_name(track) for track in taste.top_tracks[:12]]
    )
    output: List[str] = []
    seen = set()
    for artist in artists:
        text = str(artist or "").strip()
        key = normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= 8:
            break
    return output






def _taste_artist_seed_names(taste: TasteProfile, *, limit: int = 8) -> List[str]:
    artists = (
        list(taste.artist_hints or [])
        + list(taste.top_artists or [])
        + list(taste.listened_artists or [])
        + [artist_name(track) for track in taste.last_played_tracks[:12]]
        + [artist_name(track) for track in taste.recent_tracks[:12]]
        + [artist_name(track) for track in taste.top_tracks[:12]]
    )
    output: List[str] = []
    seen: set[str] = set()
    for artist in artists:
        value = str(artist or "").strip()
        key = normalize_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def _release_year(item: Dict[str, Any]) -> int:
    match = re.search(
        r"\b(19|20)\d{2}\b",
        str(
            item.get("release_year")
            or item.get("release_date")
            or item.get("year")
            or ""
        ),
    )
    return int(match.group(0)) if match else 0


def _album_segment_for_candidate(candidate: DiscoveryCandidate, taste: TasteProfile) -> str:
    item = candidate.item
    year = _release_year(item)
    current_year = datetime.now(timezone.utc).year
    if year and year >= current_year - 3:
        return "fresh_or_recent_albums"
    artist_key = normalize_text(artist_name(item))
    known_artists = {normalize_text(value) for value in _artist_queries(taste)}
    if candidate.source == "artist_graph" or item.get("artist_neighborhood") is True:
        return "adjacent_artist_albums"
    if candidate.source.startswith("lane_") or candidate.source == "genre_mood":
        return "genre_album_discovery"
    if year and year <= 2005:
        return "classic_neighbor_albums"
    if artist_key and artist_key in known_artists:
        return "known_artist_albums"
    if candidate.source == "similarity":
        return "adjacent_artist_albums"
    return "genre_album_discovery"


def _set_album_segment(
    candidate: DiscoveryCandidate,
    taste: TasteProfile,
    segment: str | None = None,
) -> DiscoveryCandidate:
    candidate.item["album_segment"] = segment or _album_segment_for_candidate(candidate, taste)
    return candidate


def _album_pool(
    server: Any,
    taste: TasteProfile,
    *,
    materialized_albums: Iterable[Dict[str, Any]],
) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen: set[str] = set()
    known_artist_keys = {normalize_text(value) for value in _artist_queries(taste)}
    supplied_albums = [
        dict(item)
        for item in materialized_albums or []
        if isinstance(item, dict)
    ]
    started = time.perf_counter()
    for index, raw in enumerate(supplied_albums):
        if not isinstance(raw, dict):
            continue
        if (
            not trim(server, raw.get("musicbrainz_release_group_id"))
            or raw.get("playable") is not True
            or int(raw.get("track_count") or 0) < 2
        ):
            continue
        album_id = trim(
            server,
            raw.get("id")
            or raw.get("browseId")
            or raw.get("album_id")
            or raw.get("musicbrainz_release_group_id"),
        )
        title = trim(server, raw.get("title") or raw.get("name") or raw.get("album"))
        artist = artist_name(raw)
        if not album_id or not title or not artist:
            continue
        album = _enrich_album_features(
            server,
            {
                **dict(raw),
                "id": album_id,
                "title": title,
                "artist": artist,
                "album_source": "artist_catalog",
            },
        )
        album["album_source"] = "artist_catalog"
        if (
            not _recommendation_eligible(album, allow_unknown=False)
            or float(album["discovery_quality_penalty"] or 0.0) >= 2.5
        ):
            continue
        signature = normalize_text(album.get("id") or f"{album.get('title')}|{album.get('artist')}")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        segment = (
            "known_artist_albums"
            if normalize_text(artist) in known_artist_keys
            else "adjacent_artist_albums"
        )
        pool.append(
            _set_album_segment(DiscoveryCandidate(
                item=album,
                source="album",
                score=max(4.2 - (index * 0.06), 1.2),
                reasons=["materialized_artist_catalog"],
                item_type="album",
            ), taste, segment)
        )
    timings["album:canonical_catalog"] = int((time.perf_counter() - started) * 1000)
    return pool, timings


def _freshness_pool(albums: Iterable[DiscoveryCandidate]) -> List[DiscoveryCandidate]:
    pool: List[DiscoveryCandidate] = []
    for candidate in albums:
        release = str(
            candidate.item.get("release_year")
            or candidate.item.get("release_date")
            or candidate.item.get("year")
            or ""
        ).strip()
        if release:
            fresh = DiscoveryCandidate(
                item=dict(candidate.item),
                source="freshness",
                score=candidate.score + 0.45,
                reasons=list(candidate.reasons or []) + ["release_metadata"],
                item_type="album",
            )
            pool.append(fresh)
    return pool


def _album_segment_pools(albums: Iterable[DiscoveryCandidate]) -> Dict[str, List[DiscoveryCandidate]]:
    segments = {
        "known_artist_albums": [],
        "adjacent_artist_albums": [],
        "genre_album_discovery": [],
        "fresh_or_recent_albums": [],
        "classic_neighbor_albums": [],
    }
    current_year = datetime.now(timezone.utc).year
    for candidate in albums or []:
        segment = str(candidate.item.get("album_segment") or "")
        if segment in segments:
            segments[segment].append(candidate)
        year = _release_year(candidate.item)
        if year and year >= current_year - 3 and segment != "fresh_or_recent_albums":
            recent_item = dict(candidate.item)
            recent_item["album_segment"] = "fresh_or_recent_albums"
            segments["fresh_or_recent_albums"].append(
                DiscoveryCandidate(
                    item=recent_item,
                    source=candidate.source,
                    score=candidate.score + 0.45,
                    reasons=list(candidate.reasons or []) + ["recent_release"],
                    item_type="album",
                )
            )
    return segments




def _enrich_taste_tracks(server: Any, taste: TasteProfile) -> None:
    for field_name in (
        "recent_tracks",
        "top_tracks",
        "last_played_tracks",
        "anchor_tracks",
        "full_history_tracks",
        "frequent_tracks",
    ):
        tracks = getattr(taste, field_name, [])
        setattr(
            taste,
            field_name,
            [
                _enrich_track_features(server, dict(track))
                for track in tracks or []
                if isinstance(track, dict)
            ],
        )


def _balanced_track_universe(
    pools: Dict[str, List[DiscoveryCandidate]],
) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    universe: List[DiscoveryCandidate] = []
    seen: set[str] = set()
    selected_by_source: Dict[str, int] = {}
    # History and the profile spine are familiar supply. Counting them as the
    # discovery universe hid the exact shortage this inventory is meant to expose.
    discovery_sources = {
        "similarity",
        "artist_graph",
        "genre_mood",
        "collaborative",
        "popularity",
        "ytmusic_home",
        *[f"lane_{lane_id}" for lane_id in LANE_RECIPES if lane_id != "all"],
    }
    source_items = {
        source: [
            candidate
            for candidate in pools.get(source, []) or []
            if candidate.item_type == "track"
        ][:quota]
        for source, quota in TRACK_POOL_QUOTAS.items()
        if source in discovery_sources
    }
    max_depth = max((len(items) for items in source_items.values()), default=0)
    for index in range(max_depth):
        for source, items in source_items.items():
            if index >= len(items):
                continue
            candidate = items[index]
            signature = item_signature(candidate.item)
            if not signature or signature in seen:
                continue
            seen.add(signature)
            item = dict(candidate.item)
            item.setdefault("discovery_origin", source)
            item.setdefault(
                "recommendation_path",
                _SOURCE_RECOMMENDATION_PATHS.get(source, "unproven"),
            )
            item.setdefault("recommendation_path_source", source)
            item.setdefault(
                "recommendation_path_confidence",
                _RECOMMENDATION_PATH_CONFIDENCE.get(
                    normalize_text(item.get("recommendation_path")),
                    0.0,
                ),
            )
            universe.append(
                DiscoveryCandidate(
                    item=item,
                    source="discovery_universe",
                    score=float(candidate.score),
                    reasons=list(candidate.reasons or []),
                    item_type="track",
                )
            )
            selected_by_source[source] = selected_by_source.get(source, 0) + 1
            if len(universe) >= DISCOVERY_UNIVERSE_TARGET:
                break
        if len(universe) >= DISCOVERY_UNIVERSE_TARGET:
            break
    return universe, selected_by_source


def _materialized_track_pool(
    server: Any,
    raw_items: Iterable[Dict[str, Any]],
    *,
    source: str,
) -> List[DiscoveryCandidate]:
    pool: List[DiscoveryCandidate] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items or []):
        if not isinstance(raw, dict):
            continue
        relation = str(
            raw.get("materialized_relation")
            or raw.get("recommendation_path")
            or _SOURCE_RECOMMENDATION_PATHS.get(source, "unproven")
        )
        _add_candidate(
            server,
            pool,
            seen,
            raw,
            source=source,
            score=max(6.0 - (index * 0.015), 1.0),
            reason=relation,
            item_metadata={
                "materialized_candidate": True,
                "materialized_relation": relation,
            },
        )
    return pool


def build_candidate_pools(
    server: Any,
    taste: TasteProfile,
    *,
    materialized_supply: "MaterializedCandidateSupply",
) -> Tuple[Dict[str, List[DiscoveryCandidate]], Dict[str, int], Dict[str, int]]:
    provider_timings: Dict[str, int] = {}
    _enrich_taste_tracks(server, taste)
    history_started = time.perf_counter()
    history = _history_pool(server, taste)
    provider_timings["history"] = int((time.perf_counter() - history_started) * 1000)
    profile_spine_started = time.perf_counter()
    profile_spine = _profile_spine_pool(server, taste)
    provider_timings["profile_spine"] = int((time.perf_counter() - profile_spine_started) * 1000)

    raw_pools = dict(materialized_supply.pools or {})
    supplied_profile = _materialized_track_pool(
        server,
        raw_pools.get("profile_spine", []),
        source="profile_spine",
    )
    existing = {item_signature(candidate.item) for candidate in profile_spine}
    profile_spine.extend(
        candidate
        for candidate in supplied_profile
        if item_signature(candidate.item) not in existing
    )
    provider_results = {
        source: _materialized_track_pool(
            server,
            raw_pools.get(source, []),
            source=source,
        )
        for source in (
            "similarity",
            "artist_graph",
            "genre_mood",
            "collaborative",
            "popularity",
            "ytmusic_home",
            "radio_artist_catalog",
        )
    }
    try:
        from ..search.catalog_pipeline import catalog_playable_backbone_tracks

        shared_backbone = _materialized_track_pool(
            server,
            catalog_playable_backbone_tracks(server, limit=160),
            source="popularity",
        )
    except Exception:
        shared_backbone = []
    if shared_backbone:
        known_popularity = {
            item_signature(candidate.item)
            for candidate in provider_results.get("popularity", [])
        }
        provider_results["popularity"].extend(
            candidate
            for candidate in shared_backbone
            if item_signature(candidate.item) not in known_popularity
        )
    materialized_genre = list(provider_results["genre_mood"])
    for lane_id in LANE_RECIPES:
        if lane_id == "all":
            continue
        source = f"lane_{lane_id}"
        materialized_genre.extend(
            _materialized_track_pool(
                server,
                raw_pools.get(source, []),
                source=source,
            )
        )
    provider_results["genre_mood"] = materialized_genre
    provider_timings["materialized_supply"] = 0

    similarity = provider_results["similarity"]
    artist_graph = provider_results["artist_graph"]
    genre_mood_results = provider_results["genre_mood"]
    genre_mood = [candidate for candidate in genre_mood_results if candidate.source == "genre_mood"]
    lane_pools = {
        f"lane_{lane_id}": [
            candidate
            for candidate in genre_mood_results
            if candidate.source == f"lane_{lane_id}"
        ]
        for lane_id in LANE_RECIPES
        if lane_id != "all"
    }
    popularity = provider_results.get("popularity", [])
    ytmusic_home = provider_results.get("ytmusic_home", [])
    collaborative = provider_results["collaborative"]
    radio_artist_catalog = provider_results["radio_artist_catalog"]

    all_track_candidates = (
        history
        + profile_spine
        + similarity
        + artist_graph
        + genre_mood
        + [candidate for items in lane_pools.values() for candidate in items]
        + ytmusic_home
        + popularity
        + collaborative
    )
    album, album_timings = _album_pool(
        server,
        taste,
        materialized_albums=raw_pools.get("album", []),
    )
    provider_timings.update(album_timings)
    provider_timings["album"] = sum(album_timings.values()) if album_timings else 0

    freshness = _freshness_pool(album)
    album_segments = _album_segment_pools(album)
    provider_timings["freshness"] = 0

    pools: Dict[str, List[DiscoveryCandidate]] = {
        "history": history,
        "profile_spine": profile_spine,
        "similarity": similarity,
        "artist_graph": artist_graph,
        "genre_mood": genre_mood,
        "album": album,
        "freshness": freshness,
        "ytmusic_home": ytmusic_home,
        "popularity": popularity,
        "collaborative": collaborative,
        "radio_artist_catalog": radio_artist_catalog,
        **lane_pools,
        **album_segments,
    }
    discovery_universe, universe_source_counts = _balanced_track_universe(pools)
    pools["discovery_universe"] = discovery_universe
    counts = {name: len(items or []) for name, items in pools.items()}
    authority_counts: Dict[str, int] = {}
    for candidate in discovery_universe:
        authority = str(candidate.item.get("source_authority") or "unknown")
        authority_counts[authority] = authority_counts.get(authority, 0) + 1
    counts.update(
        {
            "breadth_target_tracks": DISCOVERY_UNIVERSE_TARGET,
            "breadth_unique_tracks": len(discovery_universe),
            **{
                f"breadth_source_{source}": count
                for source, count in universe_source_counts.items()
            },
            **{
                f"authority_{authority}": count
                for authority, count in authority_counts.items()
            },
            "artist_graph_tracks": sum(
                1 for candidate in artist_graph if candidate.item_type == "track"
            ),
            "artist_graph_artists": sum(
                1 for candidate in artist_graph if candidate.item_type == "artist"
            ),
            "album_artist_catalog": sum(
                1
                for candidate in album
                if candidate.item.get("album_source") == "artist_catalog"
            ),
            "lane_retrieval_total": sum(len(items) for items in lane_pools.values()),
            "album_segment_total": sum(len(items) for items in album_segments.values()),
        }
    )
    return pools, counts, provider_timings


def source_counts(candidates: Iterable[DiscoveryCandidate]) -> Dict[str, int]:
    return dict(Counter(candidate.source for candidate in candidates or []))
