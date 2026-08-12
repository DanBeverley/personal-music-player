from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, wait
from copy import deepcopy
import math
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
    normalize_track_title,
    normalized_popularity,
    parse_compact_number,
)
from .server_adapter import adapt_domain_server
from ..search.query_mode import resolve_search_mode
from ..search.intelligence import (
    load_catalog_entity_memories,
    load_fuzzy_catalog_entity_memories,
    remove_unconfirmed_display_memories,
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
_SEARCH_CANONICAL_CONFLICT_GRACE_SECONDS = 1.4
_SEARCH_DECISIVE_CANONICAL_GRACE_SECONDS = max(
    0.1, float(os.environ.get("AURALIS_SEARCH_DECISIVE_CANONICAL_GRACE_SECONDS", "0.75"))
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


def _provider_identity(value: Any, *, entity_type: str) -> str:
    identity = trim_text(value)
    if not identity:
        return ""
    lowered = identity.casefold()
    rejected_prefixes = {
        "track": ("musicbrainz:recording:", "track-name:", "derived:"),
        "artist": ("musicbrainz:artist:", "artist-name:", "derived:"),
        "album": (
            "musicbrainz:release:",
            "musicbrainz:release-group:",
            "album-name:",
            "derived:",
        ),
    }
    return "" if lowered.startswith(rejected_prefixes.get(entity_type, ())) else identity


def _track_provider_identity(track: Dict[str, Any]) -> str:
    playback = track.get("playback") if isinstance(track.get("playback"), dict) else {}
    return _provider_identity(
        playback.get("source_id")
        or playback.get("video_id")
        or track.get("playback_source_id")
        or track.get("videoId")
        or track.get("video_id")
        or track.get("id"),
        entity_type="track",
    )


def _artist_provider_identity(artist: Dict[str, Any]) -> str:
    return _provider_identity(
        artist.get("provider_artist_id")
        or artist.get("browseId")
        or artist.get("artist_id")
        or artist.get("id"),
        entity_type="artist",
    )


def _canonical_artist_identity_keys(artist: Dict[str, Any]) -> set[str]:
    """Return explicit MusicBrainz artist identities carried by one record."""
    values: List[Any] = [
        artist.get("musicbrainz_artist_id"),
        artist.get("artist_mbid"),
        artist.get("canonical_artist_id"),
    ]
    values.extend(list(artist.get("musicbrainz_artist_ids") or []))
    raw_id = trim_text(artist.get("id"))
    if raw_id.casefold().startswith("musicbrainz:artist:"):
        values.append(raw_id)
    identities: set[str] = set()
    for value in values:
        identity = trim_text(value).casefold()
        if not identity:
            continue
        if identity.startswith("musicbrainz:artist:"):
            identity = identity.split("musicbrainz:artist:", 1)[1]
        if identity:
            identities.add(f"musicbrainz:artist:{identity}")
    return identities


def _merge_linked_artist_records(
    provider_artist: Dict[str, Any],
    canonical_artist: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine canonical authority with the record used for provider navigation."""
    merged = dict(canonical_artist)
    for key, value in provider_artist.items():
        if value not in (None, "", [], {}):
            merged[key] = deepcopy(value)
    for key in ("albums", "top_songs", "aliases", "musicbrainz_artist_ids"):
        combined: List[Any] = []
        seen: set[str] = set()
        for value in [
            *list(provider_artist.get(key) or []),
            *list(canonical_artist.get(key) or []),
        ]:
            marker = repr(value)
            if marker in seen:
                continue
            seen.add(marker)
            combined.append(deepcopy(value))
        if combined:
            merged[key] = combined
    for key in ("popularity", "learned_popularity", "_catalog_confidence"):
        merged[key] = max(
            float(provider_artist.get(key) or 0.0),
            float(canonical_artist.get(key) or 0.0),
        )
    provider_id = _artist_provider_identity(provider_artist)
    merged["provider_artist_id"] = provider_id
    if not trim_text(merged.get("id")) or trim_text(merged.get("id")).casefold().startswith(
        "musicbrainz:artist:"
    ):
        merged["id"] = provider_id
    merged["canonical_identity_linked"] = True
    return merged


def _coalesce_linked_artist_candidates(
    artists: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Emit one provider-navigable record for each explicitly linked artist."""
    provider_records: Dict[str, Dict[str, Any]] = {}
    canonical_only: List[Dict[str, Any]] = []
    for value in artists or []:
        if not isinstance(value, dict):
            continue
        artist = dict(value)
        provider_id = _artist_provider_identity(artist).casefold()
        if not provider_id:
            canonical_only.append(artist)
            continue
        previous = provider_records.get(provider_id)
        provider_records[provider_id] = (
            _merge_linked_artist_records(previous, artist)
            if previous is not None
            else artist
        )

    for canonical_artist in canonical_only:
        canonical_keys = _canonical_artist_identity_keys(canonical_artist)
        if not canonical_keys:
            continue
        for provider_id, provider_artist in list(provider_records.items()):
            if not canonical_keys.intersection(
                _canonical_artist_identity_keys(provider_artist)
            ):
                continue
            provider_records[provider_id] = _merge_linked_artist_records(
                provider_artist,
                canonical_artist,
            )
    return list(provider_records.values())


def _album_provider_identity(album: Dict[str, Any]) -> str:
    return _provider_identity(
        album.get("provider_album_id")
        or album.get("browseId")
        or album.get("album_id")
        or album.get("albumId")
        or album.get("id"),
        entity_type="album",
    )


def _artist_relationship_support(
    artist: Dict[str, Any],
    tracks: Iterable[Dict[str, Any]],
) -> float:
    provider_id = _artist_provider_identity(artist).casefold()
    name_key = normalize_artist_name(artist.get("name") or artist.get("artist"))
    support = 0.0
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        credited_id = trim_text(
            track.get("artist_id")
            or track.get("artistId")
            or track.get("artist_browse_id")
        ).casefold()
        credited_name = normalize_artist_name(
            track.get("channel") or track.get("artist") or track.get("artist_name")
        )
        if provider_id and credited_id and provider_id == credited_id:
            support += 1.25
        elif name_key and credited_name and name_key == credited_name:
            support += 1.0
    return support


def _matching_provider_artist(
    credited_artist: Dict[str, Any],
    artists: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    credited_id = _artist_provider_identity(credited_artist).casefold()
    if not credited_id:
        return dict(credited_artist or {})
    match = next(
        (
            dict(artist)
            for artist in artists or []
            if isinstance(artist, dict)
            and _artist_provider_identity(artist).casefold() == credited_id
        ),
        {},
    )
    return {**match, **dict(credited_artist or {})} if match else dict(credited_artist)


def _artist_from_album(
    album: Dict[str, Any],
    artists: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    name = trim_text(
        album.get("artist") or album.get("artist_name") or album.get("channel")
    )
    artist_id = trim_text(
        album.get("artist_id")
        or album.get("artistId")
        or album.get("artist_browse_id")
    )
    if artist_id:
        matched = _matching_provider_artist(
            {"id": artist_id, "name": name},
            artists,
        )
        if matched:
            return matched
    return {"name": name} if name else {}


def _provider_recording_families(
    query: str,
    tracks: Iterable[Dict[str, Any]],
    *,
    artists: Iterable[Dict[str, Any]] = (),
    albums: Iterable[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """Compare exact-title credits; provider order is only a tie-breaker."""
    provider_tracks = [
        dict(track) for track in tracks or [] if isinstance(track, dict)
    ]
    provider_artists = [
        dict(artist) for artist in artists or [] if isinstance(artist, dict)
    ]
    provider_albums = [
        dict(album) for album in albums or [] if isinstance(album, dict)
    ]
    groups: Dict[str, Dict[str, Any]] = {}
    catalog_credit_counts: Dict[str, int] = {}
    for track in provider_tracks:
        artist_key = normalize_artist_name(track.get("channel") or track.get("artist"))
        if artist_key:
            catalog_credit_counts[artist_key] = catalog_credit_counts.get(artist_key, 0) + 1
    for index, track in enumerate(provider_tracks):
        title = trim_text(track.get("title") or track.get("name"))
        artist = trim_text(track.get("channel") or track.get("artist"))
        title_match = search_text_similarity(query, title)
        artist_key = normalize_artist_name(artist)
        if title_match < 0.94 or not artist_key or not _track_provider_identity(track):
            continue
        authority = trim_text(
            track.get("source_authority") or track.get("authority")
        ).casefold()
        authority_score = (
            1.0
            if authority
            in {
                "official",
                "official_artist_channel",
                "topic",
                "vevo",
                "verified_catalog",
                "ytmusic_artist_detail",
            }
            else 0.35
            if trim_text(track.get("artist_id") or track.get("artistId") or track.get("channel_id"))
            else 0.0
        )
        group = groups.setdefault(
            artist_key,
            {
                "artist": artist,
                "artist_key": artist_key,
                "best_index": index,
                "best_track": track,
                "exact_count": 0,
                "views": 0.0,
                "popularity": 0.0,
                "authority": 0.0,
                "stable_artist_id": "",
                "stable_album_id": "",
            },
        )
        if index < int(group["best_index"]):
            group["best_index"] = index
            group["best_track"] = track
        if title_match >= 0.98:
            group["exact_count"] = int(group["exact_count"]) + 1
        group["views"] = max(
            float(group["views"]),
            parse_compact_number(track.get("views") or track.get("view_count")),
        )
        group["popularity"] = max(float(group["popularity"]), normalized_popularity(track))
        group["authority"] = max(float(group["authority"]), authority_score)
        group["stable_artist_id"] = trim_text(
            group.get("stable_artist_id")
            or track.get("artist_id")
            or track.get("artistId")
            or track.get("channel_id")
        )
        group["stable_album_id"] = trim_text(
            group.get("stable_album_id")
            or track.get("album_id")
            or track.get("albumId")
            or track.get("browseId")
        )

    result_count = max(len(provider_tracks), 1)
    for group in groups.values():
        artist_key = str(group["artist_key"])
        artist_score = 0.0
        for artist in provider_artists:
            if normalize_artist_name(artist.get("name") or artist.get("artist")) != artist_key:
                continue
            authority = trim_text(artist.get("source_authority")).casefold()
            catalog_items = len(list(artist.get("top_songs") or [])) + len(
                list(artist.get("albums") or [])
            )
            artist_score = max(
                artist_score,
                (0.55 if _artist_provider_identity(artist) else 0.0)
                + normalized_popularity(artist) * 1.4
                + (0.65 if authority in {"official", "official_artist_channel", "verified_catalog", "ytmusic_artist_detail"} else 0.0)
                + min(catalog_items, 8) * 0.12,
            )
        album_score = 0.0
        for album in provider_albums:
            album_artist = normalize_artist_name(
                album.get("artist") or album.get("artist_name") or album.get("channel")
            )
            if album_artist != artist_key:
                continue
            title_match = search_text_similarity(
                query, trim_text(album.get("title") or album.get("name"))
            )
            if title_match >= 0.94:
                album_score = max(
                    album_score,
                    2.4 * title_match
                    + (0.45 if _album_provider_identity(album) else 0.0)
                    + normalized_popularity(album) * 0.8,
                )
        best_track = dict(group["best_track"])
        track_album_title = trim_text(best_track.get("album") or best_track.get("album_title"))
        if track_album_title and search_text_similarity(query, track_album_title) >= 0.94:
            album_score = max(album_score, 2.2)
        rank_tiebreaker = max(
            0.0, 0.35 * (1.0 - (int(group["best_index"]) / result_count))
        )
        catalog_credit_count = catalog_credit_counts.get(artist_key, 0)
        view_popularity = (
            max(0.0, min(math.log10(float(group["views"]) + 1.0) / 9.0, 1.0))
            if float(group["views"]) > 0.0
            else 0.0
        )
        group["catalog_credit_count"] = catalog_credit_count
        group["view_popularity"] = round(view_popularity, 4)
        group["artist_evidence_score"] = round(artist_score, 4)
        group["album_relationship_score"] = round(album_score, 4)
        group["rank_tiebreaker"] = round(rank_tiebreaker, 4)
        group["intent_score"] = round(
            min(int(group["exact_count"]), 3) * 0.70
            + float(group["authority"])
            + float(group["popularity"]) * 1.8
            + view_popularity * 4.0
            + min(catalog_credit_count, 8) * 0.20
            + artist_score
            + album_score
            + (0.25 if group.get("stable_artist_id") else 0.0)
            + (0.20 if group.get("stable_album_id") else 0.0)
            + rank_tiebreaker,
            4,
        )
    return sorted(
        groups.values(),
        key=lambda group: (
            float(group.get("intent_score") or 0.0),
            float(group.get("popularity") or 0.0),
            int(group.get("exact_count") or 0),
            str(group.get("artist_key") or ""),
        ),
        reverse=True,
    )


def resolve_search_target(
    *,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
    canonical_resolution: Dict[str, Any] | None = None,
    search_mode: str = "",
    entity_type_hint: str = "",
    relationship_tracks: Iterable[Dict[str, Any]] | None = None,
    server=None,
) -> Dict[str, Any]:
    """Resolve one evidence-backed entity before any artist expansion.

    Text similarity discovers candidates; it never owns identity.  The
    returned bundle is the only authority for target-dependent search
    surfaces.  When competing evidence is too close, the safe result is a
    mixed page without an inferred lead artist.
    """
    server = adapt_domain_server(server)
    normalized_query = trim_text(query).casefold()
    empty = {
        "entity_type": "mixed",
        "item": {},
        "lead_artist": {},
        "containing_album": {},
        "target_identity": "",
        "confidence": 0.0,
        "confidence_tier": "ambiguous",
        "decision_margin": 0.0,
        "evidence": [],
        "resolver": "classify_then_resolve_v2",
    }
    if not normalized_query:
        return empty
    canonical = dict(canonical_resolution or {})
    canonical_title = trim_text(canonical.get("title"))
    canonical_artist = trim_text(canonical.get("artist"))
    primary_track = _best_track_match(
        server,
        query,
        tracks,
        preferred_title=canonical_title,
        preferred_artist=canonical_artist,
    )
    relationship_evidence = list(
        tracks if relationship_tracks is None else relationship_tracks
    )
    primary_artist = _best_artist_match(query, artists, relationship_evidence)
    primary_album = _best_album_match(query, albums)
    provider_recording_decision = _provider_recording_decision(
        query,
        tracks,
        artists=artists,
        albums=albums,
    )
    candidates: List[Dict[str, Any]] = []

    explicit_track = bool(re.search(r"\b(song|track|lyrics)\b", normalized_query))
    explicit_artist = bool(re.search(r"\b(artist|band|singer)\b", normalized_query))
    explicit_album = bool(re.search(r"\b(album|ep|soundtrack|ost)\b", normalized_query))

    if primary_track:
        title = trim_text(primary_track.get("title") or primary_track.get("name"))
        credited_name = trim_text(
            primary_track.get("channel") or primary_track.get("artist")
        )
        title_match = search_text_similarity(query, title)
        canonical_title_match = (
            search_text_similarity(canonical_title, title) if canonical_title else 0.0
        )
        canonical_artist_match = (
            search_text_similarity(canonical_artist, credited_name)
            if canonical_artist
            else 0.0
        )
        canonical_pair_match = (
            canonical_title_match * 0.56 + canonical_artist_match * 0.44
            if canonical_title and canonical_artist
            else 0.0
        )
        stable_id = _track_provider_identity(primary_track)
        provider_lead_track = dict(
            provider_recording_decision.get("track") or {}
        )
        structurally_supported_lead = bool(
            provider_recording_decision.get("structurally_supported")
            and stable_id
            and stable_id.casefold()
            == _track_provider_identity(provider_lead_track).casefold()
        )
        provider_authority_supported = bool(
            structurally_supported_lead
            and provider_recording_decision.get("authority_supported")
        )
        credited_artist = _matching_provider_artist(
            _credited_artist_from_track(primary_track),
            artists,
        )
        canonical_recording_id = trim_text(
            canonical.get("musicbrainz_recording_id")
        )
        canonical_artist_id = trim_text(canonical.get("musicbrainz_artist_id"))
        resolved_track = dict(primary_track)
        if canonical_recording_id:
            resolved_track["musicbrainz_recording_id"] = canonical_recording_id
            resolved_track["canonical_recording_id"] = canonical_recording_id
            resolved_track["track_key"] = f"recording:{canonical_recording_id}"
        if canonical_artist_id:
            resolved_track["musicbrainz_artist_id"] = canonical_artist_id
            resolved_track["canonical_artist_id"] = (
                f"musicbrainz:artist:{canonical_artist_id}"
            )
            credited_artist = {
                **credited_artist,
                "musicbrainz_artist_id": canonical_artist_id,
                "canonical_artist_id": f"musicbrainz:artist:{canonical_artist_id}",
            }
        for key in (
            "musicbrainz_artist_ids",
            "musicbrainz_release_id",
            "musicbrainz_release_group_id",
            "release_date",
            "release_year",
            "country",
            "recording_family_count",
            "recording_family_score",
            "recording_family_margin",
        ):
            value = canonical.get(key)
            if value not in (None, "", [], {}):
                resolved_track[key] = deepcopy(value)
        containing_album = _bind_track_containing_album(
            primary_track,
            albums=albums,
            canonical_resolution=canonical,
        )
        mismatch_penalty = (
            5.0
            if canonical_title_match >= 0.90 and canonical_artist_match < 0.62
            else 0.0
        )
        independent_canonical = bool(
            canonical.get("independent_provider_corroboration")
        )
        score = (
            title_match * 4.0
            + (2.0 if title_match >= 0.98 else 0.0)
            + (1.0 if stable_id else 0.0)
            + (0.6 if credited_name else 0.0)
            # Canonical agreement proves that the recording exists; it is not
            # equally strong evidence that the user intended a recording
            # rather than an exact-name artist or album.
            + canonical_pair_match * (5.0 if independent_canonical else 2.0)
            + normalized_popularity(primary_track) * 1.5
            + (1.25 if structurally_supported_lead else 0.0)
            + (0.5 if provider_authority_supported else 0.0)
            + (2.5 if explicit_track else 0.0)
            - mismatch_penalty
        )
        evidence = ["exact_title"] if title_match >= 0.98 else ["title_match"]
        if stable_id:
            evidence.append("provider_track_identity")
        if canonical_pair_match >= 0.80 and independent_canonical:
            evidence.append("canonical_recording_credit")
        if credited_name:
            evidence.append("credited_artist")
        if structurally_supported_lead:
            evidence.append("provider_structural_lead")
        if provider_authority_supported:
            evidence.append("source_authority")
        if provider_recording_decision.get("credit_count"):
            evidence.append("recording_family_comparison")
        if title_match >= 0.78 and stable_id and credited_name:
            candidates.append(
                {
                    "entity_type": "track",
                    "item": resolved_track,
                    "lead_artist": credited_artist,
                    "containing_album": containing_album,
                    "target_identity": (
                        f"musicbrainz:recording:{canonical_recording_id}"
                        if canonical_pair_match >= 0.80
                        and independent_canonical
                        and canonical_recording_id
                        else f"provider:track:{stable_id.casefold()}"
                    ),
                    "canonical_identity": {
                        "recording_id": canonical_recording_id,
                        "artist_id": canonical_artist_id,
                        "artist_ids": list(
                            canonical.get("musicbrainz_artist_ids") or []
                        ),
                        "release_id": trim_text(
                            canonical.get("musicbrainz_release_id")
                        ),
                        "release_group_id": trim_text(
                            canonical.get("musicbrainz_release_group_id")
                        ),
                    },
                    "score": score,
                    "popularity": normalized_popularity(primary_track),
                    "structurally_supported_lead": structurally_supported_lead,
                    "recording_family_count": int(
                        provider_recording_decision.get("credit_count") or 0
                    ),
                    "recording_family_margin": float(
                        provider_recording_decision.get("decision_margin") or 0.0
                    ),
                    "confidence_tier": (
                        "authoritative"
                        if canonical_pair_match >= 0.80
                        and independent_canonical
                        else "corroborated"
                    ),
                    "evidence": evidence,
                }
            )

    if primary_artist:
        name = trim_text(primary_artist.get("name") or primary_artist.get("artist"))
        name_match = search_text_similarity(query, name)
        stable_id = _artist_provider_identity(primary_artist)
        relationship_support = _artist_relationship_support(
            primary_artist,
            relationship_evidence,
        )
        authority = trim_text(primary_artist.get("source_authority")).casefold()
        catalog_items = len(list(primary_artist.get("top_songs") or [])) + len(
            list(primary_artist.get("albums") or [])
        )
        authority_supported = authority in {
            "official",
            "official_artist_channel",
            "verified_catalog",
            "ytmusic_artist_detail",
        }
        canonical_artist_supported = bool(
            trim_text(
                primary_artist.get("musicbrainz_artist_id")
                or primary_artist.get("artist_mbid")
            )
        )
        artist_supported = (
            relationship_support >= 1.75
            or normalized_popularity(primary_artist) >= 0.25
            or authority_supported
            or catalog_items >= 2
        )
        artist_established = bool(
            authority_supported
            or canonical_artist_supported
            or relationship_support >= 2.5
            or normalized_popularity(primary_artist) >= 0.55
            or (
                relationship_support >= 1.75
                and normalized_popularity(primary_artist) >= 0.35
            )
            or catalog_items >= 4
        )
        score = (
            name_match * 4.0
            + (2.0 if _raw_artist_name(query) == _raw_artist_name(name) else 0.0)
            + (1.0 if stable_id else 0.0)
            + min(relationship_support * 1.4, 5.6)
            + normalized_popularity(primary_artist) * 2.0
            + (1.5 if authority_supported else 0.0)
            + min(catalog_items, 8) * 0.15
            + (2.5 if explicit_artist else 0.0)
        )
        evidence = ["exact_name"] if name_match >= 0.98 else ["name_match"]
        if stable_id:
            evidence.append("provider_artist_identity")
        if relationship_support:
            evidence.append("provider_catalog_credit")
        if authority_supported:
            evidence.append("source_authority")
        if name_match >= 0.72 and stable_id and artist_supported:
            candidates.append(
                {
                    "entity_type": "artist",
                    "item": dict(primary_artist),
                    "lead_artist": dict(primary_artist),
                    "containing_album": {},
                    "target_identity": f"provider:artist:{stable_id.casefold()}",
                    "score": score,
                    "popularity": normalized_popularity(primary_artist),
                    "exact_name": name_match >= 0.98,
                    "confidence_tier": "corroborated",
                    "evidence": evidence,
                    "relationship_support": relationship_support,
                    "authority_supported": authority_supported,
                    "canonical_artist_supported": canonical_artist_supported,
                    "artist_established": artist_established,
                }
            )

    if primary_album:
        title = trim_text(primary_album.get("title") or primary_album.get("name"))
        artist_name = trim_text(
            primary_album.get("artist")
            or primary_album.get("artist_name")
            or primary_album.get("channel")
        )
        title_match = search_text_similarity(query, title)
        stable_id = _album_provider_identity(primary_album)
        album_key = canonical_album_identity(primary_album)
        relationship_support = sum(
            1
            for track in tracks or []
            if isinstance(track, dict)
            and (
                (
                    stable_id
                    and stable_id
                    == trim_text(
                        track.get("album_id")
                        or track.get("albumId")
                        or track.get("browseId")
                    )
                )
                or (
                    album_key
                    and album_key
                    == canonical_album_identity(
                        {
                            "title": track.get("album") or track.get("album_title"),
                            "artist": track.get("channel") or track.get("artist"),
                        }
                    )
                )
            )
        )
        score = (
            title_match * 4.0
            + (2.0 if title_match >= 0.98 else 0.0)
            + (1.0 if stable_id else 0.0)
            + (0.5 if artist_name else 0.0)
            + min(relationship_support * 1.4, 4.2)
            + normalized_popularity(primary_album)
            + (2.5 if explicit_album else 0.0)
        )
        evidence = ["exact_title"] if title_match >= 0.98 else ["title_match"]
        if stable_id:
            evidence.append("provider_album_identity")
        if relationship_support:
            evidence.append("album_track_relationship")
        if title_match >= 0.78 and stable_id and artist_name:
            candidates.append(
                {
                    "entity_type": "album",
                    "item": dict(primary_album),
                    "lead_artist": _artist_from_album(primary_album, artists),
                    "containing_album": dict(primary_album),
                    "target_identity": f"provider:album:{stable_id.casefold()}",
                    "score": score,
                    "confidence_tier": (
                        "corroborated" if relationship_support else "supported"
                    ),
                    "evidence": evidence,
                    "relationship_support": float(relationship_support),
                }
            )

    normalized_type_hint = trim_text(entity_type_hint).casefold()
    if normalized_type_hint in {"track", "artist", "album"}:
        # Provider-only classification is a routing hint, not a final verdict.
        # Filtering here made the first completed provider branch irreversible:
        # later canonical artist/catalog evidence could not challenge an early
        # same-title track.  Keep a small deterministic preference while the
        # final resolver compares all entity types together.
        for candidate in candidates:
            if candidate.get("entity_type") != normalized_type_hint:
                continue
            candidate["score"] = float(candidate.get("score") or 0.0) + 0.25
            candidate["evidence"] = [
                *list(candidate.get("evidence") or []),
                "provisional_provider_type",
            ]
    if not candidates or search_mode == "taste":
        return empty
    # An exact, established artist should beat an obscure homonymous recording
    # when multiple provider catalog credits support that artist.  This is a
    # generic evidence comparison, not an artist/title exception.
    track_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("entity_type") == "track"
        ),
        None,
    )
    artist_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("entity_type") == "artist"
            and candidate.get("exact_name")
        ),
        None,
    )
    if track_candidate is not None and artist_candidate is not None:
        canonical_track_artist = normalize_artist_name(
            (canonical_resolution or {}).get("artist")
        )
        candidate_artist_name = normalize_artist_name(
            (artist_candidate.get("item") or {}).get("name")
            or (artist_candidate.get("item") or {}).get("artist")
        )
        independent_canonical_track = bool(
            (canonical_resolution or {}).get("independent_provider_corroboration")
            and trim_text(
                (canonical_resolution or {}).get("musicbrainz_recording_id")
            )
        )
        # Repeated same-name uploads can make an obscure artist look
        # relationship-supported. They must not override an independently
        # canonicalized recording credited to a different artist. Established
        # artists remain eligible through authority, canonical identity or
        # meaningful catalog popularity.
        track_credit_name = normalize_artist_name(
            (track_candidate.get("lead_artist") or {}).get("name")
            or (track_candidate.get("item") or {}).get("channel")
            or (track_candidate.get("item") or {}).get("artist")
        )
        recording_evidence_supported = bool(
            independent_canonical_track
            or track_candidate.get("structurally_supported_lead")
        )
        weak_namesake_artist = bool(
            recording_evidence_supported
            and track_credit_name
            and candidate_artist_name
            and candidate_artist_name != track_credit_name
            and not bool(artist_candidate.get("artist_established"))
        )
        if weak_namesake_artist:
            artist_candidate["score"] = float(
                artist_candidate.get("score") or 0.0
            ) - 5.0
            artist_candidate["evidence"] = [
                *list(artist_candidate.get("evidence") or []),
                "weak_namesake_artist",
            ]
        popularity_delta = float(artist_candidate.get("popularity") or 0.0) - float(
            track_candidate.get("popularity") or 0.0
        )
        if popularity_delta >= 0.12 and not explicit_track:
            artist_candidate["score"] = float(
                artist_candidate.get("score") or 0.0
            ) + min(popularity_delta * 8.0, 4.5)
            artist_candidate["evidence"] = [
                *list(artist_candidate.get("evidence") or []),
                "catalog_popularity_advantage",
            ]

    if track_candidate is not None and track_candidate.get(
        "structurally_supported_lead"
    ) and not explicit_album:
        track_album = dict(track_candidate.get("containing_album") or {})
        track_album_id = _album_provider_identity(track_album).casefold()
        track_album_title = trim_text(
            track_album.get("title")
            or track_album.get("name")
            or (track_candidate.get("item") or {}).get("album")
            or (track_candidate.get("item") or {}).get("album_title")
        )
        track_artist_name = normalize_artist_name(
            (track_candidate.get("lead_artist") or {}).get("name")
            or (track_candidate.get("item") or {}).get("channel")
            or (track_candidate.get("item") or {}).get("artist")
        )
        companion_albums: List[Dict[str, Any]] = []
        for candidate in candidates:
            if candidate.get("entity_type") != "album":
                continue
            album_item = dict(candidate.get("item") or {})
            album_id = _album_provider_identity(album_item).casefold()
            album_artist_name = normalize_artist_name(
                album_item.get("artist")
                or album_item.get("artist_name")
                or album_item.get("channel")
            )
            same_artist = bool(
                track_artist_name
                and album_artist_name
                and track_artist_name == album_artist_name
            )
            same_album = bool(
                track_album_id
                and album_id
                and track_album_id == album_id
            ) or bool(
                track_album_title
                and search_text_similarity(
                    track_album_title,
                    trim_text(album_item.get("title") or album_item.get("name")),
                )
                >= 0.96
            )
            if same_artist and same_album:
                candidate["recording_family_companion"] = True
                companion_albums.append(candidate)
        if companion_albums:
            track_candidate["score"] = max(
                float(track_candidate.get("score") or 0.0),
                max(float(album.get("score") or 0.0) for album in companion_albums)
                + 0.2,
            )
            track_candidate["evidence"] = [
                *list(track_candidate.get("evidence") or []),
                "containing_album_relationship",
            ]
    candidates.sort(
        key=lambda candidate: (
            float(candidate.get("score") or 0.0),
            candidate.get("entity_type") or "",
            candidate.get("target_identity") or "",
        ),
        reverse=True,
    )
    winner = dict(candidates[0])
    conflicting_candidates = [
        candidate
        for candidate in candidates[1:]
        if not (
            winner.get("entity_type") == "track"
            and candidate.get("recording_family_companion") is True
        )
    ]
    runner_score = (
        float(conflicting_candidates[0].get("score") or 0.0)
        if conflicting_candidates
        else 0.0
    )
    margin = float(winner.get("score") or 0.0) - runner_score
    tier = str(winner.get("confidence_tier") or "supported")
    clear_winner = (
        len(candidates) == 1
        or (tier == "authoritative" and margin >= 0.15)
        or margin >= 0.65
        or (
            winner.get("entity_type") == "artist"
            and float(winner.get("relationship_support") or 0.0) >= 1.75
            and margin >= 0.25
        )
        or (
            winner.get("entity_type") == "artist"
            and winner.get("artist_established") is True
            and "catalog_popularity_advantage"
            in set(winner.get("evidence") or [])
            and margin >= 0.25
        )
        or (
            winner.get("entity_type") == "album"
            and explicit_album
            and margin >= 0.25
        )
    )
    if not clear_winner:
        ambiguous = dict(empty)
        ambiguous["decision_margin"] = round(margin, 4)
        ambiguous["evidence"] = ["competing_authoritative_entities"]
        ambiguous["candidate_scores"] = [
            {
                "entity_type": candidate.get("entity_type"),
                "target_identity": candidate.get("target_identity"),
                "score": round(float(candidate.get("score") or 0.0), 4),
            }
            for candidate in candidates[:3]
        ]
        return ambiguous

    score = float(winner.pop("score") or 0.0)
    tier_bonus = 0.10 if tier == "authoritative" else 0.05
    intent_confidence = round(
        min(0.99, 0.55 + min(score / 40.0, 0.25) + min(margin / 10.0, 0.14) + tier_bonus),
        4,
    )
    winner["confidence"] = intent_confidence
    winner["intent_confidence"] = intent_confidence
    winner["identity_confidence"] = {
        "authoritative": 0.99,
        "corroborated": 0.90,
        "supported": 0.78,
    }.get(tier, 0.65)
    winner["decision_margin"] = round(margin, 4)
    winner["resolver"] = "classify_then_resolve_v2"
    winner["candidate_scores"] = [
        {
            "entity_type": candidate.get("entity_type"),
            "target_identity": candidate.get("target_identity"),
            "score": round(float(candidate.get("score") or 0.0), 4),
        }
        for candidate in candidates[:3]
    ]
    return winner


def classify_query_intent(
    *,
    query: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
    server=None,
) -> str:
    """Compatibility facade; production consumes the full target bundle."""
    return str(
        resolve_search_target(
            server=server,
            query=query,
            tracks=tracks,
            artists=artists,
            albums=albums,
        ).get("entity_type")
        or "mixed"
    )


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
    provider_tracks: Iterable[Dict[str, Any]],
    fuzzy_tracks: Iterable[Dict[str, Any]],
    musicbrainz_tracks: Iterable[Dict[str, Any]],
    provider_artists: Iterable[Dict[str, Any]] = (),
    provider_albums: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    providers = [
        dict(item)
        for item in list(provider_tracks or [])
        if isinstance(item, dict)
        and trim_text(item.get("title") or item.get("name"))
        and trim_text(item.get("channel") or item.get("artist"))
    ]
    recording_families = _provider_recording_families(
        query,
        providers,
        artists=provider_artists,
        albums=provider_albums,
    )
    family_by_artist = {
        str(family.get("artist_key") or ""): family
        for family in recording_families
    }
    candidates: List[Dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    for raw_item in [*list(fuzzy_tracks or []), *list(musicbrainz_tracks or [])]:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        credit_key = canonical_title_artist_identity(item)
        if not credit_key:
            continue
        candidate_id = trim_text(
            item.get("musicbrainz_recording_id")
            or item.get("recording_mbid")
            or item.get("canonical_track_identity")
            or credit_key
        ).casefold()
        if candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate_id)
        candidates.append(item)

    def date_preference(value: Any) -> float:
        digits = "".join(
            character for character in trim_text(value) if character.isdigit()
        )
        if not digits:
            return -99999999.0
        return -float((digits + "99999999")[:8])

    def release_candidates(item: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            dict(value)
            for value in list(item.get("musicbrainz_release_candidates") or [])
            if isinstance(value, dict)
        ]

    def best_release(
        item: Dict[str, Any],
        provider_track: Dict[str, Any],
    ) -> Dict[str, Any]:
        provider_album = trim_text(
            provider_track.get("album") or provider_track.get("album_title")
        )
        return max(
            release_candidates(item),
            key=lambda value: (
                search_text_similarity(
                    provider_album,
                    trim_text(value.get("album")),
                )
                if provider_album
                else 0.0,
                trim_text(value.get("status")).casefold() in {"", "official"},
                trim_text(value.get("primary_type")).casefold() in {"", "album"},
                not {
                    trim_text(entry).casefold()
                    for entry in list(value.get("secondary_types") or [])
                }.intersection({"live", "compilation", "remix"}),
                date_preference(value.get("release_date")),
            ),
            default={},
        )

    ranked_by_credit: Dict[
        str,
        tuple[
            tuple[float, ...],
            tuple[float, float, Dict[str, Any], Dict[str, Any]],
        ],
    ] = {}
    for item in candidates:
        title = trim_text(item.get("title") or item.get("name"))
        artist = trim_text(item.get("channel") or item.get("artist"))
        if not title or not artist:
            continue
        title_similarity = search_text_similarity(query, title)
        if title_similarity < 0.78:
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
        best_provider: Dict[str, Any] = {}
        best_pair = 0.0
        best_provider_support = 0.0
        family = family_by_artist.get(normalize_artist_name(artist), {})
        for index, provider_track in enumerate(providers):
            provider_title = trim_text(
                provider_track.get("title") or provider_track.get("name")
            )
            provider_artist = trim_text(
                provider_track.get("channel") or provider_track.get("artist")
            )
            provider_title_match = search_text_similarity(title, provider_title)
            if provider_title_match < 0.90:
                continue
            provider_artist_match = search_text_similarity(
                artist,
                provider_artist,
            )
            pair_match = (
                provider_title_match * 0.56
                + provider_artist_match * 0.44
            )
            authority = trim_text(
                provider_track.get("source_authority")
                or provider_track.get("authority")
            ).casefold()
            authority_score = (
                1.0
                if authority
                in {
                    "official",
                    "official_artist_channel",
                    "topic",
                    "vevo",
                    "verified_catalog",
                    "ytmusic_artist_detail",
                }
                else 0.35
                if trim_text(
                    provider_track.get("artist_id")
                    or provider_track.get("artistId")
                    or provider_track.get("channel_id")
                )
                else 0.0
            )
            rank_score = max(0.0, 1.0 - (index / max(len(providers), 1)))
            support = (
                pair_match * 4.0
                + float(family.get("intent_score") or 0.0) * 1.25
                + authority_score * 0.35
                + rank_score * 0.10
            )
            if support > best_provider_support:
                best_provider = provider_track
                best_pair = pair_match
                best_provider_support = support
        independently_corroborated = bool(best_provider) and best_pair >= 0.90
        score = (
            title_similarity * 4.0
            + max(0.0, min(provider_confidence, 1.0))
            + best_provider_support
            - (0.0 if independently_corroborated else 3.0)
        )
        selected_release = best_release(item, best_provider)
        provider_album = trim_text(
            best_provider.get("album") or best_provider.get("album_title")
        )
        release_album_match = (
            search_text_similarity(
                provider_album,
                trim_text(selected_release.get("album")),
            )
            if provider_album and selected_release
            else 0.0
        )
        provider_duration = float(
            best_provider.get("duration")
            or best_provider.get("duration_seconds")
            or 0.0
        )
        canonical_duration = float(item.get("duration") or 0.0)
        duration_match = (
            max(0.0, 1.0 - abs(provider_duration - canonical_duration) / 18.0)
            if provider_duration > 0.0 and canonical_duration > 0.0
            else 0.0
        )
        item_secondary_types = {
            trim_text(value).casefold()
            for value in list(item.get("release_secondary_types") or [])
        }
        recording_quality = (
            title_similarity,
            best_pair,
            release_album_match,
            duration_match,
            0.0 if bool((item.get("raw_musicbrainz") or {}).get("video")) else 1.0,
            1.0
            if trim_text(item.get("release_status")).casefold() in {"", "official"}
            else 0.0,
            1.0
            if not item_secondary_types.intersection({"live", "compilation", "remix"})
            else 0.0,
            1.0 if trim_text(item.get("musicbrainz_recording_id")) else 0.0,
            date_preference(
                item.get("first_release_date") or item.get("release_date")
            ),
            provider_confidence,
        )
        credit_key = canonical_title_artist_identity(item)
        ranked_entry = (score, best_pair, item, best_provider)
        previous = ranked_by_credit.get(credit_key)
        if previous is None or recording_quality > previous[0]:
            ranked_by_credit[credit_key] = (recording_quality, ranked_entry)
    ranked = [value[1] for value in ranked_by_credit.values()]
    if not ranked:
        return {}
    corroborated = [entry for entry in ranked if entry[1] >= 0.90]
    if not corroborated:
        return {}
    corroborated.sort(key=lambda entry: entry[0], reverse=True)
    score, pair_match, item, provider_track = corroborated[0]
    runner_score = corroborated[1][0] if len(corroborated) > 1 else 0.0
    margin = score - runner_score
    if len(corroborated) > 1 and margin < 0.30:
        return {
            "ambiguous": True,
            "reason": "competing_recording_credits",
            "decision_margin": round(margin, 4),
            "candidate_credits": [
                {
                    "title": trim_text(entry[2].get("title") or entry[2].get("name")),
                    "artist": trim_text(
                        entry[2].get("channel") or entry[2].get("artist")
                    ),
                    "provider_source_id": trim_text(
                        entry[3].get("videoId")
                        or entry[3].get("video_id")
                        or entry[3].get("id")
                    ),
                }
                for entry in corroborated[:3]
            ],
        }
    confidence = min(
        0.97,
        0.72 + min(pair_match * 0.18, 0.18) + min(margin / 20.0, 0.07),
    )
    selected_release = best_release(item, provider_track)
    selected_family = family_by_artist.get(
        normalize_artist_name(item.get("channel") or item.get("artist")),
        {},
    )
    return {
        "title": trim_text(item.get("title") or item.get("name")),
        "artist": trim_text(item.get("channel") or item.get("artist")),
        "album": trim_text(
            selected_release.get("album")
            or item.get("album")
            or item.get("album_title")
        ),
        "confidence": round(confidence, 4),
        "source": trim_text(
            item.get("source_provider")
            or item.get("metadata_source")
            or "catalog"
        ),
        "musicbrainz_recording_id": trim_text(
            item.get("musicbrainz_recording_id")
        ),
        "musicbrainz_artist_id": trim_text(item.get("musicbrainz_artist_id")),
        "musicbrainz_artist_ids": [
            trim_text(value)
            for value in list(item.get("musicbrainz_artist_ids") or [])
            if trim_text(value)
        ],
        "musicbrainz_release_id": trim_text(
            selected_release.get("release_id")
            or item.get("musicbrainz_release_id")
        ),
        "musicbrainz_release_group_id": trim_text(
            selected_release.get("release_group_id")
            or item.get("musicbrainz_release_group_id")
        ),
        "musicbrainz_release_candidates": [
            dict(value)
            for value in list(item.get("musicbrainz_release_candidates") or [])
            if isinstance(value, dict)
        ],
        "release_date": trim_text(
            selected_release.get("release_date") or item.get("release_date")
        ),
        "release_year": trim_text(
            selected_release.get("release_year")
            or item.get("release_year")
            or item.get("year")
        ),
        "country": trim_text(
            selected_release.get("country") or item.get("country")
        ),
        "independent_provider_corroboration": True,
        "recording_family_count": len(recording_families),
        "recording_family_score": round(
            float(selected_family.get("intent_score") or 0.0), 4
        ),
        "recording_family_margin": round(margin, 4),
        "provider_source_id": trim_text(
            provider_track.get("videoId")
            or provider_track.get("video_id")
            or provider_track.get("id")
        ),
        "decision_margin": round(margin, 4),
    }


def _best_artist_match(
    query: str,
    artists: Iterable[Dict[str, Any]],
    tracks: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    valid = _coalesce_linked_artist_candidates(artists)
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
        "provider_album_id": album_id,
        "title": title,
        "artist": artist,
        "thumbnail": track.get("thumbnail"),
        "year": track.get("year") or "",
    }


def _bind_track_containing_album(
    track: Dict[str, Any],
    *,
    albums: Iterable[Dict[str, Any]],
    canonical_resolution: Dict[str, Any],
) -> Dict[str, Any]:
    """Bind canonical release evidence to a usable provider album identity."""
    canonical = dict(canonical_resolution or {})
    track_album = _album_from_track(track)
    track_album_id = _album_provider_identity(track_album).casefold()
    expected_titles = [
        trim_text(track_album.get("title")),
        trim_text(canonical.get("album")),
    ]
    expected_titles = [title for title in expected_titles if title]
    expected_artist = trim_text(
        canonical.get("artist")
        or track.get("channel")
        or track.get("artist")
    )

    def score(raw_album: Dict[str, Any]) -> tuple[float, float, float]:
        album = dict(raw_album or {})
        provider_id = _album_provider_identity(album).casefold()
        title = trim_text(album.get("title") or album.get("name"))
        artist = trim_text(
            album.get("artist")
            or album.get("artist_name")
            or album.get("channel")
        )
        title_match = max(
            (search_text_similarity(expected, title) for expected in expected_titles),
            default=0.0,
        )
        artist_match = (
            search_text_similarity(expected_artist, artist)
            if expected_artist and artist
            else 0.0
        )
        exact_provider = bool(
            track_album_id and provider_id and track_album_id == provider_id
        )
        accepted = exact_provider or (
            bool(provider_id) and title_match >= 0.90 and artist_match >= 0.72
        )
        return (
            1.0 if accepted else 0.0,
            2.0 if exact_provider else title_match + artist_match,
            normalized_popularity(album),
        )

    provider_albums = [
        dict(album)
        for album in albums or []
        if isinstance(album, dict) and _album_provider_identity(album)
    ]
    matched_album = max(provider_albums, key=score) if provider_albums else {}
    if matched_album and score(matched_album)[0] <= 0.0:
        matched_album = {}
    bound = {**track_album, **matched_album}
    provider_album_id = _album_provider_identity(bound)
    if not provider_album_id:
        # Preserve the accepted canonical release as an internal descriptor.
        # It is not publishable or playable yet, but later artist-catalog
        # hydration can bind it to a real provider album instead of losing the
        # original-album relationship entirely.
        release_group_id = trim_text(canonical.get("musicbrainz_release_group_id"))
        release_id = trim_text(canonical.get("musicbrainz_release_id"))
        canonical_album = trim_text(
            canonical.get("album") or track_album.get("title")
        )
        if not canonical_album:
            return {}
        return {
            "id": (
                f"musicbrainz:release-group:{release_group_id}"
                if release_group_id
                else (
                    f"musicbrainz:release:{release_id}"
                    if release_id
                    else ""
                )
            ),
            "title": canonical_album,
            "artist": expected_artist,
            "musicbrainz_release_group_id": release_group_id,
            "musicbrainz_release_id": release_id,
            "release_date": canonical.get("release_date") or "",
            "year": canonical.get("release_year") or "",
            "playable": False,
        }
    bound["id"] = provider_album_id
    bound["provider_album_id"] = provider_album_id
    release_group_id = trim_text(canonical.get("musicbrainz_release_group_id"))
    release_id = trim_text(canonical.get("musicbrainz_release_id"))
    if release_group_id:
        bound["musicbrainz_release_group_id"] = release_group_id
    if release_id:
        bound["musicbrainz_release_id"] = release_id
    for source_key, target_key in (
        ("release_date", "release_date"),
        ("release_year", "year"),
        ("country", "country"),
    ):
        value = canonical.get(source_key)
        if value not in (None, "") and not bound.get(target_key):
            bound[target_key] = value
    return bound


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
    expansion_started_at = time.perf_counter()
    artist_resolution_started_at = expansion_started_at
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
    artist_resolution_ms = int(
        (time.perf_counter() - artist_resolution_started_at) * 1000
    )
    related_artists = list(details.get("related_artists") or [])
    albums = list(details.get("albums") or [])
    local_catalog_started_at = time.perf_counter()
    catalog_tracks = catalog_playable_tracks_for_artist(
        getattr(server, "raw", server),
        user_scope_id=user_scope_id or "guest",
        artist=details.get("name") or resolved_artist_name,
        limit=max(limit, 24),
    )
    local_catalog_ms = int(
        (time.perf_counter() - local_catalog_started_at) * 1000
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
    all_album_ids = [
        trim_text(
            album.get("id")
            or album.get("album_id")
            or album.get("browseId")
        )
        for album in albums
    ]
    all_album_ids = [
        album_id
        for index, album_id in enumerate(all_album_ids)
        if album_id and album_id not in all_album_ids[:index]
    ]
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
    missing_artwork_ids = [
        album_id
        for album_id in all_album_ids
        if not catalog_thumbnail_url(
            albums_by_id.get(album_id) or {},
            entity_type="album",
        )
    ]
    # Complete at most the visible album page, prioritizing records whose
    # existing artist payload has no cover. This reuses the album-detail calls
    # already needed for playable tracklists instead of adding another lookup
    # path.
    album_ids = [
        *missing_artwork_ids,
        *[
            album_id
            for album_id in all_album_ids
            if album_id not in missing_artwork_ids
        ],
    ][:16]
    pending_artwork_ids = set(missing_artwork_ids) & set(album_ids)
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
    album_tracklists_started_at = time.perf_counter()
    for batch_start in range(0, len(album_ids), 4):
        if (
            len(unique_tracks) >= target_track_count
            and not pending_artwork_ids
        ):
            break
        batch_ids = album_ids[batch_start : batch_start + 4]
        future_pairs = [
            (album_id, executor.submit(load_album_tracklist, album_id))
            for album_id in batch_ids
        ]
        album_tracklists_attempted += len(future_pairs)
        for _album_id, future in future_pairs:
            pending_artwork_ids.discard(_album_id)
            try:
                album_payload = dict(future.result() or {})
            except Exception:
                continue
            album_record = {
                **dict(albums_by_id.get(_album_id) or {}),
                **{
                    key: value
                    for key, value in album_payload.items()
                    if value not in (None, "", [], {})
                },
            }
            albums_by_id[_album_id] = album_record
            album_tracks = list(album_payload.get("tracks") or [])
            if not album_tracks:
                continue
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
    album_tracklists_ms = int(
        (time.perf_counter() - album_tracklists_started_at) * 1000
    )

    albums = [
        dict(
            albums_by_id.get(
                trim_text(
                    album.get("id")
                    or album.get("album_id")
                    or album.get("browseId")
                ),
                album,
            )
        )
        for album in albums
        if isinstance(album, dict)
    ]
    track_catalog_complete = (
        len(unique_tracks) >= target_track_count
        or (
            bool(album_ids)
            and album_tracklists_attempted >= len(album_ids)
            and album_tracklists_loaded >= len(album_ids)
        )
        or (not album_ids and len(unique_tracks) >= 8)
    )
    artwork_target = min(4, len(albums))
    artwork_count = sum(
        1
        for album in albums
        if catalog_thumbnail_url(album, entity_type="album")
    )
    artwork_completion_exhausted = (
        not album_ids or album_tracklists_attempted >= len(album_ids)
    )
    album_artwork_complete = (
        artwork_count >= artwork_target or artwork_completion_exhausted
    )
    catalog_complete = (
        bool(details)
        and track_catalog_complete
        and album_artwork_complete
    )

    return {
        "artist": artist_entity,
        "tracks": unique_tracks,
        "albums": albums,
        "related_artists": related_artists,
        "catalog_status": "complete" if catalog_complete else "retryable",
        "album_tracklists_loaded": album_tracklists_loaded,
        "album_tracklists_attempted": album_tracklists_attempted,
        "stage_timings_ms": {
            "artist_resolution": artist_resolution_ms,
            "local_catalog": local_catalog_ms,
            "album_tracklists": album_tracklists_ms,
            "total": int((time.perf_counter() - expansion_started_at) * 1000),
        },
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
            "classify-resolve-v2",
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


def _provider_recording_decision(
    query: str,
    tracks: Iterable[Dict[str, Any]],
    *,
    artists: Iterable[Dict[str, Any]] = (),
    albums: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Choose a provider recording after comparing all exact-title credits."""
    ranked = _provider_recording_families(
        query,
        tracks,
        artists=artists,
        albums=albums,
    )
    if not ranked:
        return {
            "decisive": False,
            "track": {},
            "artist": "",
            "credit_count": 0,
            "reason": "no_exact_provider_recording",
        }
    leader = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    leader_track = dict(leader["best_track"])
    leader_views = float(leader["views"])
    runner_views = float(runner["views"]) if runner else 0.0
    view_ratio = (
        leader_views / runner_views
        if leader_views > 0.0 and runner_views > 0.0
        else float("inf")
        if leader_views > 0.0
        else 0.0
    )
    stable_artist_id = trim_text(
        leader_track.get("artist_id")
        or leader_track.get("artistId")
        or leader_track.get("channel_id")
    )
    stable_album_id = trim_text(
        leader_track.get("album_id")
        or leader_track.get("albumId")
        or leader_track.get("browseId")
    )
    leader_authority = trim_text(
        leader_track.get("source_authority")
        or leader_track.get("authority")
    ).casefold()
    authority_supported = leader_authority in {
        "official",
        "official_artist_channel",
        "topic",
        "vevo",
        "verified_catalog",
        "ytmusic_artist_detail",
    }
    structurally_supported = bool(
        int(leader["exact_count"]) >= 1
        and stable_artist_id
        and stable_album_id
        and _track_provider_identity(leader_track)
    )
    leader_score = float(leader.get("intent_score") or 0.0)
    runner_score = float(runner.get("intent_score") or 0.0) if runner else 0.0
    decision_margin = leader_score - runner_score
    decisive = bool(
        structurally_supported
        and (
            runner is None
            or decision_margin >= 0.65
            or (decision_margin >= 0.35 and view_ratio >= 3.0)
        )
    )
    return {
        "decisive": decisive,
        "track": leader_track,
        "artist": trim_text(leader["artist"]),
        "artist_key": trim_text(leader["artist_key"]),
        "credit_count": len(ranked),
        "exact_count": int(leader["exact_count"]),
        "view_ratio": round(view_ratio, 4) if view_ratio != float("inf") else None,
        "structurally_supported": structurally_supported,
        "authority_supported": authority_supported,
        "artist_id": stable_artist_id,
        "album_id": stable_album_id,
        "best_index": int(leader["best_index"]),
        "intent_score": round(leader_score, 4),
        "decision_margin": round(decision_margin, 4),
        "family_scores": [
            {
                "artist": trim_text(group.get("artist")),
                "score": round(float(group.get("intent_score") or 0.0), 4),
                "exact_count": int(group.get("exact_count") or 0),
                "catalog_credit_count": int(group.get("catalog_credit_count") or 0),
                "album_relationship_score": float(
                    group.get("album_relationship_score") or 0.0
                ),
                "best_index": int(group.get("best_index") or 0),
            }
            for group in ranked[:5]
        ],
        "reason": (
            "recording_family_comparison"
            if decisive
            else "recording_families_too_close"
        ),
    }


def _provider_multi_branch_corroboration(
    decision: Dict[str, Any],
    *,
    artists: Iterable[Dict[str, Any]] = (),
    albums: Iterable[Dict[str, Any]] = (),
) -> bool:
    """Require independent artist/album branch agreement before fast grace."""
    track = dict(decision.get("track") or {})
    artist_id = trim_text(decision.get("artist_id"))
    artist_name = normalize_artist_name(decision.get("artist"))
    if not artist_id or not _track_provider_identity(track) or not artist_name:
        return False
    artist_items = [item for item in artists if isinstance(item, dict)]
    album_items = [item for item in albums if isinstance(item, dict)]
    artist_match = any(
        isinstance(item, dict)
        and (
            (item_id := trim_text(item.get("id") or item.get("artist_id") or item.get("browseId")))
            and item_id == artist_id
            or (
                not item_id
                and normalize_artist_name(item.get("name") or item.get("artist")) == artist_name
            )
        )
        for item in artist_items
    )
    album_id = trim_text(decision.get("album_id"))
    album_name = normalize_track_title(track.get("album") or track.get("album_name"))
    # A selected track with an album relationship requires an actual album
    # branch match; an absent branch is unknown, never corroboration.
    album_match = True
    if album_id or album_name:
        album_match = any(
            (
                (item_id := trim_text(item.get("id") or item.get("album_id") or item.get("browseId")))
                and album_id
                and item_id == album_id
            )
            or (
                not item_id
                and album_name
                and normalize_track_title(item.get("title") or item.get("name")) == album_name
                and normalize_artist_name(item.get("artist") or item.get("artist_name")) == artist_name
            )
            for item in album_items
        )
    margin = float(decision.get("decision_margin") or 0.0)
    return bool(artist_match and album_match and margin >= 0.65 and not decision.get("conflict"))


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
    removed_untrusted_aliases += remove_unconfirmed_display_memories(
        getattr(server, "raw", server),
        query=query,
    )
    cache_key = ""
    if not bool(getattr(legacy_req, "force_refresh", False)):
        cache_key = f"fast::{_retrieval_cache_key(legacy_req, profile, limit, server=server)}"
        if not removed_untrusted_aliases:
            cached_payload = _retrieval_cache_get(cache_key)
            cached_target = dict(
                (cached_payload or {}).get("resolved_target") or {}
            )
            cached_target_evidence = {
                str(value or "").strip().casefold()
                for value in list(cached_target.get("evidence") or [])
            }
            legacy_track_target = bool(
                str(cached_target.get("entity_type") or "").casefold() == "track"
                and "recording_family_comparison" not in cached_target_evidence
            )
            if (
                cached_payload is not None
                and not legacy_track_target
                and (
                    search_mode == "taste"
                    or bool(cached_target.get("target_identity"))
                )
            ):
                diagnostics = dict(cached_payload.get("retrieval_diagnostics") or {})
                diagnostics["cache_hit"] = True
                cached_payload["retrieval_diagnostics"] = diagnostics
                return cached_payload

    local_index_started_at = time.perf_counter()
    exact_memories: List[Dict[str, Any]] = []
    for exact_entity_type in ("artist", "track", "album"):
        exact_memories.extend(
            load_catalog_entity_memories(
                server,
                query=query,
                entity_type=exact_entity_type,
                limit=8,
            )
        )
    combined_memories = [
        *exact_memories,
        *load_fuzzy_catalog_entity_memories(
            server,
            query=query,
            limit=max(limit, 12),
        ),
    ]
    # Exact and fuzzy loaders can return the same canonical record.  Counting
    # it twice would manufacture relationship evidence and make resolution
    # dependent on loader order.
    memory_by_identity: Dict[tuple[str, str], Dict[str, Any]] = {}
    for memory in combined_memories:
        if not isinstance(memory, dict):
            continue
        identity = (
            str(memory.get("entity_type") or ""),
            str(memory.get("entity_key") or ""),
        )
        previous = memory_by_identity.get(identity)
        if previous is None or float(memory.get("score") or 0.0) > float(
            previous.get("score") or 0.0
        ):
            memory_by_identity[identity] = memory
    fuzzy_memories = list(memory_by_identity.values())
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
    # Once the local index identifies an exact artist name, load relationship
    # evidence through that artist's catalog instead of asking a broad text
    # query to rediscover the same relationship.  This is local persisted data
    # and does not add a provider call to /search.
    catalog_relationship_tracks: List[Dict[str, Any]] = []
    exact_artist_names: List[str] = []
    for artist in fuzzy_artists:
        artist_name = trim_text(artist.get("name") or artist.get("artist"))
        if (
            not artist_name
            or _raw_artist_name(query) != _raw_artist_name(artist_name)
            or artist_name in exact_artist_names
        ):
            continue
        exact_artist_names.append(artist_name)
        catalog_relationship_tracks.extend(
            catalog_playable_tracks_for_artist(
                server,
                user_scope_id=str(profile.get("user_scope_id") or ""),
                artist=artist_name,
                limit=12,
            )
        )
        if len(exact_artist_names) >= 3:
            break
    fuzzy_track_identities = {
        _track_provider_identity(track).casefold()
        or (
            f"{normalize_track_title(track.get('title') or track.get('name'))}|"
            f"{normalize_artist_name(track.get('channel') or track.get('artist'))}"
        )
        for track in fuzzy_tracks
        if isinstance(track, dict)
    }
    for track in catalog_relationship_tracks:
        identity = _track_provider_identity(track).casefold() or (
            f"{normalize_track_title(track.get('title') or track.get('name'))}|"
            f"{normalize_artist_name(track.get('channel') or track.get('artist'))}"
        )
        if not identity or identity in fuzzy_track_identities:
            continue
        fuzzy_track_identities.add(identity)
        fuzzy_tracks.append(track)
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
    stage_timings_ms: Dict[str, int] = {}
    first_track_evidence_ms: int | None = None
    acceptance_barrier_ms: int | None = None

    fast_track_future = executor.submit(
        search_tracks_direct,
        query,
        max(limit * 2, 18),
        server=server,
    )
    fast_artist_future = None
    fast_album_future = None
    if complete_first_page:
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
    canonical_evidence_started_ms: int | None = None
    canonical_evidence_completed_ms: int | None = None
    canonical_evidence_outcome = "not_started"
    canonical_evidence_warranted = False
    if search_mode != "taste":
        # This lookup is independent of the provider's artist guess. Start it
        # beside the typed provider branches so a slow track lookup cannot
        # starve canonical resolution. Its result stays quarantined until the
        # provider-only classifier proves the query represents a recording.
        canonical_evidence_future = executor.submit(
            search_musicbrainz_recording_items,
            query,
            artist="",
            official_non_live=True,
            raise_errors=True,
            limit=25,
        )
        canonical_evidence_started_ms = int(
            (time.perf_counter() - retrieval_started_at) * 1000
        )
        canonical_evidence_outcome = "speculative"
    provider_recording_decision: Dict[str, Any] = {}

    def start_canonical_evidence_if_warranted(
        tracks: Iterable[Dict[str, Any]],
    ) -> None:
        nonlocal canonical_evidence_future
        nonlocal canonical_evidence_started_ms
        nonlocal canonical_evidence_outcome
        nonlocal canonical_evidence_warranted
        nonlocal provider_recording_decision
        provider_tracks = [
            dict(track)
            for track in tracks or []
            if isinstance(track, dict)
        ]
        provider_recording_decision = _provider_recording_decision(
            query,
            provider_tracks,
        )
        strong_recording_candidates = [
            track
            for track in provider_tracks
            if _track_provider_identity(track)
            and search_text_similarity(
                query,
                trim_text(track.get("title") or track.get("name")),
            )
            >= 0.90
        ]
        if (
            canonical_evidence_outcome == "catalog_hit"
            or search_mode == "taste"
            or not strong_recording_candidates
        ):
            return
        canonical_evidence_warranted = True
        persisted_resolution = _canonical_track_resolution(
            server,
            query=query,
            provider_tracks=provider_tracks,
            fuzzy_tracks=fuzzy_tracks,
            musicbrainz_tracks=[],
        )
        if (
            persisted_resolution.get("musicbrainz_recording_id")
            and persisted_resolution.get("independent_provider_corroboration")
        ):
            canonical_evidence_outcome = "catalog_hit"
            if canonical_evidence_future is not None:
                canonical_evidence_future.cancel()
            return
        canonical_evidence_outcome = "pending"

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
                if source_name == "tracks.fast":
                    if resolved_values[source_name] and first_track_evidence_ms is None:
                        first_track_evidence_ms = int(
                            (time.perf_counter() - retrieval_started_at) * 1000
                        )
                    # Start independent recording evidence as soon as a strong
                    # provider track exists. It remains invisible until the
                    # provider-only classifier locks the request to `track`.
                    start_canonical_evidence_if_warranted(
                        resolved_values[source_name]
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
    stage_timings_ms["provider_fanout"] = int(
        (time.perf_counter() - retrieval_started_at) * 1000
    )

    fast_tracks = list(resolved_values["tracks.fast"] or [])
    canonical_tracks: List[Dict[str, Any]] = []
    fast_artists = list(resolved_values["artists.fast"] or [])
    fast_albums = list(resolved_values["albums.fast"] or [])
    fast_playlists: List[Dict[str, Any]] = []
    musicbrainz_tracks: List[Dict[str, Any]] = []
    provider_recording_decision = _provider_recording_decision(
        query,
        fast_tracks,
        artists=[*fast_artists, *fuzzy_artists],
        albums=[*fast_albums, *fuzzy_albums],
    )
    provider_recording_decision["independent_branch_corroboration"] = _provider_multi_branch_corroboration(
        provider_recording_decision,
        artists=[*fast_artists, *fuzzy_artists],
        albums=[*fast_albums, *fuzzy_albums],
    )

    provider_classify_started_at = time.perf_counter()
    provider_only_target = resolve_search_target(
        server=server,
        query=query,
        tracks=fast_tracks[:24],
        artists=[*fast_artists, *fuzzy_artists][:16],
        albums=[*fast_albums, *fuzzy_albums][:16],
        relationship_tracks=[*fast_tracks, *fuzzy_tracks],
        search_mode=search_mode,
    )
    stage_timings_ms["provider_classification"] = int(
        (time.perf_counter() - provider_classify_started_at) * 1000
    )
    preclassified_intent = str(
        provider_only_target.get("entity_type") or "mixed"
    ).strip().lower()
    provider_target_item = dict(provider_only_target.get("item") or {})
    provider_target_artist = normalize_artist_name(
        (provider_only_target.get("lead_artist") or {}).get("name")
        or provider_target_item.get("artist")
        or provider_target_item.get("name")
    )
    cross_entity_recording_conflict = bool(
        preclassified_intent in {"artist", "album"}
        and provider_target_artist
        and any(
            search_text_similarity(
                query,
                trim_text(track.get("title") or track.get("name")),
            )
            >= 0.94
            and normalize_artist_name(track.get("channel") or track.get("artist"))
            not in {"", provider_target_artist}
            for track in fast_tracks
            if isinstance(track, dict)
        )
    )
    if (
        preclassified_intent == "mixed"
        and provider_recording_decision.get("structurally_supported")
    ):
        preclassified_intent = "track"
    if (
        preclassified_intent == "track"
        and search_mode != "taste"
        and canonical_evidence_future is None
    ):
        start_canonical_evidence_if_warranted(fast_tracks)

    canonical_wait_started_at = time.perf_counter()
    if canonical_evidence_future is not None and not canonical_evidence_warranted:
        canonical_evidence_future.cancel()
        canonical_evidence_outcome = (
            "ignored_non_track"
            if preclassified_intent in {"artist", "album"}
            else "ignored_no_exact_recording"
        )
    elif canonical_evidence_future is not None:
        elapsed_seconds = time.perf_counter() - retrieval_started_at
        remaining_seconds = max(
            _SEARCH_RETRIEVAL_TOTAL_BUDGET_SECONDS - elapsed_seconds,
            0.0,
        )
        decisive_provider = bool(
            provider_recording_decision.get("decisive")
            and provider_recording_decision.get("structurally_supported")
            and (
                provider_recording_decision.get("authority_supported")
                or provider_recording_decision.get("independent_branch_corroboration")
            )
            and not cross_entity_recording_conflict
        )
        if decisive_provider:
            remaining_seconds = min(
                remaining_seconds, _SEARCH_DECISIVE_CANONICAL_GRACE_SECONDS
            )
        if cross_entity_recording_conflict:
            remaining_seconds += _SEARCH_CANONICAL_CONFLICT_GRACE_SECONDS
        if canonical_evidence_future.done():
            canonical_done = {canonical_evidence_future}
        elif _SEARCH_DISABLE_TIMEOUTS:
            wait({canonical_evidence_future})
            canonical_done = {canonical_evidence_future}
        elif remaining_seconds > 0.0:
            canonical_done, _ = wait(
                {canonical_evidence_future},
                timeout=remaining_seconds,
            )
        else:
            canonical_done = set()
        if canonical_evidence_future in canonical_done:
            try:
                musicbrainz_tracks = list(
                    canonical_evidence_future.result() or []
                )
                completed_sources.append("canonical.musicbrainz")
                canonical_evidence_outcome = (
                    "hit" if musicbrainz_tracks else "empty"
                )
                canonical_evidence_completed_ms = int(
                    (time.perf_counter() - retrieval_started_at) * 1000
                )
            except Exception as exc:
                timed_out_sources.append("canonical.musicbrainz")
                canonical_evidence_outcome = (
                    "timeout"
                    if "timed out" in str(exc).casefold()
                    or isinstance(exc, TimeoutError)
                    else "error"
                )
        else:
            timed_out_sources.append("canonical.musicbrainz")
            canonical_evidence_future.cancel()
            canonical_evidence_outcome = "timeout"
    stage_timings_ms["canonical_evidence_wait"] = int(
        (time.perf_counter() - canonical_wait_started_at) * 1000
    )

    canonical_resolution_started_at = time.perf_counter()
    canonical_resolution = (
        _canonical_track_resolution(
            server,
            query=query,
            provider_tracks=fast_tracks,
            fuzzy_tracks=fuzzy_tracks,
            musicbrainz_tracks=musicbrainz_tracks,
            provider_artists=[*fast_artists, *fuzzy_artists],
            provider_albums=[*fast_albums, *fuzzy_albums],
        )
        if (
            preclassified_intent in {"track", "mixed"}
            or canonical_evidence_warranted
        )
        else {}
    )
    stage_timings_ms["canonical_resolution"] = int(
        (time.perf_counter() - canonical_resolution_started_at) * 1000
    )
    # A provider-only tie is allowed at the classification stage. Independent
    # recording evidence may resolve that tie, after which identity selection
    # is restricted to tracks. This is the path needed for famous recordings
    # with many covers/live versions such as Comfortably Numb.
    if (
        preclassified_intent == "mixed"
        and canonical_resolution.get("title")
        and canonical_resolution.get("independent_provider_corroboration")
    ):
        preclassified_intent = "track"
    if canonical_resolution.get("ambiguous"):
        canonical_evidence_outcome = "ambiguous"
    canonical_recording_challenge = bool(
        canonical_resolution.get("title")
        and canonical_resolution.get("artist")
        and canonical_resolution.get("independent_provider_corroboration")
    )
    if canonical_recording_challenge:
        canonical_title = trim_text(canonical_resolution.get("title"))
        canonical_artist = trim_text(canonical_resolution.get("artist"))
        seen_canonical_track_ids: set[str] = set()
        for raw_track in [*fast_tracks, *fuzzy_tracks]:
            if not isinstance(raw_track, dict):
                continue
            track = dict(raw_track)
            if (
                search_text_similarity(
                    canonical_title,
                    trim_text(track.get("title") or track.get("name")),
                )
                < 0.90
                or search_text_similarity(
                    canonical_artist,
                    trim_text(track.get("channel") or track.get("artist")),
                )
                < 0.80
                or not _track_provider_identity(track)
            ):
                continue
            track_id = _track_provider_identity(track).casefold()
            if track_id in seen_canonical_track_ids:
                continue
            seen_canonical_track_ids.add(track_id)
            canonical_tracks.append(track)
    same_title_provider_credits = {
        normalize_artist_name(track.get("channel") or track.get("artist"))
        for track in fast_tracks
        if isinstance(track, dict)
        and search_text_similarity(
            query,
            trim_text(track.get("title") or track.get("name")),
        )
        >= 0.94
        and normalize_artist_name(track.get("channel") or track.get("artist"))
    }
    unresolved_provider_ambiguity = bool(
        preclassified_intent == "track"
        and len(same_title_provider_credits) > 1
        and not canonical_resolution.get("title")
        and not provider_recording_decision.get("structurally_supported")
    )
    # A title+artist lookup may hydrate an accepted target later, but it must
    # never manufacture the evidence used to accept that target.
    canonical_track_query = ""

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

    target_resolution_started_at = time.perf_counter()
    recording_ambiguity_blocks_target = bool(
        canonical_resolution.get("ambiguous")
        and preclassified_intent in {"track", "mixed"}
        and not provider_recording_decision.get("structurally_supported")
    )
    if recording_ambiguity_blocks_target or unresolved_provider_ambiguity:
        resolved_target = resolve_search_target(
            server=server,
            query=query,
            tracks=[],
            artists=[],
            albums=[],
            search_mode=search_mode,
        )
        resolved_target["evidence"] = [
            str(
                canonical_resolution.get("reason")
                or "uncorroborated_competing_recording_credits"
            )
        ]
        resolved_target["decision_margin"] = float(
            canonical_resolution.get("decision_margin") or 0.0
        )
        resolved_target["candidate_credits"] = list(
            canonical_resolution.get("candidate_credits") or []
        )
        if unresolved_provider_ambiguity and not resolved_target["candidate_credits"]:
            resolved_target["candidate_credits"] = [
                {
                    "title": trim_text(track.get("title") or track.get("name")),
                    "artist": trim_text(track.get("channel") or track.get("artist")),
                    "provider_source_id": trim_text(
                        track.get("videoId")
                        or track.get("video_id")
                        or track.get("id")
                    ),
                }
                for track in fast_tracks
                if normalize_artist_name(
                    track.get("channel") or track.get("artist")
                )
                in same_title_provider_credits
            ][:3]
    else:
        resolved_target = resolve_search_target(
            server=server,
            query=query,
            tracks=[*canonical_tracks, *fast_tracks][:24],
            artists=[*fast_artists, *fuzzy_artists][:16],
            albums=[*fast_albums, *fuzzy_albums][:16],
            canonical_resolution=canonical_resolution,
            relationship_tracks=[
                *canonical_tracks,
                *fast_tracks,
                *fuzzy_tracks,
            ],
            search_mode=search_mode,
            entity_type_hint=(
                preclassified_intent
                if (
                    preclassified_intent in {"track", "artist", "album"}
                    and not canonical_recording_challenge
                )
                else ""
            ),
        )
        if (
            resolved_target.get("entity_type") == "track"
            and provider_recording_decision.get("decisive")
            and not canonical_resolution.get("musicbrainz_recording_id")
        ):
            resolved_target["evidence"] = [
                *list(resolved_target.get("evidence") or []),
                "provider_rank_dominance",
            ]
            resolved_target["provider_resolution"] = {
                key: value
                for key, value in provider_recording_decision.items()
                if key not in {"track"}
            }
    stage_timings_ms["target_resolution"] = int(
        (time.perf_counter() - target_resolution_started_at) * 1000
    )
    query_intent = str(resolved_target.get("entity_type") or "mixed")
    acceptance_barrier_ms = int(
        (time.perf_counter() - retrieval_started_at) * 1000
    )
    resolved_artist = dict(resolved_target.get("lead_artist") or {})
    primary_track = (
        dict(resolved_target.get("item") or {})
        if query_intent == "track"
        else {}
    )
    primary_artist = (
        dict(resolved_target.get("item") or {})
        if query_intent == "artist"
        else {}
    )
    primary_album = (
        dict(resolved_target.get("item") or {})
        if query_intent == "album"
        else {}
    )
    track_album = dict(resolved_target.get("containing_album") or {})
    track_expansion_artist = resolved_artist

    if query_intent == "track" and primary_track:
        _collect_track_candidates(
            track_candidates,
            server=server,
            tracks=[primary_track],
            source_name="resolved_target",
            base_score=6.6,
        )
    if resolved_artist and _artist_provider_identity(resolved_artist):
        _collect_artist_candidates(
            artist_candidates,
            artists=[resolved_artist],
            source_name=(
                "resolved_artist" if query_intent == "artist" else "credited_artist"
            ),
            base_score=6.4,
        )
    if track_album:
        _collect_album_candidates(
            album_candidates,
            albums=[track_album],
            source_name=(
                "resolved_target" if query_intent == "album" else "track_album"
            ),
            base_score=6.2 if query_intent == "album" else 5.3,
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
        "resolved_artist": resolved_artist,
        "resolved_target": resolved_target,
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
            "time_to_first_track_evidence": first_track_evidence_ms,
            "time_to_acceptance_barrier": acceptance_barrier_ms,
            "branches_required": sorted(future_sources.values()),
            "branches_skipped_from_critical_path": [],
            "acceptance_barrier_entity_type": query_intent,
            "canonical_overlap_ms": (
                max(
                    0,
                    (canonical_evidence_completed_ms or 0)
                    - (first_track_evidence_ms or 0),
                )
                if canonical_evidence_completed_ms is not None
                else 0
            ),
            "stage_timings_ms": stage_timings_ms,
            "local_index_ms": local_index_ms,
            "removed_untrusted_aliases": removed_untrusted_aliases,
            "canonical_track_query": canonical_track_query,
            "canonical_evidence_outcome": canonical_evidence_outcome,
            "canonical_evidence_started_ms": canonical_evidence_started_ms,
            "canonical_conflict_grace_applied": cross_entity_recording_conflict,
            "provider_recording_decision": {
                key: value
                for key, value in provider_recording_decision.items()
                if key != "track"
            },
            "classified_entity_type": preclassified_intent,
            "canonical_resolution": canonical_resolution,
            "target_resolver": resolved_target.get("resolver"),
            "target_identity": resolved_target.get("target_identity"),
            "target_confidence": resolved_target.get("confidence"),
            "target_confidence_tier": resolved_target.get("confidence_tier"),
            "target_decision_margin": resolved_target.get("decision_margin"),
            "target_evidence": list(resolved_target.get("evidence") or []),
            "target_candidate_scores": list(
                resolved_target.get("candidate_scores") or []
            ),
            "structured_expansion": expansion_kind,
            "provider_plan": (
                "typed_parallel"
                if complete_first_page
                else "surface_filtered"
            ),
        },
    }
    if cache_key and (
        search_mode == "taste"
        or bool((resolved_target or {}).get("target_identity"))
    ):
        _retrieval_cache_set(cache_key, payload)
    return payload
