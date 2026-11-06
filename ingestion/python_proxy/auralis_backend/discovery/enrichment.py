from __future__ import annotations

from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List
import hashlib
import json
import time

from .schema import DiscoveryArtifact, DiscoveryCandidate, DiscoveryRow, TasteProfile
from ..domain.catalog import normalize_artist_name
from .structured_providers import (
    CanonicalRecording,
    LastFmClient,
    ListenBrainzClient,
)


ACQUISITION_JOB_NAMESPACE = "discovery_acquisition_job"
ACQUISITION_JOB_MODEL = "structured-acquisition"
ACQUISITION_RESULT_TTL_SECONDS = 60 * 60 * 24 * 14
MUSICBRAINZ_RESULT_TTL_SECONDS = 60 * 60 * 24 * 90
ALBUM_CATALOG_CHECK_SECONDS = 6 * 60 * 60
ALBUM_CATALOG_MAX_CARDS = 512
RELEASE_METADATA_NAMESPACE = "canonical_recording_release_metadata"
RELEASE_METADATA_MODEL = "musicbrainz-release-metadata"
RELEASE_METADATA_RETRY_SECONDS = 60 * 60 * 24 * 7
RELEASE_METADATA_FAILURE_RETRY_SECONDS = 60 * 30
RELEASE_METADATA_BATCH_SIZE = 120
_RELEASE_METADATA_LOOKUP_CHUNK_SIZE = 40
_RELEASE_METADATA_POOL_PRIORITY = (
    "discovery_universe",
    "history",
    "profile_spine",
    "similarity",
    "artist_graph",
    "genre_mood",
    "popularity",
    "collaborative",
    "radio_artist_catalog",
    "album",
    "freshness",
)
_MUSICBRAINZ_SHARED_RETRY_DELAYS_SECONDS = (15, 30, 60, 120, 300)


@dataclass(frozen=True)
class EnrichmentRequest:
    kind: str
    key: str
    pool: str
    relation: str
    limit: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""


def _request_provider(request: EnrichmentRequest) -> str:
    explicit = _text(request.provider or request.metadata.get("provider")).casefold()
    if explicit:
        return explicit
    # Non-catalog request kinds remain provider-specific by name.
    return _text(request.kind).split("_", 1)[0].casefold()


def _is_musicbrainz_request(request: EnrichmentRequest) -> bool:
    return _request_provider(request) == "musicbrainz"


@dataclass
class CandidateEnrichmentPlan:
    user_scope_id: str
    requests: List[EnrichmentRequest] = field(default_factory=list)
    anchor_track_count: int = 0
    anchor_artist_count: int = 0
    anchor_cursor_start: int = 0
    anchor_cursor_next: int = 0
    artist_cursor_next: int = 0
    radio_discovery_cursor_next: int = 0
    prior_request_progress: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    priority_pools: tuple[str, ...] = ()
    album_seed_diagnostics: Dict[str, int] = field(default_factory=dict)


@dataclass
class MaterializedCandidateSupply:
    pools: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


def _release_year(item: Dict[str, Any]) -> str:
    for value in (
        item.get("release_year"),
        item.get("year"),
        item.get("release_date"),
        item.get("date"),
    ):
        text = _text(value)
        if len(text) >= 4 and text[:4].isdigit():
            year = int(text[:4])
            if 1800 <= year <= 2200:
                return str(year)
    return ""


def _release_metadata_from_item(item: Dict[str, Any]) -> Dict[str, Any]:
    year = _release_year(item)
    release_date = _text(item.get("release_date") or item.get("date"))
    if not release_date and year:
        release_date = year
    return {
        "release_year": year,
        "release_date": release_date,
        "album": _text(item.get("album") or item.get("release_name")),
        "musicbrainz_release_id": _text(
            item.get("musicbrainz_release_id") or item.get("release_mbid")
        ),
        "musicbrainz_release_group_id": _text(
            item.get("musicbrainz_release_group_id")
            or item.get("release_group_mbid")
        ),
        "country": _text(item.get("country")),
        "metadata_source": "musicbrainz",
    }


