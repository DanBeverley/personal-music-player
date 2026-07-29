from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, wait
from copy import deepcopy
import os
import re
from threading import Lock
import time
import unicodedata
from typing import Any, Dict, Iterable, List

from .catalog import (
    catalog_thumbnail_url,
    canonical_album_identity,
    canonical_title_artist_identity,
    normalize_artist_name,
    normalized_popularity,
    parse_compact_number,
)
from .server_adapter import adapt_domain_server
from ..search.query_mode import resolve_search_mode
from ..search.intelligence import (
    load_fuzzy_catalog_entity_memories,
    remove_untrusted_catalog_query_aliases,
    search_text_similarity,
)
from ..search.catalog_pipeline import (
    catalog_albums_for_artist,
    catalog_playable_tracks_for_artist,
)
from ..search.musicbrainz import search_musicbrainz_recording_items
from ..search.runtime import (
    search_albums_direct,
    search_artists_direct_cached,
    search_tracks_direct,
    semantic_search_anchor_artist_names,
    semantic_search_anchor_tracks,
)

def trim_text(value: str | None) -> str:
    return adapt_domain_server().trim_text(value)


def _raw_artist_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", trim_text(value)).casefold()
    text = re.sub(r"\s*-\s*topic$", "", text)
    text = re.sub(r"\s*vevo$", "", text)
    return " ".join(text.split())


