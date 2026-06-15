from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Tuple
import re
import time

from ..domain.catalog import (
    catalog_source_authority,
    is_unofficial_catalog_item,
    normalized_album_payload,
    normalized_track_payload,
)
from ..search.upstream_runtime import search_artists_direct
from .config import DISCOVERY_UNIVERSE_TARGET, LANE_RECIPES, PROVIDER_BUDGETS_MS, TRACK_POOL_QUOTAS
from .schema import DiscoveryCandidate, TasteProfile


_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="discovery-provider")
_ORCHESTRATOR_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="discovery-pool")
_STRICT_RECOMMENDATION_SOURCES = {
    "collaborative",
    "genre_mood",
    "popularity",
}


def trim(server: Any, value: Any) -> str:
    try:
        return server._recommendation_trim_text(value)
    except Exception:
        return str(value or "").strip()


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


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
        enriched["genres"] = list(dict.fromkeys([primary_genre, *secondary_genres]))
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
        ("region", "region"),
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
        enriched["genres"] = list(dict.fromkeys([primary_genre, *secondary_genres]))
    for source_key, target_key in (
        ("subgenre", "subgenre"),
        ("scene_cluster_ids", "scene_cluster_ids"),
        ("confidence", "feature_confidence"),
        ("feature_version", "catalog_feature_version"),
        ("scene_graph_version", "scene_graph_version"),
        ("release_year", "release_year"),
        ("era_bucket", "era_bucket"),
        ("language", "language"),
        ("region", "region"),
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


def _bounded_call(
    provider_name: str,
    fn: Callable[[], Any],
    *,
    budget_ms: int | None = None,
    fallback: Any = None,
) -> Tuple[Any, int, bool]:
    started = time.perf_counter()
    future = _EXECUTOR.submit(fn)
    timeout = max(float((budget_ms or PROVIDER_BUDGETS_MS.get(provider_name) or 1000)) / 1000.0, 0.05)
    timed_out = False
    try:
        value = future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        value = fallback
        timed_out = True
    except Exception:
        value = fallback
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return value, elapsed_ms, timed_out


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


def _similarity_pool(server: Any, taste: TasteProfile) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen: set[str] = set()
    deadline = time.perf_counter() + (PROVIDER_BUDGETS_MS["similarity"] / 1000.0)
    anchors = list(taste.anchor_tracks or []) + list(taste.last_played_tracks or []) + list(taste.recent_tracks or [])
    for anchor in anchors[:8]:
        remaining_ms = int(max(deadline - time.perf_counter(), 0.0) * 1000)
        if remaining_ms <= 50:
            break
        anchor_id = track_id(anchor)
        if not anchor_id:
            continue
        results, elapsed, timed_out = _bounded_call(
            "similarity",
            lambda anchor_id=anchor_id: server._assistant_tool_get_similar_tracks(anchor_id, 18),
            budget_ms=remaining_ms,
            fallback=[],
        )
        timings[f"similarity:{anchor_id}"] = elapsed
        if timed_out:
            break
        for index, track in enumerate(results or []):
            _add_candidate(
                server,
                pool,
                seen,
                track,
                source="similarity",
                score=max(3.0 - (index * 0.08), 0.8),
                reason="similar_to_recent",
            )
        if len(pool) >= TRACK_POOL_QUOTAS["similarity"]:
            break
    return pool, timings


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


def _fast_artist_neighborhood(server: Any, taste: TasteProfile) -> Dict[str, Any]:
    artists: List[Dict[str, Any]] = []
    tracks: List[Dict[str, Any]] = []
    seen_artists: set[str] = set()
    for seed_name in _artist_queries(taste)[:4]:
        direct = search_artists_direct(server, seed_name, 1)
        if not direct:
            continue
        seed_id = str(direct[0].get("id") or "").strip()
        if not seed_id:
            continue
        details_builder = getattr(server, "build_artist_details_payload", None) or getattr(
            server,
            "_build_artist_details_payload",
            None,
        )
        if not callable(details_builder):
            continue
        try:
            details = details_builder(seed_id, enrich_related=False)
        except TypeError:
            details = details_builder(seed_id)
        except Exception:
            details = {}
        related = [
            dict(item)
            for item in details.get("related_artists") or []
            if isinstance(item, dict)
        ][:6]
        for related_artist in related:
            name = str(related_artist.get("name") or "").strip()
            key = normalize_text(name)
            if not key or key in seen_artists:
                continue
            seen_artists.add(key)
            related_artist["related_to_artist"] = seed_name
            related_artist["artist_neighborhood"] = True
            artists.append(related_artist)
            try:
                tracks.extend(
                    {
                        **dict(track),
                        "related_to_artist": seed_name,
                        "artist_neighborhood": True,
                    }
                    for track in (
                        server._assistant_tool_search_tracks(f"{name} songs", 12)
                        or []
                    )
                    if isinstance(track, dict)
                )
            except Exception:
                continue
            if len(tracks) >= TRACK_POOL_QUOTAS["artist_graph"]:
                break
        if len(tracks) >= TRACK_POOL_QUOTAS["artist_graph"]:
            break
    return {"artists": artists, "tracks": tracks}


def _artist_graph_pool(server: Any, taste: TasteProfile) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen: set[str] = set()
    payload, elapsed, timed_out = _bounded_call(
        "artist_graph",
        lambda: _fast_artist_neighborhood(server, taste),
        fallback={},
    )
    timings["artist_graph:neighborhood"] = elapsed
    if not timed_out and isinstance(payload, dict):
        for index, raw in enumerate(payload.get("tracks") or []):
            _add_candidate(
                server,
                pool,
                seen,
                raw,
                source="artist_graph",
                score=max(3.8 - (index * 0.025), 1.0),
                reason="related_artist_neighborhood",
                item_metadata={"artist_neighborhood": True},
            )
        for artist in payload.get("artists") or []:
            if not isinstance(artist, dict):
                continue
            signature = normalize_text(artist.get("id") or artist.get("name"))
            if not signature:
                continue
            pool.append(
                DiscoveryCandidate(
                    item=dict(artist),
                    source="artist_graph",
                    score=float(artist.get("score") or 2.0),
                    reasons=["related_artist_neighborhood"],
                    item_type="artist",
                )
            )
    return pool, timings


def _genre_queries(taste: TasteProfile) -> List[str]:
    seed_text = " ".join(
        list(taste.taste_queries or [])
        + list(taste.recent_queries or [])
        + [metadata_text(track) for track in (taste.recent_tracks[:16] + taste.top_tracks[:16])]
    )
    base_genres = [
        "rock",
        "pop",
        "rnb",
        "soul",
        "blues",
        "metal",
        "electronic",
        "jazz",
        "ambient",
        "acoustic",
        "dance",
    ]
    matches = [genre for genre in base_genres if genre in normalize_text(seed_text)]
    if not matches:
        matches = ["rock", "pop", "soul", "electronic"]
    adjacent = {
        "rock": ("alternative rock", "classic rock", "hard rock"),
        "pop": ("dance pop", "indie pop", "synth pop"),
        "rnb": ("soul", "neo soul", "funk"),
        "soul": ("rnb", "blues", "funk"),
        "blues": ("classic blues", "blues rock", "soul blues"),
        "metal": ("hard rock", "heavy metal", "punk rock"),
        "electronic": ("edm", "house", "downtempo"),
        "ambient": ("lo-fi", "instrumental", "study music"),
        "acoustic": ("folk", "singer songwriter", "unplugged"),
        "dance": ("edm", "club", "upbeat pop"),
    }
    queries: List[str] = []
    for genre in matches[:5]:
        queries.append(f"{genre} songs")
        queries.extend(f"{item} songs" for item in adjacent.get(genre, ())[:2])
    return list(dict.fromkeys(queries))[:10]


def _genre_mood_pool(server: Any, taste: TasteProfile) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen_by_pool: Dict[str, set[str]] = {}
    deadline = time.perf_counter() + (PROVIDER_BUDGETS_MS["genre_mood"] / 1000.0)
    lane_queries = [
        (query, f"lane_{lane_id}", lane_id)
        for query_index in range(2)
        for lane_id, recipe in LANE_RECIPES.items()
        if lane_id != "all"
        for query in recipe.retrieval_queries[query_index : query_index + 1]
    ]
    genre_queries = [(query, "genre_mood", "") for query in _genre_queries(taste)[:6]]

    def add_results(
        query: str,
        source: str,
        lane_id: str,
        results: Iterable[Any],
    ) -> None:
        query_genres = _genre_tags_for_query(query)
        seen = seen_by_pool.setdefault(source, set())
        for index, track in enumerate(results or []):
            _add_candidate(
                server,
                pool,
                seen,
                track,
                source=source,
                score=max((2.7 if lane_id else 2.2) - (index * 0.04), 0.65),
                reason=query,
                item_metadata={
                    "discovery_genres": query_genres,
                    "discovery_query": query,
                    **({"discovery_lane": lane_id} if lane_id else {}),
                },
            )

    # Start one retrieval for every named lane together. Sequential lane
    # searches allowed the first slow query to consume the entire provider
    # budget and left later tabs empty.
    primary_lane_jobs = [
        (
            query,
            source,
            lane_id,
            time.perf_counter(),
            _EXECUTOR.submit(server._assistant_tool_search_tracks, query, 20),
        )
        for query, source, lane_id in lane_queries[:4]
    ]
    for query, source, lane_id, started, future in primary_lane_jobs:
        remaining_ms = int(max(deadline - time.perf_counter(), 0.0) * 1000)
        if remaining_ms <= 50:
            future.cancel()
            continue
        try:
            results = future.result(timeout=max(remaining_ms / 1000.0, 0.05))
        except TimeoutError:
            future.cancel()
            results = []
        except Exception:
            results = []
        elapsed = int((time.perf_counter() - started) * 1000)
        timings[f"{source}:{normalize_text(query)[:32]}"] = elapsed
        add_results(query, source, lane_id, results or [])

    for query, source, lane_id in [*genre_queries, *lane_queries[4:]]:
        remaining_ms = int(max(deadline - time.perf_counter(), 0.0) * 1000)
        if remaining_ms <= 50:
            break
        results, elapsed, timed_out = _bounded_call(
            "genre_mood",
            lambda query=query: server._assistant_tool_search_tracks(query, 20),
            budget_ms=remaining_ms,
            fallback=[],
        )
        timings[f"{source}:{normalize_text(query)[:32]}"] = elapsed
        if timed_out:
            break
        add_results(query, source, lane_id, results or [])
        if len(pool) >= 300:
            break
    return pool, timings


def _genre_tags_for_query(query: str) -> List[str]:
    normalized = normalize_text(query).removesuffix(" songs").strip()
    known_genres = (
        "rock",
        "pop",
        "rnb",
        "soul",
        "blues",
        "metal",
        "electronic",
        "jazz",
        "ambient",
        "acoustic",
        "dance",
        "folk",
        "funk",
        "punk",
        "house",
        "edm",
    )
    tags = [genre for genre in known_genres if genre in normalized]
    if normalized and normalized not in tags:
        tags.insert(0, normalized)
    return tags[:4]


def _album_item_from_track(server: Any, track: Dict[str, Any]) -> Dict[str, Any] | None:
    title = album_name(track)
    artist = artist_name(track)
    if not title:
        return None
    album_id = str(track.get("album_id") or track.get("albumId") or "").strip()
    if not album_id:
        album_id = f"{normalize_text(title)}::{normalize_text(artist)}"
    album = {
        "id": album_id,
        "title": title,
        "artist": artist,
        "thumbnail": track.get("thumbnail") or track.get("image") or track.get("artwork") or "",
        "release_date": track.get("release_date") or track.get("year") or track.get("release_year") or "",
        "source_track_id": track_id(track),
        "preview_track": dict(track),
        "genre": track.get("genre") or track.get("genres") or "",
        "mood": track.get("mood") or "",
        "discovery_genres": list(track.get("discovery_genres") or []),
        "discovery_query": track.get("discovery_query") or "",
        "album_source": "track_derived",
    }
    return _enrich_album_features(server, album)


def _release_year(item: Dict[str, Any]) -> int:
    match = re.search(r"\b(19|20)\d{2}\b", str(item.get("release_date") or item.get("year") or ""))
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


def _album_pool(server: Any, taste: TasteProfile, track_pools: Iterable[DiscoveryCandidate]) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen: set[str] = set()
    track_candidates = list(track_pools or [])
    deadline = time.perf_counter() + (PROVIDER_BUDGETS_MS["album"] / 1000.0)

    def load_official_albums() -> List[Dict[str, Any]]:
        profile = dict(taste.source_profile or {})
        albums: List[Dict[str, Any]] = []
        for key in (
            "recommended_album_candidate_cache",
            "snapshot_albums",
            "recommended_albums",
            "albums",
        ):
            albums.extend(
                dict(item)
                for item in profile.get(key) or []
                if isinstance(item, dict)
            )
        return albums

    official_albums, elapsed, _timed_out = _bounded_call(
        "album",
        load_official_albums,
        fallback=[],
    )
    timings["album:artist_catalog"] = elapsed
    for index, raw in enumerate(official_albums or []):
        if not isinstance(raw, dict):
            continue
        album = _enrich_album_features(server, dict(raw))
        source_name = normalize_text(raw.get("source_name"))
        album["album_source"] = (
            "artist_catalog"
            if source_name == "artist_discography"
            else "known_album"
            if source_name in {"top_album_hints", "snapshot_albums"}
            else "album_search"
        )
        if (
            not _recommendation_eligible(album, allow_unknown=False)
            or float(album["discovery_quality_penalty"] or 0.0) >= 2.5
        ):
            continue
        signature = normalize_text(album.get("id") or f"{album.get('title')}|{album.get('artist')}")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        pool.append(
            _set_album_segment(DiscoveryCandidate(
                item=album,
                source="album",
                score=max(4.2 - (index * 0.06), 1.2),
                reasons=["official_artist_catalog"],
                item_type="album",
            ), taste, "known_artist_albums")
        )

    for candidate in track_candidates:
        album = _album_item_from_track(server, candidate.item)
        signature = normalize_text((album or {}).get("id") or (album or {}).get("title"))
        if (
            not album
            or not signature
            or signature in seen
            or not _recommendation_eligible(album, allow_unknown=False)
            or float(album.get("discovery_quality_penalty") or 0.0) >= 2.5
        ):
            continue
        seen.add(signature)
        album["artist_neighborhood"] = candidate.item.get("artist_neighborhood") is True
        pool.append(
            _set_album_segment(DiscoveryCandidate(
                item=album,
                source=candidate.source,
                score=candidate.score - 0.5,
                reasons=["track_derived_album"],
                item_type="album",
            ), taste)
        )
        if len(pool) >= 64:
            break

    related_artists: List[str] = []
    related_seen: set[str] = set()
    for candidate in track_candidates:
        if candidate.source != "artist_graph":
            continue
        artist = artist_name(candidate.item)
        artist_key = normalize_text(artist)
        if not artist_key or artist_key in related_seen:
            continue
        related_seen.add(artist_key)
        related_artists.append(artist)
        if len(related_artists) >= 4:
            break
    album_artists = list(dict.fromkeys([*related_artists, *_artist_queries(taste)]))[:10]
    queries = [(f"{artist} albums", normalize_text(artist)) for artist in album_artists]
    for query, required_artist_key in queries:
        remaining_ms = int(max(deadline - time.perf_counter(), 0.0) * 1000)
        if remaining_ms <= 50:
            break
        results, elapsed, timed_out = _bounded_call(
            "album",
            lambda query=query: server._assistant_tool_search_albums(query, 12),
            budget_ms=remaining_ms,
            fallback=[],
        )
        timings[f"album:{normalize_text(query)[:32]}"] = elapsed
        if timed_out:
            break
        for index, raw in enumerate(results or []):
            if not isinstance(raw, dict):
                continue
            title = trim(server, raw.get("title") or raw.get("name") or raw.get("album"))
            if not title:
                continue
            artist = artist_name(raw)
            if required_artist_key and normalize_text(artist) != required_artist_key:
                continue
            album_id = trim(server, raw.get("id") or raw.get("browseId") or raw.get("album_id"))
            signature = normalize_text(album_id or f"{title}|{artist}")
            if not signature or signature in seen:
                continue
            album = {
                "id": album_id or signature,
                "title": title,
                "artist": artist,
                "thumbnail": raw.get("thumbnail") or raw.get("image") or "",
                "release_date": raw.get("release_date") or raw.get("year") or raw.get("release_year") or "",
                "album_source": "album_search",
            }
            album = _enrich_album_features(server, album)
            if (
                not _recommendation_eligible(album, allow_unknown=False)
                or float(album["discovery_quality_penalty"] or 0.0) >= 2.5
            ):
                continue
            seen.add(signature)
            segment = (
                "adjacent_artist_albums"
                if required_artist_key in related_seen
                else "known_artist_albums"
            )
            pool.append(
                _set_album_segment(DiscoveryCandidate(
                    item=album,
                    source="album",
                    score=max(2.2 - (index * 0.08), 0.6),
                    reasons=["album_search_fallback"],
                    item_type="album",
                ), taste, segment)
            )
        if len(pool) >= 80:
            break
    return pool, timings


def _freshness_pool(albums: Iterable[DiscoveryCandidate]) -> List[DiscoveryCandidate]:
    pool: List[DiscoveryCandidate] = []
    for candidate in albums:
        release = str(candidate.item.get("release_date") or candidate.item.get("year") or "").strip()
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


def _popularity_pool(server: Any) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    seen: set[str] = set()
    results, elapsed, timed_out = _bounded_call(
        "popularity",
        lambda: server._fallback_home_candidates(120),
        fallback=[],
    )
    timings["popularity:home"] = elapsed
    if timed_out:
        return pool, timings
    for index, raw in enumerate(results or []):
        score = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else 1.0
        _add_candidate(
            server,
            pool,
            seen,
            raw,
            source="popularity",
            score=max(float(score or 1.0) - (index * 0.01), 0.35),
            reason="catalog_popularity",
        )
    return pool, timings


def _collaborative_pool(server: Any, taste: TasteProfile) -> Tuple[List[DiscoveryCandidate], Dict[str, int]]:
    pool: List[DiscoveryCandidate] = []
    timings: Dict[str, int] = {}
    ids = list(taste.collaborative_track_ids or [])[:48]
    if not ids:
        return pool, timings
    results, elapsed, timed_out = _bounded_call(
        "collaborative",
        lambda: server._recommendation_fetch_tracks_for_ids(ids, 48),
        fallback=[],
    )
    timings["collaborative:ids"] = elapsed
    if timed_out:
        return pool, timings
    seen: set[str] = set()
    for index, track in enumerate(results or []):
        _add_candidate(
            server,
            pool,
            seen,
            track,
            source="collaborative",
            score=max(1.4 - (index * 0.02), 0.35),
            reason="weak_collaborative_boost",
        )
    return pool, timings


def _enrich_taste_tracks(server: Any, taste: TasteProfile) -> None:
    for field_name in ("recent_tracks", "top_tracks", "last_played_tracks", "anchor_tracks"):
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
    source_items = {
        source: [
            candidate
            for candidate in pools.get(source, []) or []
            if candidate.item_type == "track"
        ][:quota]
        for source, quota in TRACK_POOL_QUOTAS.items()
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


def build_candidate_pools(server: Any, taste: TasteProfile) -> Tuple[Dict[str, List[DiscoveryCandidate]], Dict[str, int], Dict[str, int]]:
    provider_timings: Dict[str, int] = {}
    _enrich_taste_tracks(server, taste)
    history_started = time.perf_counter()
    provider_futures = {
        "similarity": _ORCHESTRATOR_EXECUTOR.submit(_similarity_pool, server, taste),
        "artist_graph": _ORCHESTRATOR_EXECUTOR.submit(_artist_graph_pool, server, taste),
        "genre_mood": _ORCHESTRATOR_EXECUTOR.submit(_genre_mood_pool, server, taste),
        "popularity": _ORCHESTRATOR_EXECUTOR.submit(_popularity_pool, server),
        "collaborative": _ORCHESTRATOR_EXECUTOR.submit(_collaborative_pool, server, taste),
    }
    history = _history_pool(server, taste)
    provider_timings["history"] = int((time.perf_counter() - history_started) * 1000)

    provider_results: Dict[str, List[DiscoveryCandidate]] = {}
    for provider_name, future in provider_futures.items():
        try:
            provider_items, detailed_timings = future.result(
                timeout=max(PROVIDER_BUDGETS_MS[provider_name] / 1000.0 + 0.25, 0.5)
            )
        except Exception:
            provider_items, detailed_timings = [], {}
        provider_results[provider_name] = list(provider_items or [])
        provider_timings.update(detailed_timings or {})
        provider_timings[provider_name] = (
            sum((detailed_timings or {}).values()) if detailed_timings else 0
        )

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
    popularity = provider_results["popularity"]
    collaborative = provider_results["collaborative"]

    all_track_candidates = (
        history
        + similarity
        + artist_graph
        + genre_mood
        + [candidate for items in lane_pools.values() for candidate in items]
        + popularity
        + collaborative
    )
    album, album_timings = _album_pool(server, taste, all_track_candidates)
    provider_timings.update(album_timings)
    provider_timings["album"] = sum(album_timings.values()) if album_timings else 0

    freshness = _freshness_pool(album)
    album_segments = _album_segment_pools(album)
    provider_timings["freshness"] = 0

    pools: Dict[str, List[DiscoveryCandidate]] = {
        "history": history,
        "similarity": similarity,
        "artist_graph": artist_graph,
        "genre_mood": genre_mood,
        "album": album,
        "freshness": freshness,
        "popularity": popularity,
        "collaborative": collaborative,
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
            "album_known": sum(
                1
                for candidate in album
                if candidate.item.get("album_source") == "known_album"
            ),
            "album_track_derived": sum(
                1
                for candidate in album
                if candidate.item.get("album_source") == "track_derived"
            ),
            "album_search_fallback": sum(
                1
                for candidate in album
                if candidate.item.get("album_source") == "album_search"
            ),
            "lane_retrieval_total": sum(len(items) for items in lane_pools.values()),
            "album_segment_total": sum(len(items) for items in album_segments.values()),
        }
    )
    return pools, counts, provider_timings


def source_counts(candidates: Iterable[DiscoveryCandidate]) -> Dict[str, int]:
    return dict(Counter(candidate.source for candidate in candidates or []))