def _load_release_metadata(
    server: Any,
    recording_ids: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    ids = list(dict.fromkeys(_text(value).casefold() for value in recording_ids if _text(value)))
    if not ids:
        return {}
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return {}
    output: Dict[str, Dict[str, Any]] = {}
    try:
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            placeholders = ",".join("?" for _value in batch)
            rows = connection.execute(
                f"""
                SELECT entity_id, payload_json
                FROM recommendation_feature_store
                WHERE namespace = ? AND entity_id IN ({placeholders})
                """,
                [RELEASE_METADATA_NAMESPACE, *[f"recording:{value}" for value in batch]],
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except Exception:
                    continue
                if isinstance(payload, dict):
                    output[_text(row["entity_id"]).removeprefix("recording:").casefold()] = dict(payload)
    except Exception:
        return {}
    finally:
        connection.close()
    return output


def _store_release_metadata(
    server: Any,
    values: Dict[str, Dict[str, Any]],
) -> None:
    if not values:
        return
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    now = time.time()
    try:
        connection.executemany(
            """
            INSERT INTO recommendation_feature_store(
                namespace, entity_id, model_id, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                [
                    RELEASE_METADATA_NAMESPACE,
                    f"recording:{recording_id}",
                    RELEASE_METADATA_MODEL,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ]
                for recording_id, payload in values.items()
            ],
        )
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        connection.close()


def _recording_ids_for_playback_sources(
    server: Any,
    source_ids: Iterable[str],
) -> Dict[str, str]:
    ids = list(
        dict.fromkeys(
            _text(value)
            for value in source_ids
            if _text(value) and not _text(value).startswith("recording:")
        )
    )
    if not ids:
        return {}
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return {}
    output: Dict[str, str] = {}
    try:
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            placeholders = ",".join("?" for _value in batch)
            rows = connection.execute(
                f"""
                SELECT sources.source_key, sources.entity_key,
                       entities.payload_json
                FROM catalog_entity_sources AS sources
                LEFT JOIN catalog_entities AS entities
                  ON entities.entity_type = sources.entity_type
                 AND entities.entity_key = sources.entity_key
                WHERE sources.entity_type = 'track'
                  AND sources.source_provider = 'youtube'
                  AND sources.source_key IN ({placeholders})
                ORDER BY sources.confidence DESC
                """,
                batch,
            ).fetchall()
            for row in rows:
                source_id = _text(row["source_key"])
                entity_key = _text(row["entity_key"])
                recording_id = (
                    entity_key.removeprefix("musicbrainz:recording:")
                    if entity_key.startswith("musicbrainz:recording:")
                    else ""
                )
                if not recording_id:
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        recording_id = _text(
                            payload.get("musicbrainz_recording_id")
                            or payload.get("recording_mbid")
                        )
                if source_id and recording_id:
                    output.setdefault(source_id, recording_id.casefold())
    except Exception:
        return {}
    finally:
        connection.close()
    return output


def _lookup_release_metadata(
    server: Any,
    recording_ids: Iterable[str],
    *,
    now: float,
) -> tuple[Dict[str, Dict[str, Any]], bool]:
    missing_ids = list(dict.fromkeys(recording_ids))
    if not missing_ids:
        return {}, False
    discovered: Dict[str, Dict[str, Any]] = {}
    try:
        from ..search.musicbrainz import MusicBrainzClient, musicbrainz_recording_to_item

        client = getattr(server, "musicbrainz_client", None) or MusicBrainzClient()
        recordings: List[Dict[str, Any]] = []
        for offset in range(0, len(missing_ids), _RELEASE_METADATA_LOOKUP_CHUNK_SIZE):
            batch = missing_ids[offset : offset + _RELEASE_METADATA_LOOKUP_CHUNK_SIZE]
            recordings.extend(client.lookup_recordings(batch, limit=len(batch)))
        resolved_ids: set[str] = set()
        for recording in recordings:
            item = musicbrainz_recording_to_item(recording)
            recording_id = _text(item.get("musicbrainz_recording_id")).casefold()
            if not recording_id:
                continue
            resolved_ids.add(recording_id)
            metadata = _release_metadata_from_item(item)
            if metadata["release_year"]:
                discovered[recording_id] = metadata
        for recording_id in missing_ids:
            if recording_id not in resolved_ids:
                discovered[recording_id] = {
                    "release_year": "",
                    "release_date": "",
                    "status": "not_found",
                    "retry_after": now + RELEASE_METADATA_RETRY_SECONDS,
                    "metadata_source": "musicbrainz",
                }
        return discovered, False
    except Exception:
        return (
            {
                recording_id: {
                    "release_year": "",
                    "release_date": "",
                    "status": "retryable",
                    "retry_after": now + RELEASE_METADATA_FAILURE_RETRY_SECONDS,
                    "metadata_source": "musicbrainz",
                }
                for recording_id in missing_ids
            },
            True,
        )


def complete_inventory_release_metadata(
    server: Any,
    inventory: Any,
    *,
    max_new_lookups: int = RELEASE_METADATA_BATCH_SIZE,
    allow_remote_lookup: bool = True,
) -> Any:
    """Fill and persist release years for canonical inventory recordings.

    This runs only inside background inventory preparation. Known metadata is
    reused across pools and persisted by recording MBID; at most one bounded
    MusicBrainz batch is needed per cycle.
    """

    started = time.perf_counter()
    all_items: List[Dict[str, Any]] = []
    ordered_pool_names = list(
        dict.fromkeys(
            [
                *[name for name in _RELEASE_METADATA_POOL_PRIORITY if name in inventory.pools],
                *inventory.pools.keys(),
            ]
        )
    )
    for pool_name in ordered_pool_names:
        candidates = inventory.pools.get(pool_name) or []
        for candidate in candidates or []:
            item = candidate.item or {}
            if candidate.item_type == "album":
                all_items.extend(
                    track
                    for track in item.get("canonical_tracks") or item.get("tracks") or []
                    if isinstance(track, dict)
                )
            else:
                all_items.append(item)

    recording_ids = list(
        dict.fromkeys(
            _text(item.get("musicbrainz_recording_id") or item.get("recording_mbid")).casefold()
            for item in all_items
            if _text(item.get("musicbrainz_recording_id") or item.get("recording_mbid"))
        )
    )
    cached = _load_release_metadata(server, recording_ids)
    known_by_recording: Dict[str, Dict[str, Any]] = dict(cached)
    known_by_release_group: Dict[str, Dict[str, Any]] = {}
    discovered: Dict[str, Dict[str, Any]] = {}
    for item in all_items:
        metadata = _release_metadata_from_item(item)
        if not metadata["release_year"]:
            continue
        recording_id = _text(
            item.get("musicbrainz_recording_id") or item.get("recording_mbid")
        ).casefold()
        release_group_id = _text(metadata.get("musicbrainz_release_group_id")).casefold()
        if recording_id:
            known_by_recording[recording_id] = metadata
            if recording_id not in cached:
                discovered[recording_id] = metadata
        if release_group_id:
            known_by_release_group.setdefault(release_group_id, metadata)

    now = time.time()
    missing_ids = [
        recording_id
        for recording_id in recording_ids
        if not _release_year(known_by_recording.get(recording_id) or {})
        and float((cached.get(recording_id) or {}).get("retry_after") or 0.0) <= now
    ][: max(int(max_new_lookups or 0), 0)]
    lookup_values, lookup_failed = (
        _lookup_release_metadata(server, missing_ids, now=now)
        if allow_remote_lookup and max_new_lookups > 0
        else ({}, False)
    )
    discovered.update(lookup_values)
    for recording_id, metadata in lookup_values.items():
        if not _release_year(metadata):
            continue
        known_by_recording[recording_id] = metadata
        release_group_id = _text(
            metadata.get("musicbrainz_release_group_id")
        ).casefold()
        if release_group_id:
            known_by_release_group.setdefault(release_group_id, metadata)
    _store_release_metadata(server, discovered)
    effective_cache = {**cached, **discovered}
    remaining_lookup_count = sum(
        1
        for recording_id in recording_ids
        if not _release_year(known_by_recording.get(recording_id) or {})
        and float((effective_cache.get(recording_id) or {}).get("retry_after") or 0.0)
        <= now
    )

    filled = 0

    def hydrate(raw: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal filled
        item = dict(raw)
        if _release_year(item):
            item.setdefault("release_year", _release_year(item))
            return item
        recording_id = _text(
            item.get("musicbrainz_recording_id") or item.get("recording_mbid")
        ).casefold()
        release_group_id = _text(
            item.get("musicbrainz_release_group_id") or item.get("release_group_mbid")
        ).casefold()
        metadata = known_by_recording.get(recording_id) or known_by_release_group.get(
            release_group_id
        )
        if not metadata or not _release_year(metadata):
            return item
        for key in (
            "release_year",
            "release_date",
            "album",
            "musicbrainz_release_id",
            "musicbrainz_release_group_id",
            "country",
        ):
            value = metadata.get(key)
            if value not in (None, "") and item.get(key) in (None, ""):
                item[key] = value
        item["release_metadata_source"] = "musicbrainz"
        filled += 1
        return item

    pools: Dict[str, List[DiscoveryCandidate]] = {}
    for pool_name, candidates in inventory.pools.items():
        output: List[DiscoveryCandidate] = []
        for candidate in candidates or []:
            item = dict(candidate.item or {})
            if candidate.item_type == "album":
                tracks = [
                    hydrate(track)
                    for track in item.get("canonical_tracks") or item.get("tracks") or []
                    if isinstance(track, dict)
                ]
                item["canonical_tracks"] = tracks
                item["tracks"] = tracks
                item = hydrate(item)
            else:
                item = hydrate(item)
            output.append(
                DiscoveryCandidate(
                    item=item,
                    source=candidate.source,
                    score=candidate.score,
                    reasons=list(candidate.reasons or []),
                    item_type=candidate.item_type,
                )
            )
        pools[pool_name] = output

    with_year_ids: set[str] = set()
    for candidates in pools.values():
        for candidate in candidates:
            items = (
                candidate.item.get("canonical_tracks")
                or candidate.item.get("tracks")
                or []
                if candidate.item_type == "album"
                else [candidate.item]
            )
            for item in items:
                if not isinstance(item, dict) or not _release_year(item):
                    continue
                recording_id = _text(
                    item.get("musicbrainz_recording_id") or item.get("recording_mbid")
                ).casefold()
                if recording_id:
                    with_year_ids.add(recording_id)
    with_year = len(with_year_ids)
    counts = dict(inventory.candidate_counts or {})
    counts.update(
        {
            "release_metadata_canonical_tracks": len(recording_ids),
            "release_metadata_tracks_with_year": with_year,
            "release_metadata_cache_hits": sum(
                1 for value in cached.values() if _release_year(value)
            ),
            "release_metadata_lookup_count": len(missing_ids),
            "release_metadata_filled_count": filled,
            "release_metadata_pending_count": remaining_lookup_count,
        }
    )
    timings = dict(inventory.provider_timings_ms or {})
    timings["release_metadata"] = int((time.perf_counter() - started) * 1000)
    ledger = dict(inventory.acquisition_ledger or {})
    ledger["release_metadata"] = {
        "canonical_tracks": len(recording_ids),
        "tracks_with_year": with_year,
        "missing": max(len(recording_ids) - with_year, 0),
        "lookup_count": len(missing_ids),
        "lookup_failed": lookup_failed,
        "pending_lookup_count": remaining_lookup_count,
    }
    print(
        "[EBB:release-metadata] "
        f"canonical={len(recording_ids)} with_year={with_year} "
        f"cache_hits={counts['release_metadata_cache_hits']} "
        f"lookups={len(missing_ids)} filled={filled} "
        f"pending={remaining_lookup_count} failed={1 if lookup_failed else 0}"
    )
    return replace(
        inventory,
        pools=pools,
        candidate_counts=counts,
        provider_timings_ms=timings,
        acquisition_ledger=ledger,
    )




def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _track_id(item: Dict[str, Any]) -> str:
    explicit = _text(item.get("canonical_entity_id"))
    if explicit:
        return explicit
    recording = CanonicalRecording.from_item(item)
    return recording.entity_key or _text(
        item.get("track_key")
        or item.get("id")
        or item.get("videoId")
        or item.get("video_id")
    )


def _canonical_recording_item(
    item: Dict[str, Any],
    *,
    mbid_by_semantic: Dict[str, str],
    isrc_by_semantic: Dict[str, str],
) -> tuple[Dict[str, Any], bool]:
    recording = CanonicalRecording.from_item(item)
    recording_mbid = recording.recording_mbid
    isrc = recording.isrc
    upgraded = False
    if not recording_mbid and recording.semantic_key:
        recording_mbid = _text(mbid_by_semantic.get(recording.semantic_key))
        upgraded = bool(recording_mbid)
    if not isrc and recording.semantic_key:
        isrc = _text(isrc_by_semantic.get(recording.semantic_key))
        upgraded = upgraded or bool(isrc)
    canonical = CanonicalRecording.from_item(
        {
            **item,
            "musicbrainz_recording_id": recording_mbid,
            "isrc": isrc,
        }
    )
    normalized = {
        **dict(item),
        "title": canonical.title,
        "artist": canonical.artist,
        "canonical_entity_id": canonical.entity_key,
        "track_key": canonical.track_key,
    }
    if canonical.recording_mbid:
        normalized["musicbrainz_recording_id"] = canonical.recording_mbid
    if canonical.isrc:
        normalized["isrc"] = canonical.isrc
    return normalized, upgraded


def _merge_canonical_recordings(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    existing_recording = CanonicalRecording.from_item(existing)
    incoming_recording = CanonicalRecording.from_item(incoming)
    existing_strength = (
        4 if existing_recording.recording_mbid else 0,
        2 if existing_recording.isrc else 0,
        1 if existing_recording.release_group_mbid else 0,
        float(existing.get("relationship_score") or 0.0),
    )
    incoming_strength = (
        4 if incoming_recording.recording_mbid else 0,
        2 if incoming_recording.isrc else 0,
        1 if incoming_recording.release_group_mbid else 0,
        float(incoming.get("relationship_score") or 0.0),
    )
    preferred, secondary = (
        (incoming, existing)
        if incoming_strength > existing_strength
        else (existing, incoming)
    )
    merged = dict(secondary)
    merged.update({key: value for key, value in preferred.items() if value not in (None, "", [], {})})
    merged["relationship_score"] = max(
        float(existing.get("relationship_score") or 0.0),
        float(incoming.get("relationship_score") or 0.0),
    )
    evidence: List[Any] = []
    for value in (
        existing.get("relationship_evidence"),
        incoming.get("relationship_evidence"),
    ):
        values = value if isinstance(value, list) else [value]
        for entry in values:
            if entry not in (None, "") and entry not in evidence:
                evidence.append(entry)
    if evidence:
        merged["relationship_evidence"] = evidence
    provenances = list(
        dict.fromkeys(
            _text(value)
            for value in (
                existing.get("source_provenance"),
                incoming.get("source_provenance"),
            )
            if _text(value)
        )
    )
    if provenances:
        merged["source_provenance"] = provenances[0]
        merged["source_provenances"] = provenances
    radio_seeds = list(
        dict.fromkeys(
            _text(value)
            for item in (existing, incoming)
            for value in [
                *(item.get("radio_seed_artists") or []),
                item.get("radio_seed_artist"),
            ]
            if _text(value)
        )
    )
    if radio_seeds:
        merged["radio_seed_artist"] = radio_seeds[0]
        merged["radio_seed_artists"] = radio_seeds
    return merged


def _album_semantic_key(item: Dict[str, Any]) -> str:
    title = " ".join(
        _text(item.get("title") or item.get("name") or item.get("album"))
        .casefold()
        .split()
    )
    artist = " ".join(
        (_artist(item) or _text(item.get("artist_name"))).casefold().split()
    )
    return f"{title}|{artist}" if title and artist else ""


def _merge_catalog_albums(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    existing_mb = bool(_text(existing.get("musicbrainz_release_group_id")))
    incoming_mb = bool(_text(incoming.get("musicbrainz_release_group_id")))
    preferred, secondary = (
        (incoming, existing)
        if incoming_mb and not existing_mb
        else (existing, incoming)
    )
    merged = dict(secondary)
    merged.update(
        key_value
        for key_value in preferred.items()
        if key_value[1] not in (None, "", [], {})
    )
    track_map: Dict[str, Dict[str, Any]] = {}
    for album in (secondary, preferred):
        for track in album.get("canonical_tracks") or []:
            if not isinstance(track, dict):
                continue
            identity = _track_id(track)
            if not identity:
                continue
            track_map[identity] = (
                _merge_canonical_recordings(track_map[identity], track)
                if identity in track_map
                else dict(track)
            )
    merged["canonical_tracks"] = list(track_map.values())
    merged["track_count"] = len(track_map)
    merged["canonical_track_count"] = len(track_map)
    provenances = list(
        dict.fromkeys(
            _text(value)
            for album in (existing, incoming)
            for value in (
                album.get("source_provenance"),
                *(album.get("source_provenances") or []),
            )
            if _text(value)
        )
    )
    if provenances:
        merged["source_provenance"] = provenances[0]
        merged["source_provenances"] = provenances
    return merged


def canonicalize_materialized_pools(
    pools: Dict[str, List[Dict[str, Any]]],
) -> tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    all_tracks: List[Dict[str, Any]] = []
    for pool, values in pools.items():
        if pool == "album":
            for album in values or []:
                all_tracks.extend(
                    track
                    for track in album.get("canonical_tracks") or []
                    if isinstance(track, dict)
                )
        else:
            all_tracks.extend(value for value in values or [] if isinstance(value, dict))

    mbids: Dict[str, set[str]] = {}
    isrcs: Dict[str, set[str]] = {}
    for item in all_tracks:
        recording = CanonicalRecording.from_item(item)
        if not recording.semantic_key:
            continue
        if recording.recording_mbid:
            mbids.setdefault(recording.semantic_key, set()).add(recording.recording_mbid)
        if recording.isrc:
            isrcs.setdefault(recording.semantic_key, set()).add(recording.isrc)
    mbid_by_semantic = {
        key: next(iter(values))
        for key, values in mbids.items()
        if len(values) == 1
    }
    isrc_by_semantic = {
        key: next(iter(values))
        for key, values in isrcs.items()
        if len(values) == 1
    }

    raw_count = len(all_tracks)
    identity_upgrades = 0
    global_identities: set[str] = set()
    output: Dict[str, List[Dict[str, Any]]] = {}
    for pool, values in pools.items():
        if pool == "album":
            albums_by_semantic: Dict[str, Dict[str, Any]] = {}
            album_order: List[str] = []
            for album in values or []:
                album_tracks: Dict[str, Dict[str, Any]] = {}
                for item in album.get("canonical_tracks") or []:
                    if not isinstance(item, dict):
                        continue
                    normalized, upgraded = _canonical_recording_item(
                        item,
                        mbid_by_semantic=mbid_by_semantic,
                        isrc_by_semantic=isrc_by_semantic,
                    )
                    identity_upgrades += int(upgraded)
                    identity = _track_id(normalized)
                    if not identity:
                        continue
                    global_identities.add(identity)
                    album_tracks[identity] = (
                        _merge_canonical_recordings(album_tracks[identity], normalized)
                        if identity in album_tracks
                        else normalized
                    )
                normalized_album = {
                    **dict(album),
                    "canonical_tracks": list(album_tracks.values()),
                }
                semantic = _album_semantic_key(normalized_album)
                if not semantic:
                    continue
                if semantic not in albums_by_semantic:
                    albums_by_semantic[semantic] = normalized_album
                    album_order.append(semantic)
                else:
                    albums_by_semantic[semantic] = _merge_catalog_albums(
                        albums_by_semantic[semantic],
                        normalized_album,
                    )
            output[pool] = [
                albums_by_semantic[semantic] for semantic in album_order
            ]
            continue

        canonical_items: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for item in values or []:
            if not isinstance(item, dict):
                continue
            normalized, upgraded = _canonical_recording_item(
                item,
                mbid_by_semantic=mbid_by_semantic,
                isrc_by_semantic=isrc_by_semantic,
            )
            identity_upgrades += int(upgraded)
            identity = _track_id(normalized)
            if not identity:
                continue
            global_identities.add(identity)
            if identity not in canonical_items:
                canonical_items[identity] = normalized
                order.append(identity)
            else:
                canonical_items[identity] = _merge_canonical_recordings(
                    canonical_items[identity],
                    normalized,
                )
        output[pool] = [canonical_items[identity] for identity in order]

    return output, {
        "canonical_supply_raw_tracks": raw_count,
        "canonical_supply_unique_tracks": len(global_identities),
        "canonical_supply_duplicates_removed": max(raw_count - len(global_identities), 0),
        "canonical_supply_identity_upgrades": identity_upgrades,
    }


def _artist(item: Dict[str, Any]) -> str:
    value = item.get("artist") or item.get("channel") or item.get("author")
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    if not value:
        for entry in item.get("artists") or []:
            if isinstance(entry, dict):
                value = entry.get("name") or entry.get("title")
            elif entry:
                value = entry
            if value:
                break
    return _text(value)


def _unique(values: Iterable[str], *, limit: int) -> List[str]:
    output: List[str] = []
    seen: set[str] = set()
    for value in values:
        value = _text(value)
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def _request_signature(request: EnrichmentRequest) -> str:
    cursor = int(request.metadata.get("release_group_offset") or request.metadata.get("offset") or 0)
    provider = _request_provider(request)
    if request.kind == 'album_catalog' and provider == 'ytmusic':
        return f'album_catalog:ytmusic:v2:{request.pool}:{request.key.casefold()}:{cursor}'
    return (
        f"{request.kind}:{provider}:{request.pool}:"
        f"{request.key.casefold()}:{cursor}"
    )


def _request_progress_signature(request: EnrichmentRequest) -> str:
    """Return the durable progress identity for a paged provider request.

    Page offsets identify cached page results, but they must not identify the
    exhaustion state. Otherwise every empty page starts a new no-progress
    counter and one artist can be paged forever.
    """

    if request.kind == 'album_catalog' and _request_provider(request) == 'ytmusic':
        return f'album_catalog:ytmusic:v2:{request.pool}:{request.key.casefold()}'
    if request.kind in {"album_catalog", "artist_catalog"}:
        return (
            f"{request.kind}:{_request_provider(request)}:"
            f"{request.pool}:{request.key.casefold()}"
        )
    return _request_signature(request)


def _provider_shared_retry_at(
    request_progress: Dict[str, Dict[str, Any]],
    provider: str,
) -> float:
    """Return the one durable backoff shared by all jobs for a provider."""

    wanted = _text(provider).casefold()
    return max(
        (
            float(progress.get("provider_retry_at") or 0.0)
            for signature, progress in request_progress.items()
            if isinstance(progress, dict)
            and (
                _text(progress.get("provider")).casefold() == wanted
                or f":{wanted}:" in f":{signature.casefold()}:"
            )
        ),
        default=0.0,
    )


def _musicbrainz_shared_retry_at(
    request_progress: Dict[str, Dict[str, Any]],
) -> float:
    """Compatibility name for MusicBrainz-specific diagnostics and tests."""

    return _provider_shared_retry_at(request_progress, "musicbrainz")


def _provider_failure_details(exc: BaseException) -> Dict[str, Any]:
    cause_type = _text(getattr(exc, "cause_type", "")) or type(exc).__name__
    response = getattr(exc, "response", None)
    status_code = int(
        getattr(exc, "status_code", 0)
        or getattr(response, "status_code", 0)
        or 0
    )
    retry_after = getattr(exc, "retry_after_seconds", 0.0) or 0.0
    if not retry_after and response is not None:
        try:
            retry_after = float((response.headers or {}).get("Retry-After") or 0.0)
        except (TypeError, ValueError):
            retry_after = 0.0
    transient = bool(
        getattr(
            exc,
            "transient",
            cause_type in {"TimeoutError", "URLError"}
            or status_code == 0
            or status_code in {408, 425, 429}
            or status_code >= 500,
        )
    )
    return {
        "last_error": cause_type,
        "http_status": status_code,
        "request_phase": _text(getattr(exc, "path", "")),
        "provider_elapsed_ms": int(getattr(exc, "elapsed_ms", 0) or 0),
        "provider_retry_after_seconds": float(
            retry_after
        ),
        "transient": transient,
    }


def _provider_retry_delay_seconds(
    details: Dict[str, Any],
    attempts: int,
    *,
    provider: str = "musicbrainz",
) -> float:
    retry_after = float(details.get("provider_retry_after_seconds") or 0.0)
    if retry_after > 0:
        return min(max(retry_after, 1.0), 1800.0)
    if _text(provider).casefold() == "musicbrainz":
        index = min(max(int(attempts or 1) - 1, 0), len(_MUSICBRAINZ_SHARED_RETRY_DELAYS_SECONDS) - 1)
        return float(_MUSICBRAINZ_SHARED_RETRY_DELAYS_SECONDS[index])
    return float(min(30 * (2 ** min(max(int(attempts or 1) - 1, 0), 4)), 300))


def _request_cursor(
    progress: Dict[str, Dict[str, Any]],
    *,
    kind: str,
    pool: str,
    key: str,
) -> int:
    prefix = f"{kind}:"
    return max(
        (
            int(value.get("cursor") or 0)
            for signature, value in progress.items()
            if signature.startswith(prefix)
            and f":{pool}:" in signature
            and f":{key.casefold()}" in signature
            and isinstance(value, dict)
        ),
        default=0,
    )


def _select_scheduled_requests(
    pending: List[EnrichmentRequest],
    limit: int,
    *,
    priority_pools: tuple[str, ...] = (),
) -> List[EnrichmentRequest]:
    if limit <= 0:
        return []
    default_pool_order = (
        "similarity",
        "profile_spine",
        "radio_artist_catalog",
        "artist_graph",
        "genre_mood",
        "collaborative",
        "popularity",
        "album",
    )
    pool_order = tuple(
        dict.fromkeys([*priority_pools, *default_pool_order])
    )
    buckets: Dict[str, List[EnrichmentRequest]] = {}
    for request in pending:
        buckets.setdefault(request.pool, []).append(request)
    ordered_pools = [
        pool for pool in pool_order if buckets.get(pool)
    ]
    ordered_pools.extend(pool for pool in buckets if pool not in ordered_pools)
    selected: List[EnrichmentRequest] = []
    # MusicBrainz is one optional canonical-enrichment lane.  Select regular
    # providers first, reserving at most one MB request for the cycle.  This
    # prevents six album/radio pages from monopolising acquisition capacity.
    non_mb = [pool for pool in ordered_pools if any(
        not _is_musicbrainz_request(request)
        for request in buckets.get(pool, [])
    )]

    def pop_non_mb(pool: str) -> EnrichmentRequest | None:
        bucket = buckets.get(pool) or []
        for index, request in enumerate(bucket):
            if not _is_musicbrainz_request(request):
                return bucket.pop(index)
        return None
    mb_available = any(
        _is_musicbrainz_request(request)
        for request in pending
    )
    while len(selected) < limit - (1 if mb_available else 0):
        added = False
        for pool in non_mb:
            request = pop_non_mb(pool)
            if request is not None:
                selected.append(request)
                added = True
                if len(selected) >= limit - (1 if mb_available else 0):
                    break
        if not added:
            break
    if mb_available and len(selected) < limit:
        for pool in ordered_pools:
            bucket = buckets.get(pool) or []
            mb_request = next(
                (request for request in bucket if _is_musicbrainz_request(request)),
                None,
            )
            if mb_request is not None:
                bucket.remove(mb_request)
                selected.append(mb_request)
                break
    # Fill any remaining capacity with non-MB work only.  MB remains capped at
    # one even when regular providers have fewer pending jobs.
    while len(selected) < limit:
        added = False
        for pool in non_mb:
            request = pop_non_mb(pool)
            if request is not None:
                selected.append(request)
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
    return selected


def _job_key(user_scope_id: str, request: EnrichmentRequest) -> str:
    raw = f"{user_scope_id}|{_request_signature(request)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _profile_genres(taste: TasteProfile) -> List[str]:
    values: List[str] = []
    profile = dict(taste.source_profile or {})
    for key in ("top_genres", "genres", "genre_hints", "preferred_genres"):
        raw = profile.get(key)
        if isinstance(raw, dict):
            values.extend(str(value) for value in raw.keys())
        elif isinstance(raw, list):
            values.extend(str(value) for value in raw)
    for track in [*taste.recent_tracks, *taste.top_tracks, *taste.anchor_tracks]:
        raw = track.get("genre") or track.get("genres") or track.get("tags")
        if isinstance(raw, list):
            values.extend(str(value) for value in raw)
        elif raw:
            values.append(str(raw))
    return _unique(values, limit=6)


def _artist_musicbrainz_ids(taste: TasteProfile) -> Dict[str, str]:
    identities: Dict[str, str] = {}
    for track in [
        *taste.full_history_tracks,
        *taste.recent_tracks,
        *taste.top_tracks,
        *taste.anchor_tracks,
    ]:
        if not isinstance(track, dict):
            continue
        artist_key = _artist(track).casefold()
        artist_ids = track.get("musicbrainz_artist_ids") or []
        if isinstance(artist_ids, str):
            artist_ids = [artist_ids]
        artist_id = _text(track.get("musicbrainz_artist_id") or next(iter(artist_ids), ""))
        if artist_key and artist_id:
            identities.setdefault(artist_key, artist_id)
    return identities


def _diverse_history_anchors(taste: TasteProfile, cursor: int) -> tuple[List[Dict[str, Any]], int]:
    history = [
        dict(track)
        for track in (
            taste.full_history_tracks
            or [*taste.recent_tracks, *taste.top_tracks, *taste.last_played_tracks, *taste.anchor_tracks]
        )
        if isinstance(track, dict) and _track_id(track)
    ]
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    artist_counts: Dict[str, int] = {}
    album_counts: Dict[str, int] = {}

    def add(track: Dict[str, Any], *, diverse: bool = True) -> bool:
        identity = _track_id(track)
        artist_key = _artist(track).casefold()
        album = track.get("album") or track.get("album_title") or ""
        if isinstance(album, dict):
            album = album.get("title") or album.get("name") or ""
        album_key = _text(album).casefold()
        if not identity or identity in seen:
            return False
        if diverse and artist_key and artist_counts.get(artist_key, 0) >= 2:
            return False
        if diverse and album_key and album_counts.get(album_key, 0) >= 1:
            return False
        seen.add(identity)
        output.append(track)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if album_key:
            album_counts[album_key] = album_counts.get(album_key, 0) + 1
        return True

    frequent = sorted(
        list(taste.frequent_tracks or taste.top_tracks),
        key=lambda track: (
            int(track.get("play_count") or 0),
            float(track.get("last_played_at") or 0),
        ),
        reverse=True,
    )
    older = sorted(
        history,
        key=lambda track: float(track.get("last_played_at") or 0),
    )
    saved = [
        track
        for track in history
        if track.get("is_favorite") is True
        or track.get("in_library") is True
        or track.get("saved") is True
    ]
    for group, quota in (
        (list(taste.recent_tracks), 6),
        (frequent, 6),
        (older, 4),
        (saved, 4),
    ):
        added = 0
        for track in group:
            if add(dict(track)):
                added += 1
            if added >= quota:
                break
    if history:
        rotated = [*history[cursor:], *history[:cursor]]
        for track in rotated:
            if len(output) >= 24:
                break
            add(track)
        for track in rotated:
            if len(output) >= 24:
                break
            add(track, diverse=False)
    next_cursor = (cursor + max(len(output), 1)) % max(len(history), 1)
    return output, next_cursor


def profile_artist_demand(taste: TasteProfile, ledger: Dict[str, Any]) -> List[str]:
    """Recent artist identities whose catalog or relationship lane is untried.

    Reuse durable request progress, including terminal empty results. Retryable
    work remains demand but materialization still owns provider backoff/leases.
    No timestamp-only profile change invalidates the retained inventory.
    """
    progress = dict(ledger.get('request_progress') or {})
    ids = {**dict(ledger.get('canonical_artist_ids') or {}), **_artist_musicbrainz_ids(taste)}
    artists = _unique([_artist(track) for track in
        [*taste.recent_tracks, *taste.last_played_tracks][:64]], limit=32)
    demand = []
    for artist in artists:
        if normalize_artist_name(artist) in {'', 'unknown', 'unknown artist'}:
            continue
        def finished(request):
            state = (progress.get(_request_progress_signature(request))
                     or progress.get(_request_signature(request)) or {})
            return state.get('state') in {'resolved', 'exhausted'} or state.get('exhausted') is True
        catalog = any(finished(EnrichmentRequest('artist_catalog', artist, 'profile_spine',
            'same_artist_catalog', 16, provider=provider)) for provider in ('ytmusic', 'lastfm'))
        related = finished(EnrichmentRequest('lastfm_artist_similar', ids.get(artist.casefold()) or artist,
            'artist_graph', 'artist_neighbor', 8, metadata={'offset': 0}))
        albums = finished(EnrichmentRequest('album_catalog', artist, 'album',
            'artist_album_catalog', 3, provider='ytmusic'))
        if not catalog or not related or not albums:
            demand.append(artist)
    return demand


def build_enrichment_plan(
    taste: TasteProfile,
    *,
    acquisition_ledger: Dict[str, Any] | None = None,
    allowed_pools: set[str] | None = None,
    radio_discovery_artist_seeds: List[Dict[str, Any]] | None = None,
    radio_discovery_deficit: int = 0,
    radio_reservoir_deficit: int = 0,
) -> CandidateEnrichmentPlan:
    ledger = dict(acquisition_ledger or {})
    request_progress = {
        str(key): dict(value)
        for key, value in dict(ledger.get("request_progress") or {}).items()
        if isinstance(value, dict)
    }
    cursor = max(int(ledger.get("anchor_cursor") or 0), 0)
    anchor_tracks, cursor_next = _diverse_history_anchors(taste, cursor)
    demand_artists = profile_artist_demand(taste, ledger)
    demand_keys = {normalize_artist_name(artist) for artist in demand_artists}
    familiar = _unique([*taste.artist_hints, *taste.top_artists, *taste.listened_artists,
                       *[_artist(track) for track in anchor_tracks]], limit=32)
    artist_cursor = max(int(ledger.get("artist_cursor") or 0), 0)
    def rotate(values):
        start = artist_cursor % len(values) if values else 0
        return [*values[start:], *values[:start]]
    # Interleave before the bound so new listening cannot sit behind 32 old
    # favourites; keep familiar seeds and their existing rotating ownership.
    recent, old = rotate(demand_artists), rotate(familiar)
    interleaved = [artist for i in range(max(len(recent), len(old)))
                   for artist in ([recent[i]] if i < len(recent) else []) +
                                 ([old[i]] if i < len(old) else [])]
    all_anchor_artists = _unique(interleaved, limit=64)
    all_anchor_artists = [artist for artist in all_anchor_artists
                          if normalize_artist_name(artist) not in {"", "unknown", "unknown artist"}]
    rotated_artists = all_anchor_artists
    anchor_artists = rotated_artists[:8]
    if allowed_pools == {"radio_artist_catalog"}:
        # Radio replenishment is a bounded local shortage repair. Prefer
        # direct seeds closest to the 12-track minimum and cap one pass.
        seed_counts = {
            str(key).casefold(): int(value or 0)
            for key, value in dict(ledger.get("radio_seed_counts") or {}).items()
        }
        incomplete_artists = [
            artist
            for artist in anchor_artists
            if seed_counts.get(str(artist).casefold(), 0) < 12
        ]
        direct_repairs = sorted(
            incomplete_artists,
            key=lambda artist: max(12 - seed_counts.get(str(artist).casefold(), 0), 0),
        )
        # Discovery seeds come from persisted, profile-compatible artist
        # evidence. They must take precedence over repairing familiar seeds
        # while the radio discovery reserve is deficient.
        discovery = []
        for seed in radio_discovery_artist_seeds or []:
            name = _text(seed.get("name") if isinstance(seed, dict) else seed)
            if name and name.casefold() not in {a.casefold() for a in all_anchor_artists}:
                discovery.append(name)
        # Skip seeds whose provider requests are durably exhausted before
        # applying the batch bound. Otherwise exhausted candidates at the
        # front of the frontier can starve later eligible artists forever.
        candidate_artist_ids = {
            str(key).casefold(): str(value)
            for key, value in dict(ledger.get("canonical_artist_ids") or {}).items()
            if _text(key) and _text(value)
        }
        candidate_artist_ids.update(_artist_musicbrainz_ids(taste))
        for seed in radio_discovery_artist_seeds or []:
            if isinstance(seed, dict):
                name = _text(seed.get("name"))
                identity = _text(seed.get("musicbrainz_artist_id"))
                if name and identity:
                    candidate_artist_ids[name.casefold()] = identity

        def has_non_exhausted_radio_request(artist: str) -> bool:
            artist_mbid = _text(candidate_artist_ids.get(artist.casefold()))
            for provider in ("ytmusic", "lastfm", "listenbrainz", "musicbrainz"):
                if provider in {"listenbrainz", "musicbrainz"} and not artist_mbid:
                    continue
                request = EnrichmentRequest(
                    kind="artist_catalog",
                    provider=provider,
                    key=(artist_mbid if provider in {"listenbrainz", "musicbrainz"} else artist),
                    pool="radio_artist_catalog",
                    relation="same_artist_catalog",
                    limit=24 if provider != "musicbrainz" else 12,
                    metadata={"provider": provider},
                )
                progress = request_progress.get(_request_progress_signature(request), {})
                if (
                    progress.get("exhausted") is not True
                    and _text(progress.get("state")).casefold() != "exhausted"
                ):
                    return True
            return False

        # Keep the frontier intact until durable jobs/cache/provider backoffs
        # are inspected by materialization. Its four-request execution bound
        # belongs after that inspection; an earlier four-seed cut can starve
        # later available work behind cooling or cached artists forever.
        active_seed = _text(ledger.get("radio_active_seed"))
        if active_seed:
            candidates = [active_seed, *discovery, *direct_repairs]
        elif int(radio_discovery_deficit or 0) > 0:
            candidates = [*discovery, *direct_repairs]
        else:
            candidates = [*direct_repairs, *discovery]
        eligible = [
            artist
            for artist in _unique(candidates, limit=max(len(candidates), 1))
            if has_non_exhausted_radio_request(artist)
        ]
        anchor_artists = eligible
    artist_cursor_next = (
        (artist_cursor + max(len(anchor_artists), 1)) % len(all_anchor_artists)
        if all_anchor_artists
        else 0
    )
    if allowed_pools == {"radio_artist_catalog"} and _text(ledger.get("radio_active_seed")):
        # Retries for an in-progress seed must not rotate the familiar artist
        # cursor; advancement is committed only by the qualification/terminal
        # transition in the expansion worker.
        artist_cursor_next = artist_cursor
    radio_discovery_cursor_next = int(ledger.get("radio_discovery_cursor") or 0)
    if allowed_pools == {"radio_artist_catalog"}:
        # Cursor movement is a qualification event, not provider completion.
        # The expansion worker advances this only after a seed is qualified or
        # reaches a terminal request state.
        radio_discovery_cursor_next = max(int(ledger.get("radio_discovery_cursor") or 0), 0)
    artist_ids = {
        str(key): str(value)
        for key, value in dict(ledger.get("canonical_artist_ids") or {}).items()
        if _text(key) and _text(value)
    }
    artist_ids.update(_artist_musicbrainz_ids(taste))
    for seed in radio_discovery_artist_seeds or []:
        if isinstance(seed, dict):
            name = _text(seed.get("name"))
            identity = _text(seed.get("musicbrainz_artist_id"))
            if name and identity:
                artist_ids[name.casefold()] = identity
    optional_row_counts = {
        str(key): int(value or 0)
        for key, value in dict(ledger.get("optional_row_counts") or {}).items()
    }
    # Popular Radio acquisition belongs exclusively to the radio-supply
    # worker.  Feed preparation may consume the persisted radio reservoir,
    # but must never open a second provider-acquisition path merely because
    # its optional row is short.
    radio_work_needed = (
        allowed_pools == {"radio_artist_catalog"}
        and (
            int(optional_row_counts.get("popular_radio") or 0) < 8
            or int(radio_discovery_deficit or 0) > 0
            or int(radio_reservoir_deficit or 0) > 0
            or bool(radio_discovery_artist_seeds)
        )
    )
    requests: List[EnrichmentRequest] = []
    if taste.is_cold_start or not anchor_artists:
        sitewide_offset = max(
            [
                int(progress.get("cursor") or 0)
                for signature, progress in request_progress.items()
                if signature.startswith("listenbrainz_sitewide_recordings:popularity:")
            ]
            or [int(ledger.get("listenbrainz_sitewide_offset") or 0)]
        )
        requests.append(
            EnrichmentRequest(
                kind="listenbrainz_sitewide_recordings",
                key="all_time",
                pool="popularity",
                relation="broad_global",
                limit=40,
                metadata={
                    "user_scope_id": taste.user_scope_id,
                    "offset": sitewide_offset,
                },
            )
        )
    for track in anchor_tracks:
        recording = CanonicalRecording.from_item(track)
        if not recording.title or not recording.artist:
            continue
        requests.append(
            EnrichmentRequest(
                kind="lastfm_track_similar",
                key=recording.recording_mbid or recording.track_key,
                pool="similarity",
                relation="track_radio",
                limit=24,
                metadata={
                    "canonical_track": dict(track),
                    "related_to_track": recording.track_key,
                    "related_to_artist": recording.artist,
                    "user_scope_id": taste.user_scope_id,
                },
            )
        )
    for artist in anchor_artists:
        artist_mbid = _text(artist_ids.get(artist.casefold()))
        metadata = {
            "profile_seed_artist": artist,
            "radio_seed_key": next(
                (_text(seed.get("key")) for seed in (radio_discovery_artist_seeds or [])
                 if isinstance(seed, dict) and _text(seed.get("name")).casefold() == artist.casefold()),
                "",
            ),
            "musicbrainz_artist_id": artist_mbid,
            "provider_artist_id": next(
                (
                    _text(seed.get("provider_artist_id"))
                    for seed in (radio_discovery_artist_seeds or [])
                    if isinstance(seed, dict)
                    and _text(seed.get("name")).casefold() == artist.casefold()
                ),
                "",
            ),
            "radio_discovery_provenance": next(
                (
                    _text(seed.get("source"))
                    for seed in (radio_discovery_artist_seeds or [])
                    if isinstance(seed, dict)
                    and _text(seed.get("name")).casefold() == artist.casefold()
                ),
                "",
            ),
            "user_scope_id": taste.user_scope_id,
        }
        # Artist catalogue ownership is shared. Each provider publishes into
        # the same durable pool and fails independently; MusicBrainz is an
        # optional canonical contributor instead of a readiness gate.
        for provider in ("ytmusic", "lastfm", "listenbrainz"):
            if provider == "listenbrainz" and not artist_mbid:
                continue
            requests.append(
                EnrichmentRequest(
                    kind="artist_catalog",
                    provider=provider,
                    key=artist_mbid if provider == "listenbrainz" else artist,
                    pool="profile_spine",
                    relation="same_artist_catalog",
                    limit=16,
                    metadata={**metadata, "provider": provider},
                )
            )
        if radio_work_needed:
            for provider in ("ytmusic", "lastfm", "listenbrainz", "musicbrainz"):
                if provider in {"listenbrainz", "musicbrainz"} and not artist_mbid:
                    continue
                requests.append(
                    EnrichmentRequest(
                        kind="artist_catalog",
                        provider=provider,
                        key=(artist_mbid if provider in {"listenbrainz", "musicbrainz"} else artist),
                        pool="radio_artist_catalog",
                        relation="same_artist_catalog",
                        limit=24 if provider != "musicbrainz" else 12,
                        metadata={
                            **metadata,
                            "provider": provider,
                            "radio_seed_artist": artist,
                            "release_group_limit": 1,
                            "release_group_offset": _request_cursor(
                                request_progress,
                                kind="artist_catalog",
                                pool="radio_artist_catalog",
                                key=(artist_mbid if provider in {"listenbrainz", "musicbrainz"} else artist),
                            ),
                        },
                    )
                )
        requests.extend(
            EnrichmentRequest(
                kind="lastfm_artist_similar",
                key=artist_mbid or artist,
                pool="artist_graph",
                relation="artist_neighbor",
                limit=8,
                metadata={**metadata, "offset": neighbor_offset},
            )
            for neighbor_offset in (0, 3)
        )

    album_shortages = {
        str(key): int(value or 0)
        for key, value in dict(ledger.get("album_shelf_shortages") or {}).items()
    }
    album_reserve_shortage = int(ledger.get("qualified_album_reserve_shortage") or 0)
    album_work_needed = (
        not album_shortages
        or any(album_shortages.values())
        or album_reserve_shortage > 0
        or bool(demand_artists)
    )
    album_seed_diagnostics: Dict[str, int] = {}
    if album_work_needed:
        album_artist_counts = {
            normalize_artist_name(key): int(value or 0)
            for key, value in dict(ledger.get("album_artist_counts") or {}).items()
        }
        album_artist_evidence = {
            normalize_artist_name(key): value
            for key, value in dict(ledger.get('album_artist_evidence') or {}).items()
        }
        album_priority_artists = _unique(
            [*demand_artists, *list(ledger.get("album_priority_artist_seeds") or [])],
            limit=32,
        )
        album_priority_keys = {
            normalize_artist_name(artist)
            for artist in album_priority_artists
        }
        album_seed_artists = _unique(
            [
                *album_priority_artists,
                *rotated_artists,
                *list(ledger.get("album_expansion_artist_seeds") or []),
                *list(ledger.get("backbone_artist_seeds") or []),
            ],
            limit=48,
        )
        if ledger.get('album_profile_assessed'):
            supported_keys = {normalize_artist_name(value) for value in all_anchor_artists}
            evidence_keys = set(album_artist_evidence)
            album_seed_artists = [artist for artist in album_seed_artists
                if normalize_artist_name(artist) in supported_keys or normalize_artist_name(artist) in evidence_keys]
        seed_order = {
            normalize_artist_name(artist): index
            for index, artist in enumerate(album_seed_artists)
        }
        album_seed_artists.sort(
            key=lambda artist: (
                normalize_artist_name(artist) not in album_priority_keys,
                album_artist_counts.get(normalize_artist_name(artist), 0) >= 3,
                album_artist_counts.get(normalize_artist_name(artist), 0),
                seed_order.get(normalize_artist_name(artist), 0),
            )
        )
        # An empty shelf needs a bounded share of directly supported artists.
        # A long discovery-priority list otherwise occupies all 16 slots on
        # every pass, starving the listener's own artists indefinitely.
        supported = [
            artist for artist in album_seed_artists
            if normalize_artist_name(artist) not in {"", "unknown", "unknown artist"}
            and artist.casefold() in {value.casefold() for value in all_anchor_artists}
            and album_artist_counts.get(normalize_artist_name(artist), 0) < 3
        ]
        if any(album_shortages.values()) and supported:
            discovery = [artist for artist in album_seed_artists if artist not in supported]
            interleaved = []
            for index in range(max(len(discovery), len(supported))):
                if index < len(discovery):
                    interleaved.append(discovery[index])
                if index < len(supported):
                    interleaved.append(supported[index])
            album_seed_artists = interleaved
        selected_album_seeds = 0
        exhausted_album_seeds = 0
        for artist in album_seed_artists:
            if normalize_artist_name(artist) in {"", "unknown", "unknown artist"}:
                continue
            artist_key = " ".join(artist.casefold().split())
            artist_mbid = _text(artist_ids.get(artist_key))
            request_key = artist_mbid or artist
            base_metadata = {
                "profile_seed_artist": artist,
                "musicbrainz_artist_id": artist_mbid,
                "user_scope_id": taste.user_scope_id,
                "album_artist_qualified_count": album_artist_counts.get(
                    normalize_artist_name(artist),
                    0,
                ),
                **dict(album_artist_evidence.get(normalize_artist_name(artist)) or {}),
            }
            yt_progress = request_progress.get(f'album_catalog:ytmusic:v2:album:{artist.casefold()}') or {}
            artist_requests = [
                EnrichmentRequest(
                    kind="album_catalog",
                    provider="ytmusic",
                    key=artist,
                    pool="album",
                    relation="artist_album_catalog",
                    limit=3,
                    metadata={**base_metadata, "provider": "ytmusic",
                              'provider_artist_id': yt_progress.get('provider_artist_id') or base_metadata.get('provider_artist_id') or '',
                              'offset': 0 if yt_progress.get('exhausted') else int(yt_progress.get('cursor') or 0)},
                )
            ]
            if artist_mbid:
                artist_requests.append(
                    EnrichmentRequest(
                        kind="album_catalog",
                        provider="musicbrainz",
                        key=request_key,
                        pool="album",
                        relation="artist_album_catalog",
                        limit=1,
                        metadata={
                            **base_metadata,
                            "provider": "musicbrainz",
                            "release_group_offset": _request_cursor(
                                request_progress,
                                kind="album_catalog",
                                pool="album",
                                key=request_key,
                            ),
                        },
                    )
                )
            eligible_requests = []
            for request in artist_requests:
                progress = (request_progress.get(_request_progress_signature(request))
                            or request_progress.get(_request_signature(request)) or {})
                if request.provider == 'ytmusic' and float(progress.get('catalog_next_check_at') or 0) > time.time():
                    continue
                if (
                    progress.get("exhausted") is True
                    or _text(progress.get("state")).casefold() == "exhausted"
                ):
                    if not (request.provider == 'ytmusic' and
                            float(progress.get('catalog_next_check_at') or float('inf')) <= time.time()):
                        continue
                eligible_requests.append(request)
            if not eligible_requests:
                exhausted_album_seeds += 1
                continue
            # Apply the seed bound after terminal provider filtering. Exhausted
            # frontiers must not hide later profile artists on every pass.
            requests.extend(eligible_requests)
            selected_album_seeds += 1
        album_seed_diagnostics = {
            "offered_seed_count": len(album_seed_artists),
            "supported_seed_count": len(supported),
            "selected_seed_count": selected_album_seeds,
            "exhausted_seed_count": exhausted_album_seeds,
        }
    for genre in _profile_genres(taste):
        requests.append(
            EnrichmentRequest(
                kind="lastfm_tag_tracks",
                key=genre,
                pool="genre_mood",
                relation="structured_tag",
                limit=24,
                metadata={
                    "structured_tag": genre,
                    "user_scope_id": taste.user_scope_id,
                },
            )
        )
    username = _text(taste.listenbrainz_username)
    if username and taste.taste_mode in {"blended", "listenbrainz_first"}:
        requests.append(
            EnrichmentRequest(
                kind="listenbrainz_user_recommendations",
                key=username,
                pool="collaborative",
                relation="collaborative_neighbor",
                limit=60 if taste.taste_mode == "listenbrainz_first" else 30,
                metadata={
                    "taste_mode": taste.taste_mode,
                    "user_scope_id": taste.user_scope_id,
                    "offset": int(ledger.get("listenbrainz_user_offset") or 0),
                },
            )
        )

    failed_domains = set(str(value) for value in ledger.get("failed_domains") or [])
    reserve_shortages = {
        str(key): int(value or 0)
        for key, value in dict(ledger.get("row_reserve_shortages") or {}).items()
    }
    has_inventory_coverage = any(
        key in ledger
        for key in (
            "failed_domains",
            "optional_row_counts",
            "row_reserve_shortages",
        )
    )
    refresh_required = bool(ledger.get("candidate_refresh_required"))
    refresh_generation = _text(ledger.get("candidate_refresh_generation"))
    refresh_pools = {
        "similarity",
        "profile_spine",
        "artist_graph",
        "genre_mood",
        "collaborative",
        "popularity",
    }

    def needs_reserve(*domains: str) -> bool:
        return any(int(reserve_shortages.get(domain) or 0) > 0 for domain in domains)

    def targets_shortage(request: EnrichmentRequest) -> bool:
        if not has_inventory_coverage:
            return True
        # Coverage of evolving listening is independent of total item counts.
        # Only catalog/relationship requests for the uncovered seeds bypass
        # quantity gates; album/radio keep their separate acquisition owners.
        if (request.pool in {'profile_spine', 'artist_graph', 'album'}
                and normalize_artist_name(request.metadata.get('profile_seed_artist') or '')
                in demand_keys):
            return True
        # Freshness and quantity are independent contracts. An expired
        # inventory must execute discovery acquisition even when its retained
        # rows still satisfy the low-water marks. Album and radio acquisition
        # remain owned by their independent workers/contracts.
        if refresh_required and request.pool in refresh_pools:
            return True
        if request.kind == "album_catalog":
            # Optional album shortages are tracked separately from the core
            # feed contracts and still need guaranteed scheduler capacity.
            return album_work_needed
        if request.kind == "artist_catalog" and request.pool == "radio_artist_catalog":
            return "popular_radio" in failed_domains or (
                not failed_domains and radio_work_needed
            )
        if request.kind == "lastfm_artist_similar":
            return bool(album_shortages.get("featured_new_albums") or album_reserve_shortage) or bool(
                failed_domains
                & {"recommended_artists", "made_for_you_tracks", "quiet_picks"}
            ) or needs_reserve(
                "recommended_artists",
                "made_for_you_tracks",
                "quiet_picks",
            )
        if request.pool == "profile_spine":
            return bool(
                failed_domains
                & {"made_for_you_tracks", "recommended_artists", "popular_radio"}
            ) or needs_reserve(
                "made_for_you_tracks",
                "recommended_artists",
                "quiet_picks",
            )
        return bool(
            failed_domains
            & {"todays_pick", "because_you_played", "quiet_picks", "made_for_you_tracks"}
        ) or needs_reserve(
            "because_you_played",
            "quiet_picks",
            "made_for_you_tracks",
        )

    deduped: List[EnrichmentRequest] = []
    seen: set[str] = set()
    for request in requests:
        if allowed_pools is not None and request.pool not in allowed_pools:
            continue
        signature = _request_signature(request)
        progress = (
            request_progress.get(_request_progress_signature(request))
            or request_progress.get(signature)
            or {}
        )
        refresh_request = bool(
            refresh_required
            and refresh_generation
            and request.pool in refresh_pools
        )
        already_attempted_for_refresh = bool(
            refresh_request
            and _text(progress.get("refresh_generation")) == refresh_generation
        )
        if (
            signature in seen
            or already_attempted_for_refresh
            or (progress.get("exhausted") is True and not refresh_request
                and not (request.kind == 'album_catalog' and request.provider == 'ytmusic'
                         and float(progress.get('catalog_next_check_at') or float('inf')) <= time.time()))
            or not targets_shortage(request)
        ):
            continue
        seen.add(signature)
        if refresh_request:
            request = EnrichmentRequest(
                kind=request.kind,
                key=request.key,
                pool=request.pool,
                relation=request.relation,
                limit=request.limit,
                metadata={
                    **dict(request.metadata or {}),
                    "refresh_generation": refresh_generation,
                },
                provider=request.provider,
            )
        deduped.append(request)
    return CandidateEnrichmentPlan(
        user_scope_id=taste.user_scope_id,
        requests=deduped,
        anchor_track_count=len(anchor_tracks),
        anchor_artist_count=len(anchor_artists),
        anchor_cursor_start=cursor,
        anchor_cursor_next=cursor_next,
        artist_cursor_next=artist_cursor_next,
        radio_discovery_cursor_next=radio_discovery_cursor_next,
        prior_request_progress=request_progress,
        album_seed_diagnostics=album_seed_diagnostics,
        priority_pools=(
            ("album", "similarity", "artist_graph", "profile_spine")
            if int(ledger.get("album_unexposed_artist_deficit") or 0) > 0
            else ()
        ),
    )


def _load_job(server: Any, user_scope_id: str, request: EnrichmentRequest) -> Dict[str, Any] | None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [ACQUISITION_JOB_NAMESPACE, _job_key(user_scope_id, request)],
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        return dict(payload) if isinstance(payload, dict) else None
    except Exception:
        return None
    finally:
        connection.close()


def _store_job(
    server: Any,
    user_scope_id: str,
    request: EnrichmentRequest,
    payload: Dict[str, Any],
) -> None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    now = time.time()
    value = {
        **dict(payload),
        "request_signature": _request_signature(request),
        "kind": request.kind,
        "provider": _request_provider(request),
        "pool": request.pool,
        "key": request.key,
        "updated_at": now,
        # Successful canonical pages are stable and expensive to reacquire;
        # retain them for 90 days. Pending/retryable failures keep the normal
        # short acquisition TTL so a stuck job cannot become permanent.
        "expires_at": now + (
            ALBUM_CATALOG_CHECK_SECONDS
            if (payload.get('state') in {'resolved', 'exhausted'} and
                (request.kind == 'album_catalog_index' or (request.kind == 'album_catalog' and request.provider == 'ytmusic')))
            else
            MUSICBRAINZ_RESULT_TTL_SECONDS
            if _is_musicbrainz_request(request)
            and str(payload.get("state") or "").casefold() in {"resolved", "exhausted"}
            else ACQUISITION_RESULT_TTL_SECONDS
        ),
    }
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    try:
        connection.execute(
            """
            INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id = excluded.model_id,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            [
                ACQUISITION_JOB_NAMESPACE,
                _job_key(user_scope_id, request),
                ACQUISITION_JOB_MODEL,
                json.dumps(value, ensure_ascii=False),
                now,
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _musicbrainz_acquisition_client(server: Any) -> Any:
    configured = getattr(server, "musicbrainz_client", None)
    if configured is not None:
        return configured
    from ..search.musicbrainz import (
        MUSICBRAINZ_ACQUISITION_TIMEOUT_SECONDS,
        MusicBrainzClient,
    )

    return MusicBrainzClient(
        timeout_seconds=MUSICBRAINZ_ACQUISITION_TIMEOUT_SECONDS
    )


def _resolve_artist_mbid(server: Any, artist: str, artist_mbid: str = "") -> str:
    if _text(artist_mbid):
        return _text(artist_mbid)
    client = _musicbrainz_acquisition_client(server)
    matches = client.search_artists(artist, limit=5)
    wanted = " ".join(_text(artist).casefold().split())
    for match in matches:
        names = {
            " ".join(_text(match.get("name")).casefold().split()),
            " ".join(_text(match.get("sort-name")).casefold().split()),
        }
        names.update(
            " ".join(_text(alias.get("name") or alias.get("sort-name")).casefold().split())
            for alias in match.get("aliases") or []
            if isinstance(alias, dict)
        )
        if wanted in names:
            return _text(match.get("id"))
    return ""


def _canonicalize_recording(server: Any, item: Dict[str, Any]) -> Dict[str, Any] | None:
    recording_mbid = _text(item.get("musicbrainz_recording_id") or item.get("recording_mbid"))
    if recording_mbid:
        if _text(item.get("title") or item.get("recording_name")) and (
            _artist(item) or _text(item.get("artist_name"))
        ):
            return {
                **item,
                "musicbrainz_recording_id": recording_mbid,
            }
        from ..search.musicbrainz import MusicBrainzClient, musicbrainz_recording_to_item

        client = getattr(server, "musicbrainz_client", None) or MusicBrainzClient()
        matches = client.search_recordings(f"rid:{recording_mbid}", limit=1)
        if not matches:
            return None
        return {
            **musicbrainz_recording_to_item(matches[0]),
            **item,
            "musicbrainz_recording_id": recording_mbid,
        }
    title = _text(item.get("title") or item.get("recording_name"))
    artist = _artist(item) or _text(item.get("artist_name"))
    if not title or not artist:
        return None
    recording = CanonicalRecording.from_item({**item, "title": title, "artist": artist})
    return {
        **item,
        "title": title,
        "artist": artist,
        "track_key": recording.track_key,
    }


def _canonical_recording_rows(
    server: Any,
    rows: Iterable[Dict[str, Any]],
    *,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        canonical = _canonicalize_recording(server, raw)
        if canonical is None:
            continue
        identity = _track_id(canonical)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        output.append(
            {
                **canonical,
                **dict(request.metadata),
                "materialized_pool": request.pool,
                "materialized_relation": request.relation,
                "recommendation_path": request.relation,
                "relationship_provider": raw.get("relationship_provider"),
                "relationship_evidence": raw.get("relationship_evidence"),
                "relationship_score": raw.get("relationship_score"),
                "source_provenance": (
                    f"structured:{request.kind}:{_request_provider(request)}"
                ),
            }
        )
        if len(output) >= request.limit:
            break
    return output


def _album_relationship_metadata(request: EnrichmentRequest) -> Dict[str, Any]:
    return {key:value for key,value in request.metadata.items() if key in {
        'related_to_artist','artist_graph_keys','artist_neighborhood','relation_strength',
        'recommendation_path','provider_artist_id','musicbrainz_artist_id','genres',
        'discovery_genres','album_relation_origin','relationship_provider','relationship_evidence'}}


def _resolve_musicbrainz_album_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    from ..search.musicbrainz import (
        browse_musicbrainz_artist_album_items,
        browse_musicbrainz_release_group_tracks,
    )

    artist = _text(request.metadata.get("profile_seed_artist"))
    artist_mbid = _resolve_artist_mbid(
        server,
        artist,
        _text(request.metadata.get("musicbrainz_artist_id")),
    )
    client = _musicbrainz_acquisition_client(server)
    albums = browse_musicbrainz_artist_album_items(
        artist,
        artist_id=artist_mbid,
        client=client,
        limit=request.limit,
        offset=max(int(request.metadata.get("release_group_offset") or 0), 0),
        raise_errors=True,
    )
    output: List[Dict[str, Any]] = []
    for album in albums:
        release_group_id = _text(album.get("musicbrainz_release_group_id"))
        canonical_tracks = browse_musicbrainz_release_group_tracks(
            release_group_id,
            client=client,
            limit=40,
            raise_errors=True,
        )
        if not canonical_tracks:
            continue
        canonical_tracks = _canonical_recording_rows(
            server,
            canonical_tracks,
            request=EnrichmentRequest(
                kind="canonical_album_track",
                key=release_group_id,
                pool="album_tracks",
                relation="same_album",
                limit=len(canonical_tracks),
                metadata=request.metadata,
            ),
        )
        if not canonical_tracks:
            continue
        album_candidate = {
                **album,
                **_album_relationship_metadata(request),
                "id": f"musicbrainz:release-group:{release_group_id}",
                "browseId": f"musicbrainz:release-group:{release_group_id}",
                "canonical_album_identity": (
                    f"musicbrainz:release-group:{release_group_id}"
                ),
                "musicbrainz_artist_id": artist_mbid,
                "canonical_tracks": canonical_tracks,
                "track_count": len(canonical_tracks),
                "canonical_track_count": len(canonical_tracks),
                "playable_coverage": 0.0,
                "playable": False,
                "source": "musicbrainz",
                "source_provider": "musicbrainz",
                "source_authority": "canonical",
                "source_identity_authority": "verified_catalog",
                "album_source": "artist_catalog",
                "relationship_provider": "musicbrainz",
                "relationship_evidence": "canonical_artist_discography",
                "source_provenance": "structured:album_catalog:musicbrainz",
            }
        output.append(album_candidate)
    return output


def _resolve_musicbrainz_artist_catalog_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    from ..search.musicbrainz import (
        browse_musicbrainz_artist_album_items,
        browse_musicbrainz_release_group_tracks,
    )

    artist = _text(request.metadata.get("profile_seed_artist"))
    artist_mbid = _resolve_artist_mbid(
        server,
        artist,
        _text(request.metadata.get("musicbrainz_artist_id")),
    )
    if not artist_mbid:
        return []
    client = _musicbrainz_acquisition_client(server)
    albums = browse_musicbrainz_artist_album_items(
        artist,
        artist_id=artist_mbid,
        client=client,
        limit=max(int(request.metadata.get("release_group_limit") or 4), 1),
        offset=max(int(request.metadata.get("release_group_offset") or 0), 0),
        raise_errors=True,
    )
    rows: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    # The worker qualifies a seed at the publish minimum.  Provider pages can
    # be noisy, so continue scanning until twelve accepted rows, but never
    # turn one seed into a 24/27-track deepening job.
    qualification_limit = min(12, request.limit)
    direct_limit = qualification_limit
    for album in albums:
        release_group_id = _text(album.get("musicbrainz_release_group_id"))
        for track in browse_musicbrainz_release_group_tracks(
            release_group_id,
            client=client,
            limit=40,
            raise_errors=True,
        ):
            # The requested canonical artist is authoritative.  A release
            # browse can include guest/various-artist recordings; only accept
            # a track when its credited artist relationship contains the
            # requested MusicBrainz identity (name matching is a last resort
            # for providers that omit IDs).
            requested_mbid = _text(artist_mbid).removeprefix(
                "musicbrainz:artist:"
            ).casefold()
            credited_ids = {
                _text(value)
                .removeprefix("musicbrainz:artist:")
                .casefold()
                for value in (
                    track.get("musicbrainz_artist_id"),
                    track.get("artist_mbid"),
                )
                if _text(value)
            }
            credited_names = {
                _artist(track).casefold(),
                *(
                    (_artist(value) if isinstance(value, dict) else _text(value))
                    .casefold()
                    for value in (track.get("artists") or [])
                    if (_artist(value) if isinstance(value, dict) else _text(value))
                ),
            }
            if credited_ids and requested_mbid not in credited_ids:
                continue
            if not credited_ids and artist.casefold() not in credited_names:
                continue
            semantic = "|".join(
                (
                    _text(track.get("title")).casefold(),
                    _artist(track).casefold(),
                )
            )
            if not semantic.strip("|") or semantic in seen_titles:
                continue
            seen_titles.add(semantic)
            rows.append(
                {
                    **track,
                    "related_to_artist": artist,
                    "radio_seed_artist": artist,
                    "musicbrainz_artist_id": artist_mbid,
                    "radio_discovery_provenance": _text(
                        request.metadata.get("radio_discovery_provenance")
                    ),
                    "relationship_provider": "musicbrainz",
                    "relationship_evidence": "canonical_artist_discography",
                    "relationship_score": 1.0,
                }
            )
            if len(rows) >= direct_limit:
                break
        if len(rows) >= direct_limit:
            break
    return _canonical_recording_rows(server, rows, request=request)


def _provider_thumbnail(item: Dict[str, Any]) -> str:
    thumbnails = item.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for value in reversed(thumbnails):
            if isinstance(value, dict) and _text(value.get("url")):
                return _text(value.get("url"))
    return _text(item.get("thumbnail") or item.get("image"))


def _provider_artist_names(item: Dict[str, Any]) -> List[str]:
    values = [_artist(item), _text(item.get("artist_name"))]
    for value in item.get("artists") or []:
        values.append(_artist(value) if isinstance(value, dict) else _text(value))
    return _unique(values, limit=8)


def _artist_catalog_match(item: Dict[str, Any], wanted_artist: str) -> bool:
    wanted = " ".join(_text(wanted_artist).casefold().split())
    return bool(
        wanted
        and wanted
        in {
            " ".join(value.casefold().split())
            for value in _provider_artist_names(item)
            if value
        }
    )


def _ytmusic_artist_metadata(server: Any, artist: str) -> Dict[str, Any]:
    """Resolve an exact YTMusic artist entity, never a track thumbnail."""

    client = getattr(server, "ytmusic", None)
    wanted = " ".join(_text(artist).casefold().split())
    if client is None or not wanted:
        return {}
    try:
        results = client.search(artist, filter="artists", limit=8) or []
    except Exception:
        return {}
    match = next(
        (
            row
            for row in results
            if isinstance(row, dict)
            and " ".join(_text(row.get("artist") or row.get("name")).casefold().split())
            == wanted
        ),
        None,
    )
    if not isinstance(match, dict):
        return {}
    artist_id = _text(match.get("browseId") or match.get("id"))
    detail: Dict[str, Any] = {}
    if artist_id and hasattr(client, "get_artist"):
        try:
            detail = client.get_artist(artist_id) or {}
        except Exception:
            detail = {}
    thumbnails: List[str] = []
    for source in (match, detail):
        for value in source.get("thumbnails") or []:
            if isinstance(value, dict) and _text(value.get("url")):
                thumbnails.append(_text(value.get("url")))
        thumbnail = _provider_thumbnail(source)
        if thumbnail:
            thumbnails.append(thumbnail)
    thumbnails = list(dict.fromkeys(thumbnails))
    return {
        "name": _text(match.get("artist") or match.get("name") or artist),
        "provider_artist_id": artist_id,
        "canonical_artist_id": (
            f"provider:artist:{artist_id.casefold()}" if artist_id else ""
        ),
        "artist_thumbnail": thumbnails[-1] if thumbnails else "",
        "artwork_source_url": thumbnails[-1] if thumbnails else "",
        "artwork_source_urls": list(reversed(thumbnails)),
        "artwork_provider": "ytmusic",
    }


def _resolve_ytmusic_artist_catalog_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    client = getattr(server, "ytmusic", None)
    artist = _text(request.metadata.get("profile_seed_artist") or request.key)
    if client is None or not artist:
        return []
    artist_metadata = _ytmusic_artist_metadata(server, artist)
    rows = client.search(
        f"{artist} songs",
        filter="songs",
        limit=max(min(int(request.limit) * 2, 48), int(request.limit)),
    ) or []
    output: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not _artist_catalog_match(row, artist):
            continue
        video_id = _text(row.get("videoId") or row.get("id"))
        title = _text(row.get("title") or row.get("name"))
        if not video_id or not title:
            continue
        album = row.get("album") if isinstance(row.get("album"), dict) else {}
        track_artist_metadata = {
            key: value
            for key, value in artist_metadata.items()
            if value and key not in {
                "artwork_provider",
                "artwork_source_url",
                "artwork_source_urls",
            }
        }
        output.append(
            {
                **row,
                **track_artist_metadata,
                "id": video_id,
                "videoId": video_id,
                "title": title,
                "artist": artist,
                "artist_id": artist_metadata.get("provider_artist_id") or next(
                    (
                        _text(value.get("id") or value.get("browseId"))
                        for value in row.get("artists") or []
                        if isinstance(value, dict)
                        and _text(value.get("id") or value.get("browseId"))
                    ),
                    "",
                ),
                "album": _text(album.get("name") or album.get("title")),
                "album_id": _text(album.get("id") or album.get("browseId")),
                "thumbnail": _provider_thumbnail(row),
                "provider": "ytmusic",
                "source_provider": "ytmusic",
                "source_identity_authority": "verified_catalog",
                "relationship_provider": "ytmusic",
                "relationship_evidence": "artist_catalog",
                "relationship_score": 1.0,
            }
        )
        if len(output) >= int(request.limit):
            break
    return _canonical_recording_rows(server, output, request=request)


class AlbumCatalogIdentityError(RuntimeError):
    transient = False

    def __init__(self, code):
        super().__init__(code)
        self.cause_type = code


class AlbumCatalogPage(list):
    """Provider page progress travels with results, including empty pages."""
    def __init__(self, items, *, cursor, exhausted, artist_id, bounded=False):
        super().__init__(items)
        self.cursor = cursor
        self.exhausted = exhausted
        self.artist_id = artist_id
        self.bounded = bounded


class ArtistGraphPage(list):
    """Recording results and separate, non-playable artist relationships."""
    def __init__(self, items, *, artist_edges):
        super().__init__(items)
        self.artist_edges = artist_edges


def _album_catalog_entries(server: Any, request: EnrichmentRequest):
    client = server.ytmusic
    artist = _text(request.metadata.get('profile_seed_artist') or request.key)
    identity_request = EnrichmentRequest('album_artist_identity', artist, 'album', 'artist_identity', 1, provider='ytmusic')
    identity = _load_job(server, 'catalog', identity_request) or {}
    artist_id = _text(request.metadata.get('provider_artist_id'))
    if not artist_id and float(identity.get('expires_at') or 0) > time.time():
        artist_id = _text(identity.get('provider_artist_id'))
    if not artist_id:
        normalized_artist = normalize_artist_name(artist)
        matches = {
            _text(row.get('browseId') or row.get('id'))
            for row in client.search(artist, filter='artists', limit=8) or []
            if (_text(row.get('artist') or row.get('name')).casefold() == artist.casefold()
                or (normalized_artist and
                    normalize_artist_name(row.get('artist') or row.get('name')) == normalized_artist))
            and _text(row.get('browseId') or row.get('id')).startswith('UC')
        }
        if len(matches) != 1:
            raise AlbumCatalogIdentityError('album_artist_identity_ambiguous' if matches else 'album_artist_identity_missing')
        artist_id = matches.pop()
        _store_job(server, 'catalog', identity_request, {'state':'resolved','provider_artist_id':artist_id})
    if not artist_id.startswith('UC'):
        raise AlbumCatalogIdentityError('album_artist_identity_invalid')
    index_request = EnrichmentRequest('album_catalog_index', artist_id, 'album', 'artist_discography', 1, provider='ytmusic')
    index = _load_job(server, 'catalog', index_request) or {}
    offset = max(int(request.metadata.get('offset') or 0), 0)
    needed = offset + int(request.limit) + 1
    cards = index.get('results') or []
    if (float(index.get('expires_at') or 0) <= time.time()
        or (not index.get('complete') and needed > len(cards))):
        detail = client.get_artist(artist_id) or {}
        section = detail.get('albums') or {}
        bound = min(max(needed, 48), ALBUM_CATALOG_MAX_CARDS)
        rows = (client.get_artist_albums(section.get('browseId') or artist_id, section['params'], limit=bound)
                if section.get('params') else section.get('results') or [])
        # Persist listing facts only; page size is an explicit hard bound even
        # when ytmusicapi returns an oversized provider page.
        unique = {}
        for row in rows or []:
            key = _text(row.get('browseId') or row.get('id'))
            if key and key not in unique:
                unique[key] = {k:row[k] for k in ['browseId','id','title','year','type','thumbnails'] if k in row}
        cards = list(unique.values())[:ALBUM_CATALOG_MAX_CARDS]
        index = {'state':'resolved','results':cards,
                 'complete':not section.get('params') or len(rows or []) < bound}
        _store_job(server, 'catalog', index_request, index)
    page = cards[offset:offset + int(request.limit)]
    cursor = offset + len(page)
    exhausted = bool(index.get('complete') and cursor >= len(cards))
    bounded = cursor >= ALBUM_CATALOG_MAX_CARDS and not index.get('complete')
    return artist_id, page, (0 if bounded else cursor), exhausted, bounded


def _resolve_ytmusic_album_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    client = getattr(server, "ytmusic", None)
    artist = _text(request.metadata.get("profile_seed_artist") or request.key)
    if client is None or not artist:
        return []
    artist_id, results, cursor, exhausted, bounded = _album_catalog_entries(server, request)
    output: List[Dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        browse_id = _text(row.get("browseId") or row.get("id"))
        title = _text(row.get("title") or row.get("name"))
        if not browse_id or not title:
            continue
        detail_request = EnrichmentRequest('album_detail', browse_id, 'album', 'album_tracks', 1, provider='ytmusic')
        cached_detail = _load_job(server, 'catalog', detail_request) or {}
        if float(cached_detail.get('expires_at') or 0) > time.time():
            detail = cached_detail.get('detail') or {}
        else:
            detail = client.get_album(browse_id) or {}
            if not detail.get('tracks'):
                raise RuntimeError('album_catalog_detail_incomplete')
            _store_job(server, 'catalog', detail_request, {'state':'resolved','detail':detail})
        raw_tracks = detail.get("tracks") or []
        tracks: List[Dict[str, Any]] = []
        for track in raw_tracks:
            if not isinstance(track, dict):
                continue
            track_artist = artist if _artist_catalog_match(track, artist) else _artist(track)
            video_id = _text(track.get("videoId") or track.get("id"))
            track_title = _text(track.get("title") or track.get("name"))
            if not track_title or not track_artist:
                continue
            tracks.append(
                {
                    **track,
                    "id": video_id,
                    "videoId": video_id,
                    "title": track_title,
                    "artist": track_artist,
                    "album": title,
                    "album_id": browse_id,
                    "provider": "ytmusic",
                    "source_provider": "ytmusic",
                    "source_identity_authority": "verified_catalog",
                    "relationship_provider": "ytmusic",
                    "relationship_evidence": "artist_album_catalog",
                }
            )
        canonical_tracks = _canonical_recording_rows(
            server,
            tracks,
            request=EnrichmentRequest(
                kind="album_track",
                provider="ytmusic",
                key=browse_id,
                pool="album_tracks",
                relation="same_album",
                limit=len(tracks),
                metadata=request.metadata,
            ),
        )
        if len(canonical_tracks) < 2:
            continue
        output.append(
            {
                **row,
                **_album_relationship_metadata(request),
                "id": f"ytmusic:album:{browse_id}",
                "browseId": browse_id,
                "album_id": browse_id,
                "canonical_album_identity": f"ytmusic:album:{browse_id}",
                "title": title,
                "artist": _artist(detail) or artist,
                'artists': detail.get('artists') or [],
                'catalog_artist_id': artist_id,
                'artist_graph_keys': list(dict.fromkeys([*(request.metadata.get('artist_graph_keys') or []), artist])),
                'provider_artist_id': next((_text(credit.get('id') or credit.get('browseId'))
                    for credit in detail.get('artists') or [] if isinstance(credit,dict)
                    and _text(credit.get('id') or credit.get('browseId'))),
                    artist_id if not _artist(detail) or _artist(detail).casefold()==artist.casefold() else ''),
                "thumbnail": _provider_thumbnail(detail or row),
                "release_year": _text(
                    detail.get("year") or row.get("year") or row.get("release_year")
                ),
                "canonical_tracks": canonical_tracks,
                "track_count": len(canonical_tracks),
                "canonical_track_count": max(len(canonical_tracks), len(raw_tracks)),
                "playable_coverage": 0.0,
                "playable": False,
                "source": "ytmusic",
                "source_provider": "ytmusic",
                "source_authority": "verified_catalog",
                "source_identity_authority": "verified_catalog",
                "album_source": "artist_catalog",
                "relationship_provider": "ytmusic",
                "relationship_evidence": "artist_album_catalog",
                "source_provenance": "structured:album_catalog:ytmusic",
            }
        )
    return AlbumCatalogPage(output, cursor=cursor, exhausted=exhausted, artist_id=artist_id, bounded=bounded)


def _resolve_artist_catalog_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    provider = _request_provider(request)
    artist = _text(request.metadata.get("profile_seed_artist") or request.key)
    artist_mbid = _text(request.metadata.get("musicbrainz_artist_id"))
    if provider == "ytmusic":
        return _resolve_ytmusic_artist_catalog_rows(server, request)
    if provider == "lastfm":
        client = LastFmClient(server)
        artist_metadata = client.artist_info(artist, artist_mbid=artist_mbid)
        rows = client.top_tracks(
            artist,
            artist_mbid=artist_mbid,
            limit=request.limit,
        )
        rows = [
            {**row, **{key: value for key, value in artist_metadata.items() if value}}
            for row in rows
        ]
        return _canonical_recording_rows(server, rows, request=request)
    if provider == "listenbrainz":
        if not artist_mbid:
            return []
        rows = ListenBrainzClient(server).top_recordings(
            artist_mbid,
            limit=request.limit,
        )
        return _canonical_recording_rows(server, rows, request=request)
    if provider == "musicbrainz":
        return _resolve_musicbrainz_artist_catalog_rows(server, request)
    raise RuntimeError(f"unsupported_artist_catalog_provider:{provider}")


def _resolve_album_catalog_rows(
    server: Any,
    request: EnrichmentRequest,
) -> List[Dict[str, Any]]:
    provider = _request_provider(request)
    if provider == "ytmusic":
        return _resolve_ytmusic_album_rows(server, request)
    if provider == "musicbrainz":
        return _resolve_musicbrainz_album_rows(server, request)
    raise RuntimeError(f"unsupported_album_catalog_provider:{provider}")


def _fetch_request(server: Any, request: EnrichmentRequest) -> List[Dict[str, Any]]:
    artist = _text(request.metadata.get("profile_seed_artist"))
    artist_mbid_hint = _text(request.metadata.get("musicbrainz_artist_id"))
    if request.kind == "lastfm_track_similar":
        seed = CanonicalRecording.from_item(dict(request.metadata.get("canonical_track") or {}))
        rows = LastFmClient(server).similar_tracks(seed, limit=request.limit)
        return _canonical_recording_rows(server, rows, request=request)
    if request.kind == "listenbrainz_sitewide_recordings":
        rows = ListenBrainzClient(server).sitewide_recordings(
            limit=request.limit,
            offset=int(request.metadata.get("offset") or 0),
        )
        return _canonical_recording_rows(server, rows, request=request)
    if request.kind == "lastfm_artist_similar":
        neighbor_offset = max(int(request.metadata.get("offset") or 0), 0)
        neighbor_batch_size = 3
        neighbors = LastFmClient(server).similar_artists(
            artist,
            artist_mbid=artist_mbid_hint,
            limit=neighbor_offset + neighbor_batch_size,
        )[neighbor_offset : neighbor_offset + neighbor_batch_size]
        rows: List[Dict[str, Any]] = []
        artist_edges = []
        per_artist = max(2, min(6, (request.limit + max(len(neighbors), 1) - 1) // max(len(neighbors), 1)))
        listenbrainz = ListenBrainzClient(server)
        for neighbor in neighbors:
            neighbor_name = _artist(neighbor)
            if not neighbor_name:
                continue
            neighbor_mbid = _resolve_artist_mbid(
                server,
                neighbor_name,
                _text(neighbor.get("musicbrainz_artist_id")),
            )
            artist_edges.append({
                "artist": neighbor_name,
                "musicbrainz_artist_id": neighbor_mbid,
                "related_to_artist": artist,
                "relationship_score": neighbor.get("relationship_score"),
                "relationship_provider": "lastfm",
                "relationship_evidence": "artist_similarity",
            })
            if not neighbor_mbid:
                continue
            for row in listenbrainz.top_recordings(neighbor_mbid, limit=per_artist):
                rows.append(
                    {
                        **row,
                        "related_to_artist": artist,
                        "neighbor_artist": neighbor_name,
                        "relationship_score": neighbor.get("relationship_score"),
                        "relationship_provider": "lastfm+listenbrainz",
                        "relationship_evidence": "artist_similarity_catalog",
                    }
                )
        return ArtistGraphPage(
            _canonical_recording_rows(server, rows, request=request),
            artist_edges=artist_edges,
        )
    if request.kind == "lastfm_tag_tracks":
        rows = LastFmClient(server).tag_tracks(request.key, limit=request.limit)
        return _canonical_recording_rows(server, rows, request=request)
    if request.kind == "listenbrainz_user_recommendations":
        listenbrainz = ListenBrainzClient(server)
        payload = listenbrainz.get(
            f"https://api.listenbrainz.org/1/cf/recommendation/user/{request.key}/recording",
            params={
                "count": request.limit,
                "offset": int(request.metadata.get("offset") or 0),
            },
        )
        rows = [
            {
                "musicbrainz_recording_id": value.get("recording_mbid"),
                "relationship_score": value.get("score"),
                "relationship_provider": "listenbrainz",
                "relationship_evidence": "personal_collaborative",
            }
            for value in (payload.get("payload") or {}).get("mbids") or []
            if isinstance(value, dict)
        ]
        metadata = listenbrainz.recording_metadata(
            row.get("musicbrainz_recording_id") for row in rows
        )
        hydrated = [
            {**metadata[mbid], **row}
            for row in rows
            if (mbid := _text(row.get("musicbrainz_recording_id"))) in metadata
        ]
        return _canonical_recording_rows(server, hydrated, request=request)
    if request.kind == "album_catalog":
        return _resolve_album_catalog_rows(server, request)
    if request.kind == "artist_catalog":
        return _resolve_artist_catalog_rows(server, request)
    raise RuntimeError(f"unsupported_structured_request:{request.kind}")


def materialize_enrichment_plan(
    server: Any,
    plan: CandidateEnrichmentPlan,
    *,
    time_budget_seconds: float | None = None,
    max_workers: int = 8,
    max_pending_jobs: int | None = None,
) -> MaterializedCandidateSupply:
    started = time.perf_counter()
    pools: Dict[str, List[Dict[str, Any]]] = {}
    failures: Dict[str, str] = {}
    request_progress = {
        str(key): dict(value)
        for key, value in dict(plan.prior_request_progress or {}).items()
        if isinstance(value, dict)
    }
    pending: List[EnrichmentRequest] = []
    completed = 0
    cached = 0
    retry_deferred = 0
    exhausted = 0
    all_requests = plan.requests
    for request in all_requests:
        job = _load_job(server, plan.user_scope_id, request)
        refresh_generation = _text(request.metadata.get("refresh_generation"))
        cached_refresh_generation = _text(
            (job or {}).get("refresh_generation")
        )
        bypass_prior_generation = bool(
            refresh_generation
            and refresh_generation != cached_refresh_generation
        )
        if (
            job
            and not bypass_prior_generation
            and float(job.get("expires_at") or 0) > time.time()
        ):
            state = _text(job.get("state"))
            if request.kind == 'lastfm_artist_similar' and state in {'resolved', 'exhausted'} and job.get('artist_edges'):
                progress_signature = _request_progress_signature(request)
                request_progress[progress_signature] = {
                    **dict(request_progress.get(progress_signature) or {}), **job,
                }
                results = [dict(value) for value in job.get('results') or [] if isinstance(value, dict)]
                if results:
                    pools.setdefault(request.pool, []).extend(results)
                cached += 1
                completed += 1
                continue
            if state == "retryable":
                progress_signature = _request_progress_signature(request)
                prior_progress = dict(request_progress.get(progress_signature) or {})
                request_progress[progress_signature] = {
                    **prior_progress,
                    **{
                        key: job.get(key)
                        for key in (
                            "state",
                            "attempts",
                            "retry_at",
                            "provider",
                            "provider_retry_at",
                            "last_error",
                            "http_status",
                            "request_phase",
                            "provider_elapsed_ms",
                        )
                        if job.get(key) is not None
                    },
                }
            if state == "resolved":
                progress_signature = _request_progress_signature(request)
                prior_progress = request_progress.get(progress_signature) or {}
                if int(job.get('cursor') or 0) >= int(prior_progress.get('cursor') or 0):
                    request_progress[progress_signature] = {**prior_progress, **job}
                results = [dict(value) for value in job.get("results") or [] if isinstance(value, dict)]
                if results:
                    pools.setdefault(request.pool, []).extend(results)
                    cached += 1
                    completed += 1
                    continue
                if job.get("exhausted") is True:
                    completed += 1
                    exhausted += 1
                    continue
            if state == "exhausted":
                completed += 1
                exhausted += 1
                continue
            if (
                state == "retryable"
                and float(job.get("retry_at") or 0) > time.time()
                and str(job.get("last_error") or "")
                != "AcquisitionDeadlineExceeded"
            ):
                retry_deferred += 1
                continue
        pending.append(request)

    pending_total = len(pending)
    # Every provider has one persisted circuit/backoff shared by its jobs.
    # A failing provider is removed from this batch while healthy providers
    # continue; MusicBrainz additionally remains capped to one request.
    now = time.time()
    providers = {
        _request_provider(request) for request in pending if _request_provider(request)
    }
    provider_retry_at = {
        provider: _provider_shared_retry_at(request_progress, provider)
        for provider in providers
    }
    pending = [
        request
        for request in pending
        if provider_retry_at.get(_request_provider(request), 0.0) <= now
    ]
    provider_deferred = pending_total - len(pending)
    # Keep at most 16 eligible album artists after durable job and provider
    # cooldown inspection, so cooled seeds do not hide the later frontier.
    pending_album_seeds = set()
    bounded_pending = []
    for request in pending:
        if request.kind == "album_catalog":
            seed = normalize_artist_name(request.metadata.get("profile_seed_artist"))
            if seed not in pending_album_seeds and len(pending_album_seeds) >= 16:
                continue
            pending_album_seeds.add(seed)
        bounded_pending.append(request)
    pending = bounded_pending
    regular_pending = [
        request
        for request in pending
        if not _is_musicbrainz_request(request)
    ]
    canonical_pending = [
        request
        for request in pending
        if _is_musicbrainz_request(request)
    ]
    canonical_pending.sort(
        key=lambda request: int(
            (
                request_progress.get(_request_progress_signature(request))
                or request_progress.get(_request_signature(request))
                or {}
            ).get("attempts")
            or 0
        )
    )
    pending = [*regular_pending, *canonical_pending]
    if max_pending_jobs is None:
        scheduled = pending
    else:
        scheduled = _select_scheduled_requests(
            pending,
            max(int(max_pending_jobs), 0),
            priority_pools=tuple(plan.priority_pools or ()),
        )
    deferred_count = pending_total - len(scheduled)
    print(
        "[EBB:acquisition] "
        f"planned={len(all_requests)} cached={cached} "
        f"scheduled={len(scheduled)} deferred={deferred_count} "
        f"pools={','.join(request.pool for request in scheduled)} "
        f"providers={','.join(_request_provider(request) for request in scheduled)}"
    )

    for request in scheduled:
        signature = _request_signature(request)
        progress_signature = _request_progress_signature(request)
        prior = dict(
            request_progress.get(progress_signature)
            or request_progress.get(signature)
            or {}
        )
        pending_progress = {
            **prior,
            "state": "pending",
            "queued_at": time.time(),
            "refresh_generation": _text(
                request.metadata.get("refresh_generation")
            ),
        }
        request_progress[progress_signature] = pending_progress
        _store_job(server, plan.user_scope_id, request, pending_progress)

    deadline = (
        started + max(float(time_budget_seconds), 1.0)
        if time_budget_seconds
        else None
    )
    executor = ThreadPoolExecutor(
        max_workers=max(min(int(max_workers or 1), 8), 1),
        thread_name_prefix="auralis-structured-acquisition",
    )
    futures: Dict[Future[List[Dict[str, Any]]], EnrichmentRequest] = {
        executor.submit(_fetch_request, server, request): request
        for request in scheduled
    }
    repeated_batch_count = 0
    timed_out_request_count = 0
    seen_by_pool: Dict[str, set[str]] = {
        pool: {_track_id(item) for item in values if _track_id(item)}
        for pool, values in pools.items()
    }
    for request in all_requests:
        progress = (
            request_progress.get(_request_progress_signature(request))
            or request_progress.get(_request_signature(request))
            or {}
        )
        seen_by_pool.setdefault(request.pool, set()).update(
            _text(identity)
            for identity in progress.get("returned_identities") or []
            if _text(identity)
        )
    try:
        completion_timeout = (
            max(deadline - time.perf_counter(), 0.001)
            if deadline is not None
            else None
        )
        for future in as_completed(futures, timeout=completion_timeout):
            request = futures[future]
            signature = _request_signature(request)
            progress_signature = _request_progress_signature(request)
            prior = dict(
                request_progress.get(progress_signature)
                or request_progress.get(signature)
                or {}
            )
            try:
                results = future.result()
            except Exception as exc:
                details = _provider_failure_details(exc)
                failure_name = str(details.get("last_error") or type(exc).__name__)
                failures[
                    f"{request.kind}:{_request_provider(request)}:{request.key}"
                ] = failure_name
                attempts = int(prior.get("attempts") or 0) + 1
                is_musicbrainz = _is_musicbrainz_request(request)
                provider_name = _request_provider(request)
                transient = bool(details.get("transient"))
                retry_delay = _provider_retry_delay_seconds(
                    details,
                    attempts,
                    provider=provider_name,
                )
                retry_at = time.time() + retry_delay
                request_progress[progress_signature] = {
                    **prior,
                    "state": (
                        "exhausted"
                        if is_musicbrainz and not transient
                        else "retryable"
                    ),
                    "attempts": attempts,
                    "retry_at": retry_at,
                    "provider": provider_name,
                    "provider_retry_at": retry_at if transient else 0.0,
                    "last_error": failure_name,
                    "http_status": int(details.get("http_status") or 0),
                    "request_phase": str(details.get("request_phase") or ""),
                    "provider_elapsed_ms": int(
                        details.get("provider_elapsed_ms") or 0
                    ),
                    "exhausted": bool(is_musicbrainz and not transient),
                }
                _store_job(
                    server,
                    plan.user_scope_id,
                    request,
                    request_progress[progress_signature],
                )
                if is_musicbrainz:
                    print(
                        "[EBB:musicbrainz] "
                        f"kind={request.kind} seed={request.key} "
                        f"phase={details.get('request_phase') or '-'} "
                        f"error={failure_name} "
                        f"status={int(details.get('http_status') or 0)} "
                        f"elapsed_ms={int(details.get('provider_elapsed_ms') or 0)} "
                        f"retry_in_s={int(retry_delay) if transient else 0}"
                    )
                else:
                    print(
                        "[EBB:provider] "
                        f"provider={provider_name or '-'} "
                        f"kind={request.kind} seed={request.key} "
                        f"error={failure_name} "
                        f"status={int(details.get('http_status') or 0)} "
                        f"retry_in_s={int(retry_delay)}"
                    )
                continue
            completed += 1
            pool_seen = seen_by_pool.setdefault(request.pool, set())
            unique_results: List[Dict[str, Any]] = []
            returned: List[str] = []
            for result in results:
                identity = _track_id(result)
                if identity:
                    returned.append(identity)
                if not identity or identity in pool_seen:
                    repeated_batch_count += 1
                    continue
                pool_seen.add(identity)
                unique_results.append(result)
            pools.setdefault(request.pool, []).extend(unique_results)
            no_new = 0 if unique_results else int(prior.get("consecutive_no_new") or 0) + 1
            # An empty MusicBrainz release-group page is a definitive end of
            # that artist catalogue. Duplicate-only pages retain the bounded
            # two-page rule because a later page may still contain new supply.
            exhausted = (
                request.kind in {"album_catalog", "artist_catalog"}
                and _is_musicbrainz_request(request)
                and not results
            ) or no_new >= 2
            if isinstance(results, AlbumCatalogPage):
                exhausted = results.exhausted
            cursor_base = int(
                request.metadata.get("release_group_offset")
                or request.metadata.get("offset")
                or 0
            )
            if request.kind in {"album_catalog", "artist_catalog"}:
                cursor_step = int(
                    request.metadata.get("release_group_limit")
                    or (request.limit if request.kind == "album_catalog" else 1)
                )
            else:
                cursor_step = len(results)
            progress = {
                "state": "exhausted" if exhausted else "resolved",
                "attempts": int(prior.get("attempts") or 0) + 1,
                "cursor": cursor_base + max(cursor_step, 0),
                "returned_identities": list(dict.fromkeys([*(prior.get('returned_identities') or []), *returned]))[-512:],
                "last_raw_count": len(results),
                "last_accepted_count": len(unique_results),
                "consecutive_no_new": no_new,
                "exhausted": exhausted,
                # A duplicate-only recheck still contains valid catalog
                # metadata. Never overwrite its page cache with an empty list.
                "results": list(results) if request.kind == 'album_catalog' else unique_results,
            }
            if isinstance(results, AlbumCatalogPage):
                progress.update(cursor=results.cursor, provider_artist_id=results.artist_id,
                    catalog_frontier_bounded=results.bounded,
                    catalog_next_check_at=time.time()+ALBUM_CATALOG_CHECK_SECONDS if exhausted or results.bounded else 0)
            if isinstance(results, ArtistGraphPage):
                progress["artist_edges"] = results.artist_edges
            request_progress[progress_signature] = progress
            _store_job(server, plan.user_scope_id, request, progress)
            if deadline is not None and time.perf_counter() >= deadline:
                break
    except FuturesTimeoutError:
        now = time.time()
        for future, request in futures.items():
            if future.done():
                continue
            timed_out_request_count += 1
            progress_signature = _request_progress_signature(request)
            signature = _request_signature(request)
            prior = dict(
                request_progress.get(progress_signature)
                or request_progress.get(signature)
                or {}
            )
            attempts = int(prior.get("attempts") or 0) + 1
            is_musicbrainz = _is_musicbrainz_request(request)
            provider_name = _request_provider(request)
            retry_delay = (
                _provider_retry_delay_seconds(
                    {"provider_retry_after_seconds": 0},
                    attempts,
                    provider=provider_name,
                )
            )
            timeout_progress = {
                **prior,
                "state": "retryable",
                "attempts": attempts,
                "retry_at": now + retry_delay,
                "provider": provider_name,
                "provider_retry_at": now + retry_delay,
                "last_error": "AcquisitionDeadlineExceeded",
            }
            request_progress[progress_signature] = timeout_progress
            _store_job(
                server,
                plan.user_scope_id,
                request,
                timeout_progress,
            )
            failures[
                f"{request.kind}:{_request_provider(request)}:{request.key}"
            ] = (
                "AcquisitionDeadlineExceeded"
            )
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    raw_pool_counts = {name: len(values) for name, values in pools.items()}
    pools, canonical_stats = canonicalize_materialized_pools(pools)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(
        "[EBB:acquisition] "
        f"completed={completed} failed={len(failures)} "
        f"canonical={canonical_stats['canonical_supply_unique_tracks']}/"
        f"{canonical_stats['canonical_supply_raw_tracks']} "
        f"duplicates={canonical_stats['canonical_supply_duplicates_removed']} "
        f"upgraded={canonical_stats['canonical_supply_identity_upgrades']} "
        f"elapsed_ms={elapsed_ms}"
    )
    return MaterializedCandidateSupply(
        pools=pools,
        diagnostics={
            "planned_request_count": len(all_requests),
            "album_seed_diagnostics": dict(plan.album_seed_diagnostics),
            "completed_request_count": completed,
            "cached_request_count": cached,
            "retry_deferred_request_count": retry_deferred,
            "exhausted_request_count": exhausted,
            "provider_deferred_request_count": provider_deferred,
            "scheduled_request_count": len(scheduled),
            "scheduled_pool_counts": {
                pool: sum(1 for request in scheduled if request.pool == pool)
                for pool in dict.fromkeys(request.pool for request in scheduled)
            },
            "scheduled_provider_counts": {
                provider: sum(
                    1
                    for request in scheduled
                    if _request_provider(request) == provider
                )
                for provider in dict.fromkeys(
                    _request_provider(request) for request in scheduled
                )
            },
            "deferred_request_count": deferred_count,
            "failed_request_count": len(failures),
            "timed_out_request_count": timed_out_request_count,
            "failure_types": failures,
            "musicbrainz_provider_retry_at": _musicbrainz_shared_retry_at(
                request_progress
            ),
            "provider_retry_at": {
                provider: _provider_shared_retry_at(request_progress, provider)
                for provider in sorted(
                    {
                        _text(progress.get("provider")).casefold()
                        for progress in request_progress.values()
                        if isinstance(progress, dict)
                        and _text(progress.get("provider"))
                    }
                )
            },
            "anchor_track_count": plan.anchor_track_count,
            "anchor_artist_count": plan.anchor_artist_count,
            "anchor_cursor_start": plan.anchor_cursor_start,
            "anchor_cursor_next": plan.anchor_cursor_next,
            "artist_cursor_next": plan.artist_cursor_next,
            "radio_discovery_cursor_next": plan.radio_discovery_cursor_next,
            "repeated_batch_count": repeated_batch_count,
            **canonical_stats,
            "request_progress": request_progress,
            "raw_pool_counts": raw_pool_counts,
            "canonical_pool_counts": {name: len(values) for name, values in pools.items()},
            "elapsed_ms": elapsed_ms,
        },
    )