_SEARCH_RETRIEVAL_CACHE_TTL_SECONDS = max(
    5,
    int(os.environ.get("AURALIS_SEARCH_RETRIEVAL_CACHE_TTL_SECONDS", "120")),
)
_SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS = max(
    0.35,
    float(os.environ.get("AURALIS_SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS", "5.5")),
)
_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS = max(
    1.5,
    float(os.environ.get("AURALIS_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS", "6.5")),
)
_SEARCH_DISABLE_TIMEOUTS = (
    os.environ.get("AURALIS_SEARCH_DISABLE_TIMEOUTS", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)
_search_retrieval_cache_lock = Lock()
_search_retrieval_cache: Dict[str, Dict[str, Any]] = {}


def _resolve_retrieval_search_mode(legacy_req, *, server: Any) -> str:
    explicit_mode = trim_text(getattr(legacy_req, "search_mode", "") or "").lower()
    if explicit_mode in {"exact", "entity", "taste"}:
        return explicit_mode
    query = trim_text(getattr(legacy_req, "query", "") or "")
    if not query:
        return "exact"
    return resolve_search_mode(
        query,
        normalize_text_fn=server.normalize_text,
        explicit_mode=explicit_mode,
    )


def _upsert_entity_candidate(
    combined: Dict[str, Dict[str, Any]],
    *,
    entity_id: str,
    payload: Dict[str, Any],
    source_name: str,
    source_score: float,
) -> None:
    if not entity_id:
        return
    current = combined.get(entity_id)
    if current is None:
        combined[entity_id] = {
            "payload": dict(payload),
            "source_scores": {source_name: float(source_score)},
        }
        return
    current_best_score = max(current["source_scores"].values(), default=0.0)
    current["source_scores"][source_name] = max(
        float(current["source_scores"].get(source_name) or 0.0),
        float(source_score),
    )
    if float(source_score) > current_best_score + 0.1:
        merged_payload = dict(payload)
        for key, value in current["payload"].items():
            if merged_payload.get(key) in (None, "", 0) and value not in (None, ""):
                merged_payload[key] = value
        current["payload"] = merged_payload
        return
    for key, value in payload.items():
        if current["payload"].get(key) in (None, "", 0) and value not in (None, ""):
            current["payload"][key] = value


def _track_candidate_entity_id(server, track: Dict[str, Any]) -> str:
    canonical_identity = canonical_title_artist_identity(track)
    if "|" in canonical_identity:
        return f"canonical:{canonical_identity}"
    track_id = trim_text(track.get("id"))
    if track_id:
        return f"id:{track_id}"
    signature = server.recommendation_track_signature(track)
    return f"signature:{signature}" if signature else ""


def _album_candidate_entity_id(album: Dict[str, Any]) -> str:
    title = trim_text(album.get("title") or album.get("album"))
    artist = trim_text(album.get("artist") or album.get("channel") or album.get("artist_name"))
    if title and artist:
        return f"canonical:{canonical_album_identity({'title': title, 'artist': artist})}"
    album_id = trim_text(album.get("id") or album.get("album_id") or album.get("albumId"))
    if album_id:
        return f"id:{album_id}"
    canonical_identity = canonical_album_identity(album)
    return f"canonical:{canonical_identity}" if canonical_identity else ""


def _collect_track_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    server,
    tracks: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, raw_track in enumerate(tracks or []):
        normalized = server.normalize_track(raw_track)
        if normalized is None:
            continue
        track_id = trim_text(normalized.get("id") or normalized.get("videoId"))
        provider = trim_text(
            normalized.get("source_provider") or normalized.get("provider")
        ).lower()
        playback = (
            normalized.get("playback")
            if isinstance(normalized.get("playback"), dict)
            else {}
        )
        playback_source_id = trim_text(
            playback.get("source_id") or playback.get("video_id")
        )
        if (
            normalized.get("playable") is False
            or (
                (provider == "musicbrainz" or track_id.startswith("musicbrainz:"))
                and not playback_source_id
                and not normalized.get("videoId")
            )
        ):
            continue
        entity_id = _track_candidate_entity_id(server, normalized)
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=normalized,
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def _collect_artist_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    artists: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, artist in enumerate(artists or []):
        entity_id = trim_text((artist or {}).get("id"))
        if not entity_id:
            continue
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=dict(artist),
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def _collect_album_candidates(
    combined: Dict[str, Dict[str, Any]],
    *,
    albums: Iterable[Dict[str, Any]],
    source_name: str,
    base_score: float,
) -> None:
    for index, album in enumerate(albums or []):
        entity_id = _album_candidate_entity_id(dict(album or {}))
        _upsert_entity_candidate(
            combined,
            entity_id=entity_id,
            payload=dict(album),
            source_name=source_name,
            source_score=max(base_score - (index * 0.08), 0.1),
        )


def classify_query_intent(
    *,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
    server=None,
) -> str:
    server = adapt_domain_server(server)
    normalized_query = trim_text(query).lower()
    if not normalized_query:
        return "mixed"
    if "lyrics" in normalized_query:
        return "lyric"
    if any(
        token in normalized_query
        for token in ["mood", "chill", "focus", "sleep", "ambient", "playlist"]
    ):
        return "mood"
    if normalized_query.startswith("songs like ") or normalized_query.startswith("similar to "):
        return "mixed"

    def best_track_score() -> float:
        scores = []
        for item in (tracks or [])[:10]:
            title = trim_text(item.get("title") or item.get("name"))
            artist = trim_text(item.get("channel") or item.get("artist"))
            scores.append(
                max(
                    search_text_similarity(query, title),
                    search_text_similarity(query, f"{title} {artist}") * 0.92,
                )
            )
        return max(scores, default=0.0)

    def best_artist_score() -> float:
        return max(
            (
                search_text_similarity(
                    query,
                    trim_text(item.get("name") or item.get("artist")),
                )
                for item in (artists or [])[:10]
            ),
            default=0.0,
        )

    def best_album_score() -> float:
        scores = []
        for item in (albums or [])[:10]:
            title = trim_text(item.get("title") or item.get("name"))
            artist = trim_text(item.get("artist") or item.get("channel"))
            scores.append(
                max(
                    search_text_similarity(query, title),
                    search_text_similarity(query, f"{title} {artist}") * 0.92,
                )
            )
        return max(scores, default=0.0)

    top_track_score = best_track_score()
    top_artist_score = best_artist_score()
    top_album_score = best_album_score()

    best_artist_name = ""
    best_artist_popularity = 0.0
    best_artist_has_provider_identity = False
    if artists:
        best_artist = max(
            artists[:10],
            key=lambda item: search_text_similarity(
                query,
                trim_text(item.get("name") or item.get("artist")),
            ),
        )
        best_artist_name = trim_text(
            best_artist.get("name") or best_artist.get("artist")
        )
        best_artist_popularity = normalized_popularity(best_artist)
        best_artist_has_provider_identity = bool(
            trim_text(
                best_artist.get("provider_artist_id")
                or best_artist.get("id")
            )
        )
    artist_catalog_support = sum(
        1
        for item in (tracks or [])[:16]
        if best_artist_name
        and search_text_similarity(
            best_artist_name,
            trim_text(item.get("channel") or item.get("artist")),
        )
        >= 0.88
    )
    exact_track_support = sum(
        1
        for item in (tracks or [])[:16]
        if search_text_similarity(
            query,
            trim_text(item.get("title") or item.get("name")),
        )
        >= 0.94
    )

    # A high-authority exact artist result is stronger evidence than an
    # unrelated recording that merely shares the query as its title. This
    # resolves ambiguous names such as "Dio" without turning obscure
    # same-name artist uploads into artist searches.
    if (
        top_artist_score >= 0.96
        and best_artist_has_provider_identity
        and best_artist_popularity >= 0.35
    ):
        return "artist"
    if (
        top_artist_score >= 0.88
        and best_artist_has_provider_identity
        and best_artist_popularity >= 0.48
        and artist_catalog_support >= 1
    ):
        return "artist"

    # Track-credit support distinguishes a real artist query from an album or
    # recording that happens to have the same title. This must run before the
    # album rule so "Nirvana" cannot become an obscure same-name album search.
    if (
        top_artist_score >= 0.82
        and artist_catalog_support >= 2
        and (
            top_artist_score >= 0.90
            or top_artist_score >= top_track_score - 0.04
        )
    ):
        return "artist"

    # An exact same-name artist without catalog support is not enough evidence
    # on its own. For example, "The Trooper" should remain a recording query.
    strong_exact_track = any(
        search_text_similarity(
            query,
            trim_text(item.get("title") or item.get("name")),
        )
        >= 0.98
        and bool(trim_text(item.get("channel") or item.get("artist")))
        and bool(
            trim_text(
                item.get("track_key")
                or item.get("canonical_recording_id")
                or item.get("videoId")
                or item.get("id")
            )
        )
        for item in (tracks or [])[:16]
    )
    if strong_exact_track and top_track_score >= 0.94 and artist_catalog_support < 2:
        return "track"
    if (
        top_album_score >= 0.90
        and top_album_score >= top_track_score + 0.04
        and top_album_score >= top_artist_score - 0.01
        and artist_catalog_support < 2
    ):
        return "album"
    if exact_track_support and top_track_score >= 0.90 and artist_catalog_support < 2:
        return "track"
    if top_artist_score >= 0.82 and artist_catalog_support >= 2:
        return "artist"
    if top_track_score >= 0.78 and top_track_score >= max(top_artist_score, top_album_score) + 0.06:
        return "track"
    if top_artist_score >= 0.78 and top_artist_score >= max(top_track_score, top_album_score) + 0.06:
        return "artist"
    if top_album_score >= 0.78 and top_album_score >= max(top_track_score, top_artist_score) + 0.06:
        return "album"
    return "mixed"


def _best_track_match(
    server: Any,
    query: str,
    tracks: Iterable[Dict[str, Any]],
    *,
    preferred_title: str = "",
    preferred_artist: str = "",
) -> Dict[str, Any]:
    valid = [dict(item) for item in tracks or [] if isinstance(item, dict)]
    if not valid:
        return {}

    def match_score(item: Dict[str, Any]) -> tuple[float, float, float]:
        title = trim_text(item.get("title") or item.get("name"))
        artist = trim_text(item.get("channel") or item.get("artist"))
        query_score = max(
            search_text_similarity(query, title),
            search_text_similarity(
                query,
                " ".join(value for value in (title, artist) if value),
            )
            * 0.96,
        )
        canonical_title_score = (
            search_text_similarity(preferred_title, title)
            if preferred_title
            else 0.0
        )
        canonical_artist_score = (
            search_text_similarity(preferred_artist, artist)
            if preferred_artist
            else 0.0
        )
        canonical_pair_score = (
            canonical_title_score * 0.56 + canonical_artist_score * 0.44
            if preferred_title and preferred_artist
            else max(canonical_title_score, canonical_artist_score)
        )
        popularity = parse_compact_number(
            item.get("popularity") or item.get("views"),
        )
        return (
            query_score * 2.0 + canonical_pair_score * 4.0,
            canonical_pair_score,
            popularity,
        )

    return max(
        valid,
        key=match_score,
    )


def _canonical_track_resolution(
    server: Any,
    *,
    query: str,
    fuzzy_tracks: Iterable[Dict[str, Any]],
    musicbrainz_tracks: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    candidates = [
        dict(item)
        for item in [*list(fuzzy_tracks or []), *list(musicbrainz_tracks or [])]
        if isinstance(item, dict)
    ]
    ranked: List[tuple[float, Dict[str, Any]]] = []
    for item in candidates:
        title = trim_text(item.get("title") or item.get("name"))
        artist = trim_text(item.get("channel") or item.get("artist"))
        if not title or not artist:
            continue
        title_similarity = search_text_similarity(query, title)
        combined_similarity = search_text_similarity(
            query,
            f"{title} {artist}".strip(),
        )
        relevance = max(title_similarity, combined_similarity * 0.94)
        if relevance < 0.72:
            continue
        try:
            provider_confidence = float(
                item.get("musicbrainz_score")
                or item.get("source_identity_confidence")
                or item.get("catalog_entity_confidence")
                or 0.0
            )
        except (TypeError, ValueError):
            provider_confidence = 0.0
        try:
            learned_popularity = float(item.get("learned_popularity") or 0.0)
        except (TypeError, ValueError):
            learned_popularity = 0.0
        release_year_text = trim_text(
            item.get("release_year") or item.get("year")
        )
        release_year = (
            int(release_year_text[:4])
            if release_year_text[:4].isdigit()
            else 9999
        )
        chronology_bonus = (
            max(0.0, min((2005 - release_year) / 100.0, 0.35))
            if release_year < 9999
            else 0.0
        )
        score = (
            relevance * 5.0
            + max(0.0, min(provider_confidence, 1.0)) * 2.0
            + max(0.0, min(learned_popularity, 1.0)) * 1.5
            + chronology_bonus
        )
        ranked.append((score, item))
    if not ranked:
        return {}
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    score, item = ranked[0]
    confidence = max(
        search_text_similarity(
            query,
            trim_text(item.get("title") or item.get("name")),
        ),
        min(score / 8.5, 1.0),
    )
    if confidence < 0.76:
        return {}
    return {
        "title": trim_text(item.get("title") or item.get("name")),
        "artist": trim_text(item.get("channel") or item.get("artist")),
        "album": trim_text(item.get("album") or item.get("album_title")),
        "confidence": round(confidence, 4),
        "source": trim_text(
            item.get("source_provider")
            or item.get("metadata_source")
            or "catalog"
        ),
        "musicbrainz_recording_id": trim_text(
            item.get("musicbrainz_recording_id")
        ),
    }


def _best_artist_match(
    query: str,
    artists: Iterable[Dict[str, Any]],
    tracks: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    valid = [dict(item) for item in artists or [] if isinstance(item, dict)]
    if not valid:
        return {}
    credited_names: Dict[str, float] = {}
    credited_ids: Dict[str, float] = {}
    for index, track in enumerate(
        item for item in tracks or [] if isinstance(item, dict)
    ):
        weight = max(0.35, 1.0 - index * 0.035)
        artist_name = normalize_artist_name(
            track.get("channel")
            or track.get("artist")
            or track.get("artist_name")
        )
        artist_id = trim_text(
            track.get("artist_id")
            or track.get("artistId")
            or track.get("artist_browse_id")
        ).casefold()
        if artist_name:
            credited_names[artist_name] = credited_names.get(artist_name, 0.0) + weight
        if artist_id:
            credited_ids[artist_id] = credited_ids.get(artist_id, 0.0) + weight

    def score(item: Dict[str, Any]) -> tuple[float, float, str]:
        name = trim_text(item.get("name") or item.get("artist"))
        name_key = normalize_artist_name(name)
        exact_name = _raw_artist_name(query) == _raw_artist_name(name)
        artist_id = trim_text(
            item.get("provider_artist_id")
            or item.get("id")
            or ""
        ).casefold()
        relationship_score = (
            min(credited_names.get(name_key, 0.0), 6.0) * 1.8
            + min(credited_ids.get(artist_id, 0.0), 6.0) * 2.2
        )
        authority = trim_text(item.get("source_authority")).lower()
        authority_score = (
            2.0
            if authority
            in {
                "official",
                "official_artist_channel",
                "verified_catalog",
                "ytmusic_artist_detail",
            }
            else 0.0
        )
        canonical_score = (
            1.5
            if trim_text(
                item.get("musicbrainz_artist_id")
                or item.get("artist_mbid")
            )
            else 0.0
        )
        catalog_score = min(
            len(list(item.get("albums") or []))
            + len(list(item.get("top_songs") or [])),
            12,
        ) * 0.15
        text_score = search_text_similarity(query, name) * 6.0
        return (
            (12.0 if exact_name else 0.0)
            + text_score
            + relationship_score
            + authority_score
            + canonical_score
            + catalog_score
            + normalized_popularity(item) * 1.5,
            relationship_score,
            name,
        )

    return max(
        valid,
        key=score,
    )


def _best_album_match(
    query: str,
    albums: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    valid = [dict(item) for item in albums or [] if isinstance(item, dict)]
    if not valid:
        return {}
    return max(
        valid,
        key=lambda item: max(
            search_text_similarity(
                query,
                trim_text(item.get("title") or item.get("name")),
            ),
            search_text_similarity(
                query,
                " ".join(
                    value
                    for value in (
                        trim_text(item.get("title") or item.get("name")),
                        trim_text(item.get("artist") or item.get("channel")),
                    )
                    if value
                ),
            )
            * 0.94,
        ),
    )


def _credited_artist_from_track(track: Dict[str, Any]) -> Dict[str, Any]:
    artist_entities = track.get("artist_entities")
    if isinstance(artist_entities, list):
        for entity in artist_entities:
            if not isinstance(entity, dict):
                continue
            name = trim_text(entity.get("name") or entity.get("artist"))
            artist_id = trim_text(
                entity.get("id") or entity.get("browseId") or entity.get("browse_id")
            )
            if name and artist_id:
                return {
                    "id": artist_id,
                    "name": name,
                    "thumbnail": entity.get("thumbnail"),
                }
    name = trim_text(track.get("channel") or track.get("artist"))
    artist_id = trim_text(
        track.get("artist_id")
        or track.get("artistId")
        or track.get("artist_browse_id")
    )
    return {"id": artist_id, "name": name} if name else {}


def _album_from_track(track: Dict[str, Any]) -> Dict[str, Any]:
    album_id = trim_text(
        track.get("album_id") or track.get("albumId") or track.get("browseId")
    )
    title = trim_text(track.get("album") or track.get("album_title"))
    artist = trim_text(track.get("channel") or track.get("artist"))
    if not title:
        return {}
    return {
        "id": album_id,
        "title": title,
        "artist": artist,
        "thumbnail": track.get("thumbnail"),
        "year": track.get("year") or "",
    }


def load_artist_entity_expansion(
    server: Any,
    *,
    artist_id: str,
    artist_name: str,
    user_scope_id: str,
    limit: int,
    include_related: bool = False,
) -> Dict[str, Any]:
    server = adapt_domain_server(server)
    resolved_artist_id = trim_text(artist_id)
    resolved_artist_name = trim_text(artist_name)
    if resolved_artist_id.startswith("musicbrainz:artist:"):
        resolved_artist_id = ""
    artist_entity: Dict[str, Any] = {}
    if (
        not resolved_artist_id
        or resolved_artist_id.startswith(("artist-name:", "derived:"))
    ) and resolved_artist_name:
        matches = search_artists_direct_cached(
            resolved_artist_name,
            4,
            server=server,
        )
        best = _best_artist_match(resolved_artist_name, matches)
        if (
            best
            and search_text_similarity(
                resolved_artist_name,
                trim_text(best.get("name") or best.get("artist")),
            )
            >= 0.78
        ):
            resolved_artist_id = trim_text(best.get("id"))
            artist_entity = dict(best)
    def load_details(provider_artist_id: str) -> Dict[str, Any]:
        if not provider_artist_id:
            return {}
        try:
            return dict(
                server.build_artist_details_payload(
                    provider_artist_id,
                    enrich_related=include_related,
                    lightweight=False,
                )
                or {}
            )
        except Exception:
            return {}

    details = load_details(resolved_artist_id)
    details_name = trim_text(details.get("name"))
    if (
        resolved_artist_name
        and (
            not details
            or (
                details_name
                and search_text_similarity(
                    resolved_artist_name,
                    details_name,
                )
                < 0.78
            )
        )
    ):
        # Track payloads sometimes carry a video-channel ID rather than the
        # YTMusic artist browse ID. Resolve the credited name once and replace
        # the unusable ID instead of accepting an empty artist catalog.
        matches = search_artists_direct_cached(
            resolved_artist_name,
            4,
            server=server,
        )
        best = _best_artist_match(resolved_artist_name, matches)
        best_id = trim_text((best or {}).get("id"))
        if (
            best_id
            and search_text_similarity(
                resolved_artist_name,
                trim_text((best or {}).get("name") or (best or {}).get("artist")),
            )
            >= 0.78
        ):
            replacement_details = load_details(best_id)
            if replacement_details:
                resolved_artist_id = best_id
                artist_entity = dict(best)
                details = replacement_details
    if details:
        artist_entity = {
            "id": resolved_artist_id,
            "name": details.get("name") or resolved_artist_name,
            "thumbnail": details.get("thumbnail"),
            "description": details.get("description") or "",
        }
    elif resolved_artist_id and not artist_entity:
        artist_entity = {
            "id": resolved_artist_id,
            "name": resolved_artist_name,
        }
    related_artists = list(details.get("related_artists") or [])
    albums = list(details.get("albums") or [])
    catalog_tracks = catalog_playable_tracks_for_artist(
        getattr(server, "raw", server),
        user_scope_id=user_scope_id or "guest",
        artist=details.get("name") or resolved_artist_name,
        limit=max(limit, 24),
    )
    tracks: List[Dict[str, Any]] = [
        *list(details.get("top_songs") or []),
        *list(catalog_tracks or []),
    ]
    seen_track_ids: set[str] = set()
    unique_tracks: List[Dict[str, Any]] = []

    def add_tracks(values: Iterable[Dict[str, Any]]) -> None:
        for raw_track in values or []:
            if not isinstance(raw_track, dict):
                continue
            track = dict(raw_track)
            track_id = trim_text(
                track.get("track_key")
                or track.get("canonical_recording_id")
                or track.get("id")
                or track.get("videoId")
            )
            if not track_id:
                track_id = canonical_title_artist_identity(track)
            if not track_id or track_id in seen_track_ids:
                continue
            seen_track_ids.add(track_id)
            unique_tracks.append(track)

    add_tracks(tracks)

    # YTMusic's artist payload normally exposes only a short "top songs"
    # shelf. Artist search needs the catalog, so complete it from the artist's
    # actual album tracklists instead of asking the generic text-search endpoint
    # for more loosely matching uploads.
    target_track_count = max(
        20,
        min(max(int(limit or 0) // 2, 20), 32),
    )
    album_tracklists_loaded = 0
    album_tracklists_attempted = 0
    album_ids = [
        trim_text(
            album.get("id")
            or album.get("album_id")
            or album.get("browseId")
        )
        for album in albums
    ]
    album_ids = [album_id for album_id in album_ids if album_id]
    albums_by_id = {
        trim_text(
            album.get("id")
            or album.get("album_id")
            or album.get("browseId")
        ): dict(album)
        for album in albums
        if isinstance(album, dict)
        and trim_text(
            album.get("id")
            or album.get("album_id")
            or album.get("browseId")
        )
    }
    executor = _search_executor(server)

    def load_album_tracklist(album_id: str) -> Dict[str, Any]:
        try:
            return dict(
                server.assistant_tool_get_album_details(album_id) or {}
            )
        except Exception:
            return {}

    # Album detail calls are independent. Load a small deterministic batch in
    # parallel instead of paying one provider round-trip per album in series.
    # Stop submitting more batches as soon as the catalog target is met.
    for batch_start in range(0, len(album_ids), 4):
        if len(unique_tracks) >= target_track_count:
            break
        batch_ids = album_ids[batch_start : batch_start + 4]
        future_pairs = [
            (album_id, executor.submit(load_album_tracklist, album_id))
            for album_id in batch_ids
        ]
        album_tracklists_attempted += len(future_pairs)
        for _album_id, future in future_pairs:
            try:
                album_payload = dict(future.result() or {})
            except Exception:
                continue
            album_tracks = list(album_payload.get("tracks") or [])
            if not album_tracks:
                continue
            album_record = {
                **dict(albums_by_id.get(_album_id) or {}),
                **{
                    key: value
                    for key, value in album_payload.items()
                    if value not in (None, "", [], {})
                },
            }
            album_thumbnail = catalog_thumbnail_url(
                album_record,
                entity_type="album",
            )
            album_title = trim_text(
                album_record.get("title")
                or album_record.get("name")
                or album_record.get("album")
            )
            hydrated_album_tracks: List[Dict[str, Any]] = []
            for raw_track in album_tracks:
                if not isinstance(raw_track, dict):
                    continue
                track = dict(raw_track)
                track.setdefault("album_id", _album_id)
                if album_title:
                    track.setdefault("album", album_title)
                if (
                    album_thumbnail
                    and not catalog_thumbnail_url(track, entity_type="track")
                ):
                    track["thumbnail"] = album_thumbnail
                hydrated_album_tracks.append(track)
            album_tracklists_loaded += 1
            add_tracks(hydrated_album_tracks)

    catalog_complete = bool(details) and (
        len(unique_tracks) >= target_track_count
        or (
            bool(album_ids)
            and album_tracklists_attempted >= len(album_ids)
            and album_tracklists_loaded >= len(album_ids)
        )
        or (not album_ids and len(unique_tracks) >= 8)
    )

    return {
        "artist": artist_entity,
        "tracks": unique_tracks,
        "albums": albums,
        "related_artists": related_artists,
        "catalog_status": "complete" if catalog_complete else "retryable",
        "album_tracklists_loaded": album_tracklists_loaded,
        "album_tracklists_attempted": album_tracklists_attempted,
    }


def _load_album_entity_expansion(
    server: Any,
    *,
    album_id: str,
) -> Dict[str, Any]:
    if not trim_text(album_id):
        return {}
    try:
        return dict(server.assistant_tool_get_album_details(album_id) or {})
    except Exception:
        return {}


def _retrieval_cache_key(
    legacy_req,
    profile,
    limit: int,
    *,
    server: Any | None = None,
) -> str:
    server = adapt_domain_server(server)
    recent_query_key = "|".join((profile.get("recent_queries") or [])[:4])
    recent_track_key = "|".join(
        trim_text(track.get("id"))
        for track in (profile.get("last_played_tracks") or [])[:4]
        if trim_text(track.get("id"))
    )
    return "||".join(
        [
            trim_text(legacy_req.query).lower(),
            trim_text(getattr(legacy_req, "surface", "") or "search"),
            trim_text(getattr(legacy_req, "result_type", "") or ""),
            "deferred"
            if bool(getattr(legacy_req, "defer_side_surfaces", False))
            else "expanded",
            trim_text(profile.get("user_scope_id") or "guest"),
            recent_query_key,
            recent_track_key,
            str(max(int(limit or 0), 0)),
        ]
    )


def _retrieval_cache_get(cache_key: str) -> Dict[str, Any] | None:
    if not cache_key:
        return None
    with _search_retrieval_cache_lock:
        cached = _search_retrieval_cache.get(cache_key)
        if not cached:
            return None
        if float(cached.get("expires_at") or 0.0) <= time.time():
            _search_retrieval_cache.pop(cache_key, None)
            return None
        return deepcopy(cached.get("payload"))


def _retrieval_cache_set(cache_key: str, payload: Dict[str, Any]) -> None:
    if not cache_key:
        return
    with _search_retrieval_cache_lock:
        _search_retrieval_cache[cache_key] = {
            "expires_at": time.time() + _SEARCH_RETRIEVAL_CACHE_TTL_SECONDS,
            "payload": deepcopy(payload),
        }
        if len(_search_retrieval_cache) > 96:
            expired_keys = [
                key
                for key, entry in _search_retrieval_cache.items()
                if float(entry.get("expires_at") or 0.0) <= time.time()
            ]
            for key in expired_keys:
                _search_retrieval_cache.pop(key, None)
            while len(_search_retrieval_cache) > 96:
                oldest_key = min(
                    _search_retrieval_cache,
                    key=lambda key: float(
                        (_search_retrieval_cache.get(key) or {}).get("expires_at") or 0.0
                    ),
                )
                _search_retrieval_cache.pop(oldest_key, None)


def _search_executor(server: Any):
    return getattr(server, "search_executor", None) or getattr(
        server,
        "recommendation_executor",
    )


def retrieve_search_candidates_fast(
    legacy_req,
    profile,
    *,
    limit: int,
    server: Any | None = None,
) -> Dict[str, Any]:
    server = adapt_domain_server(server)
    query = trim_text(legacy_req.query)
    search_mode = _resolve_retrieval_search_mode(legacy_req, server=server)
    requested_surface = trim_text(
        getattr(legacy_req, "result_type", "") or ""
    ).lower()
    defer_expansion = bool(
        getattr(legacy_req, "defer_side_surfaces", False)
    ) and not requested_surface
    complete_first_page = not requested_surface and not defer_expansion
    if not query:
        return {
            "query_intent": "mixed",
            "track_candidates": {},
            "artist_candidates": {},
            "album_candidates": {},
            "playlists": [],
            "anchor_tracks": [],
            "anchor_artist_names": [],
            "normalized_anchor_artists": set(),
            "retriever_counts": {},
            "retrieval_diagnostics": {
                "mode": "fast_query_fallback",
                "search_mode": search_mode,
                "cache_hit": False,
                "retrieval_ms": 0,
                "partial_completion": False,
            },
        }

    removed_untrusted_aliases = remove_untrusted_catalog_query_aliases(
        getattr(server, "raw", server),
        query=query,
    )
    cache_key = ""
    if not bool(getattr(legacy_req, "force_refresh", False)):
        cache_key = f"fast::{_retrieval_cache_key(legacy_req, profile, limit, server=server)}"
        if not removed_untrusted_aliases:
            cached_payload = _retrieval_cache_get(cache_key)
            if cached_payload is not None:
                diagnostics = dict(cached_payload.get("retrieval_diagnostics") or {})
                diagnostics["cache_hit"] = True
                cached_payload["retrieval_diagnostics"] = diagnostics
                return cached_payload

    local_index_started_at = time.perf_counter()
    fuzzy_memories = load_fuzzy_catalog_entity_memories(
        server,
        query=query,
        limit=max(limit, 12),
    )
    fuzzy_tracks = [
        memory.get("payload")
        for memory in fuzzy_memories
        if memory.get("entity_type") == "track"
        and isinstance(memory.get("payload"), dict)
    ]
    fuzzy_artists = [
        memory.get("payload")
        for memory in fuzzy_memories
        if memory.get("entity_type") == "artist"
        and isinstance(memory.get("payload"), dict)
    ]
    fuzzy_albums = [
        memory.get("payload")
        for memory in fuzzy_memories
        if memory.get("entity_type") == "album"
        and isinstance(memory.get("payload"), dict)
    ]
    strong_fuzzy_artist = any(
        search_text_similarity(
            query,
            trim_text(item.get("name") or item.get("artist")),
        )
        >= 0.90
        for item in fuzzy_artists[:6]
    )
    strong_fuzzy_track = any(
        search_text_similarity(
            query,
            trim_text(item.get("title") or item.get("name")),
        )
        >= 0.84
        for item in fuzzy_tracks[:8]
    )
    local_catalog_can_complete = strong_fuzzy_artist or strong_fuzzy_track

    local_index_ms = int(
        (time.perf_counter() - local_index_started_at) * 1000
    )

    executor = _search_executor(server)
    retrieval_started_at = time.perf_counter()
    track_candidates: Dict[str, Dict[str, Any]] = {}
    artist_candidates: Dict[str, Dict[str, Any]] = {}
    album_candidates: Dict[str, Dict[str, Any]] = {}
    completed_sources: List[str] = []
    timed_out_sources: List[str] = []
    provider_timings_ms: Dict[str, int] = {}

    fast_track_future = executor.submit(
        search_tracks_direct,
        query,
        max(limit * 2, 18),
        server=server,
    )
    fast_artist_future = None
    fast_album_future = None
    if complete_first_page and not local_catalog_can_complete:
        fast_artist_future = executor.submit(
            search_artists_direct_cached,
            query,
            max(limit, 12),
            server=server,
        )
        fast_album_future = executor.submit(
            search_albums_direct,
            query,
            max(limit, 18),
            server=server,
        )
    elif not complete_first_page:
        fast_artist_future = executor.submit(
            search_artists_direct_cached,
            query,
            max(limit, 8),
            server=server,
        )
        fast_album_future = executor.submit(
            search_albums_direct,
            query,
            max(limit, 8),
            server=server,
        )
    canonical_evidence_future = None
    if not (strong_fuzzy_artist and not strong_fuzzy_track):
        canonical_evidence_future = executor.submit(
            search_musicbrainz_recording_items,
            query,
            limit=8,
        )

    canonical_track_query = ""
    for memory in fuzzy_memories:
        if memory.get("entity_type") != "track":
            continue
        payload = memory.get("payload")
        if not isinstance(payload, dict):
            continue
        title = trim_text(payload.get("title") or payload.get("name"))
        artist = trim_text(payload.get("artist") or payload.get("channel"))
        if (
            not title
            or not artist
            or search_text_similarity(query, title) < 0.78
        ):
            continue
        canonical_track_query = f"{title} {artist}".strip()
        break

    future_sources = {
        fast_track_future: "tracks.fast",
    }
    if fast_artist_future is not None:
        future_sources[fast_artist_future] = "artists.fast"
    if fast_album_future is not None:
        future_sources[fast_album_future] = "albums.fast"
    resolved_values: Dict[str, Any] = {
        "tracks.fast": [],
        "artists.fast": [],
        "albums.fast": [],
    }
    pending = set(future_sources)
    provider_deadline = (
        None
        if _SEARCH_DISABLE_TIMEOUTS
        else time.perf_counter()
        + _SEARCH_RETRIEVAL_BRANCH_TIMEOUT_SECONDS
    )
    while pending:
        remaining = (
            None
            if provider_deadline is None
            else max(provider_deadline - time.perf_counter(), 0.0)
        )
        if remaining == 0.0:
            break
        completed_now, pending = wait(
            pending,
            timeout=remaining,
            return_when=FIRST_COMPLETED,
        )
        if not completed_now:
            break
        for future in completed_now:
            source_name = future_sources[future]
            try:
                result = future.result() or {}
                resolved_values[source_name] = list(result or [])
                completed_sources.append(source_name)
                provider_timings_ms[source_name] = int(
                    (time.perf_counter() - retrieval_started_at) * 1000
                )
            except Exception:
                timed_out_sources.append(source_name)
    for future in pending:
        source_name = future_sources[future]
        timed_out_sources.append(source_name)
        provider_timings_ms[source_name] = int(
            (time.perf_counter() - retrieval_started_at) * 1000
        )
        future.cancel()

    fast_tracks = list(resolved_values["tracks.fast"] or [])
    canonical_tracks: List[Dict[str, Any]] = []
    fast_artists = list(resolved_values["artists.fast"] or [])
    fast_albums = list(resolved_values["albums.fast"] or [])
    fast_playlists: List[Dict[str, Any]] = []
    musicbrainz_tracks: List[Dict[str, Any]] = []

    # A known canonical track should not force a second provider request when
    # the direct result already agrees with its title and artist. Only resolve
    # the longer exact query when the first provider result conflicts.
    local_resolution = _canonical_track_resolution(
        server,
        query=query,
        fuzzy_tracks=fuzzy_tracks,
        musicbrainz_tracks=[],
    )
    direct_match = _best_track_match(
        server,
        query,
        fast_tracks,
        preferred_title=trim_text(local_resolution.get("title")),
        preferred_artist=trim_text(local_resolution.get("artist")),
    )
    expected_title = trim_text(local_resolution.get("title"))
    expected_artist = trim_text(local_resolution.get("artist"))
    direct_agrees = bool(direct_match) and (
        not expected_title
        or search_text_similarity(
            expected_title,
            trim_text(direct_match.get("title") or direct_match.get("name")),
        )
        >= 0.88
    ) and (
        not expected_artist
        or search_text_similarity(
            expected_artist,
            trim_text(direct_match.get("channel") or direct_match.get("artist")),
        )
        >= 0.82
    )
    if (
        canonical_track_query
        and server.normalize_text(canonical_track_query)
        != server.normalize_text(query)
        and float(local_resolution.get("confidence") or 0.0) >= 0.88
        and not direct_agrees
    ):
        canonical_started_at = time.perf_counter()
        try:
            canonical_tracks = list(
                search_tracks_direct(
                    canonical_track_query,
                    max(limit, 12),
                    server=server,
                )
                or []
            )
            completed_sources.append("tracks.canonical")
        except Exception:
            timed_out_sources.append("tracks.canonical")
        provider_timings_ms["tracks.canonical"] = int(
            (time.perf_counter() - canonical_started_at) * 1000
        )

    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=[*canonical_tracks, *fast_tracks],
        source_name=(
            "provider_intent"
            if search_mode == "taste"
            else "fast_query"
        ),
        base_score=4.3,
    )
    _collect_artist_candidates(
        artist_candidates,
        artists=fast_artists,
        source_name="fast_artist",
        base_score=4.0,
    )
    _collect_album_candidates(
        album_candidates,
        albums=fast_albums,
        source_name="fast_album",
        base_score=3.9,
    )
    _collect_track_candidates(
        track_candidates,
        server=server,
        tracks=fuzzy_tracks,
        source_name="catalog_fuzzy",
        base_score=4.7,
    )
    _collect_artist_candidates(
        artist_candidates,
        artists=fuzzy_artists,
        source_name="catalog_fuzzy",
        base_score=4.7,
    )
    _collect_album_candidates(
        album_candidates,
        albums=fuzzy_albums,
        source_name="catalog_fuzzy",
        base_score=4.7,
    )

    query_intent = classify_query_intent(
        server=server,
        query=query,
        # Live exact-query candidates must be evaluated before fuzzy catalog
        # memories. A stale local alias must never push an exact provider track
        # outside the classifier's first-page evidence window.
        tracks=[*canonical_tracks, *fast_tracks, *fuzzy_tracks][:24],
        artists=[*fast_artists, *fuzzy_artists][:16],
        albums=[*fast_albums, *fuzzy_albums][:16],
    )
    canonical_resolution: Dict[str, Any] = {}
    if query_intent in {"track", "mixed"} and not defer_expansion:
        canonical_resolution = _canonical_track_resolution(
            server,
            query=query,
            fuzzy_tracks=fuzzy_tracks,
            musicbrainz_tracks=[],
        )
        if float(canonical_resolution.get("confidence") or 0.0) < 0.88:
            canonical_evidence_future = canonical_evidence_future or executor.submit(
                search_musicbrainz_recording_items,
                query,
                limit=8,
            )
            elapsed_seconds = time.perf_counter() - retrieval_started_at
            remaining_seconds = max(
                _SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS - elapsed_seconds,
                0.0,
            )
            if _SEARCH_DISABLE_TIMEOUTS:
                canonical_done = {canonical_evidence_future}
                wait(canonical_done)
            elif remaining_seconds > 0.0:
                canonical_done, _ = wait(
                    {canonical_evidence_future},
                    timeout=min(remaining_seconds, 1.5),
                )
            else:
                canonical_done = set()
            if canonical_evidence_future in canonical_done:
                try:
                    musicbrainz_tracks = list(
                        canonical_evidence_future.result() or []
                    )
                    completed_sources.append("canonical.musicbrainz")
                except Exception:
                    timed_out_sources.append("canonical.musicbrainz")
            else:
                timed_out_sources.append("canonical.musicbrainz")
                canonical_evidence_future.cancel()
            canonical_resolution = _canonical_track_resolution(
                server,
                query=query,
                fuzzy_tracks=fuzzy_tracks,
                musicbrainz_tracks=musicbrainz_tracks,
            )
        else:
            if canonical_evidence_future is not None:
                canonical_evidence_future.cancel()
    else:
        if canonical_evidence_future is not None:
            canonical_evidence_future.cancel()
    primary_track = _best_track_match(
        server,
        query,
        [*canonical_tracks, *fast_tracks],
        preferred_title=trim_text(canonical_resolution.get("title")),
        preferred_artist=trim_text(canonical_resolution.get("artist")),
    )
    primary_artist = _best_artist_match(
        query,
        [*fuzzy_artists, *fast_artists],
        [*canonical_tracks, *fast_tracks],
    )
    primary_album = _best_album_match(
        query,
        [*fuzzy_albums, *fast_albums],
    )
    credited_artist = _credited_artist_from_track(primary_track)
    track_album = _album_from_track(primary_track)
    track_expansion_artist = credited_artist
    if (
        primary_artist
        and credited_artist
        and normalize_artist_name(
            primary_artist.get("name") or primary_artist.get("artist")
        )
        == normalize_artist_name(credited_artist.get("name"))
    ):
        track_expansion_artist = primary_artist

    if credited_artist:
        _collect_artist_candidates(
            artist_candidates,
            artists=[credited_artist],
            source_name="credited_artist",
            base_score=5.6,
        )
    if query_intent == "artist" and primary_artist:
        _collect_artist_candidates(
            artist_candidates,
            artists=[primary_artist],
            source_name="resolved_artist",
            base_score=6.2,
        )
    if track_album:
        _collect_album_candidates(
            album_candidates,
            albums=[track_album],
            source_name="track_album",
            base_score=5.3,
        )

    expansion_future = None
    expansion_kind = ""
    if defer_expansion:
        expansion_future = None
    elif query_intent in {"track", "mixed"} and track_expansion_artist:
        # SearchService owns canonical artist completion for the initial
        # response. Running it here as well performed the same artist details
        # and album-tracklist work twice before one /search could return.
        expansion_future = None
    elif complete_first_page:
        expansion_future = None
    elif query_intent == "album" and primary_album:
        expansion_kind = "album"
        expansion_future = executor.submit(
            _load_album_entity_expansion,
            server,
            album_id=trim_text(
                primary_album.get("id")
                or primary_album.get("album_id")
                or primary_album.get("albumId")
            ),
        )

    expansion_payload: Dict[str, Any] = {}
    if expansion_future is not None:
        elapsed_seconds = time.perf_counter() - retrieval_started_at
        remaining_seconds = max(
            _SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS - elapsed_seconds,
            0.0,
        )
        if _SEARCH_DISABLE_TIMEOUTS:
            expansion_done = {expansion_future}
            wait(expansion_done)
        elif remaining_seconds > 0.0:
            expansion_done, _ = wait(
                {expansion_future},
                timeout=remaining_seconds,
            )
        else:
            expansion_done = set()
        if expansion_future in expansion_done:
            try:
                expansion_payload = dict(expansion_future.result() or {})
                completed_sources.append(f"structured.{expansion_kind}")
                provider_timings_ms[f"structured.{expansion_kind}"] = int(
                    (time.perf_counter() - retrieval_started_at) * 1000
                )
            except Exception:
                timed_out_sources.append(f"structured.{expansion_kind}")
        else:
            timed_out_sources.append(f"structured.{expansion_kind}")
            provider_timings_ms[f"structured.{expansion_kind}"] = int(
                (time.perf_counter() - retrieval_started_at) * 1000
            )
            expansion_future.cancel()

    if expansion_kind in {"artist", "track_artist"} and expansion_payload:
        expanded_artist = expansion_payload.get("artist")
        if isinstance(expanded_artist, dict) and expanded_artist:
            _collect_artist_candidates(
                artist_candidates,
                artists=[expanded_artist],
                source_name=(
                    "resolved_artist"
                    if expansion_kind == "artist"
                    else "credited_artist"
                ),
                base_score=5.8,
            )
        _collect_track_candidates(
            track_candidates,
            server=server,
            tracks=list(expansion_payload.get("tracks") or []),
            source_name=(
                "artist_catalog"
                if expansion_kind == "artist"
                else "same_artist_catalog"
            ),
            base_score=4.8,
        )
        _collect_album_candidates(
            album_candidates,
            albums=list(expansion_payload.get("albums") or []),
            source_name=(
                "artist_discography"
                if expansion_kind == "artist"
                else "credited_artist_discography"
            ),
            base_score=4.6,
        )
    elif expansion_kind == "album" and expansion_payload:
        _collect_album_candidates(
            album_candidates,
            albums=[expansion_payload],
            source_name="resolved_album",
            base_score=5.8,
        )
        _collect_track_candidates(
            track_candidates,
            server=server,
            tracks=list(expansion_payload.get("tracks") or []),
            source_name="album_tracklist",
            base_score=4.8,
        )

    anchor_tracks = semantic_search_anchor_tracks(
        legacy_req,
        [
            entry.get("payload")
            for entry in track_candidates.values()
            if isinstance(entry.get("payload"), dict)
        ],
        [*canonical_tracks, *fast_tracks],
        limit=4,
        server=server,
    )
    anchor_artist_names = semantic_search_anchor_artist_names(anchor_tracks, 6, server=server)
    normalized_anchor_artists = {
        server.normalize_text(name)
        for name in anchor_artist_names
        if server.normalize_text(name)
    }

    related_artists: List[Dict[str, Any]] = []
    seen_related_artist_ids: set[str] = set()
    primary_artist_id = trim_text(
        (expansion_payload.get("artist") or {}).get("id")
        if isinstance(expansion_payload.get("artist"), dict)
        else ""
    )
    primary_artist_name = server.normalize_text(
        (expansion_payload.get("artist") or {}).get("name")
        if isinstance(expansion_payload.get("artist"), dict)
        else ""
    )
    for raw_artist in list(expansion_payload.get("related_artists") or []):
        if not isinstance(raw_artist, dict):
            continue
        artist_id = trim_text(raw_artist.get("id"))
        artist_name = trim_text(
            raw_artist.get("name")
            or raw_artist.get("artist")
            or raw_artist.get("title")
        )
        normalized_name = server.normalize_text(artist_name)
        identity = artist_id or normalized_name
        if (
            not identity
            or identity in seen_related_artist_ids
            or (primary_artist_id and artist_id == primary_artist_id)
            or (primary_artist_name and normalized_name == primary_artist_name)
        ):
            continue
        seen_related_artist_ids.add(identity)
        related_artists.append(dict(raw_artist))
        if len(related_artists) >= 36:
            break

    payload = {
        "query_intent": query_intent,
        "track_candidates": track_candidates,
        "artist_candidates": artist_candidates,
        "album_candidates": album_candidates,
        "playlists": fast_playlists,
        "related_artists": related_artists,
        "resolved_artist": (
            primary_artist
            if query_intent == "artist"
            else credited_artist
            if query_intent in {"track", "mixed"}
            else {}
        ),
        "canonical_resolution": canonical_resolution,
        "anchor_tracks": anchor_tracks,
        "anchor_artist_names": anchor_artist_names,
        "normalized_anchor_artists": normalized_anchor_artists,
        "retriever_counts": {
            "track_candidates": len(track_candidates),
            "artist_candidates": len(artist_candidates),
            "album_candidates": len(album_candidates),
            "catalog_fuzzy": len(fuzzy_memories),
            "canonical_exact_tracks": len(canonical_tracks),
            "structured_tracks": len(list(expansion_payload.get("tracks") or [])),
            "structured_albums": len(list(expansion_payload.get("albums") or [])),
            "related_artists": len(related_artists),
            "canonical_evidence": len(musicbrainz_tracks),
        },
        "retrieval_diagnostics": {
            "mode": "fast_query_fallback",
            "search_mode": search_mode,
            "cache_hit": False,
            "partial_completion": bool(
                track_candidates or artist_candidates or album_candidates
            ),
            "retrieval_ms": int((time.perf_counter() - retrieval_started_at) * 1000),
            "deadline_ms": int(_SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS * 1000),
            "timeouts_disabled": bool(_SEARCH_DISABLE_TIMEOUTS),
            "completed_sources": completed_sources,
            "timed_out_sources": timed_out_sources,
            "provider_timings_ms": provider_timings_ms,
            "local_index_ms": local_index_ms,
            "removed_untrusted_aliases": removed_untrusted_aliases,
            "canonical_track_query": canonical_track_query,
            "canonical_resolution": canonical_resolution,
            "structured_expansion": expansion_kind,
            "provider_plan": (
                "direct_plus_local_catalog"
                if complete_first_page and local_catalog_can_complete
                else "typed_parallel"
                if complete_first_page
                else "surface_filtered"
            ),
        },
    }
    if cache_key:
        _retrieval_cache_set(cache_key, payload)
    return payload
