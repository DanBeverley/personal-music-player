from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List
import hashlib
import json
import time

from .schema import DiscoveryArtifact, DiscoveryCandidate, DiscoveryRow, TasteProfile
from .structured_providers import (
    CanonicalRecording,
    LastFmClient,
    ListenBrainzClient,
)


ACQUISITION_JOB_NAMESPACE = "discovery_acquisition_job"
ACQUISITION_JOB_MODEL = "structured-acquisition"
ACQUISITION_RESULT_TTL_SECONDS = 60 * 60 * 24 * 14
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


@dataclass(frozen=True)
class EnrichmentRequest:
    kind: str
    key: str
    pool: str
    relation: str
    limit: int
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateEnrichmentPlan:
    user_scope_id: str
    requests: List[EnrichmentRequest] = field(default_factory=list)
    anchor_track_count: int = 0
    anchor_artist_count: int = 0
    anchor_cursor_start: int = 0
    anchor_cursor_next: int = 0
    artist_cursor_next: int = 0
    prior_request_progress: Dict[str, Dict[str, Any]] = field(default_factory=dict)


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


def hydrate_artifact_release_metadata(
    server: Any,
    artifact: DiscoveryArtifact,
    *,
    allow_remote_lookup: bool = True,
) -> tuple[DiscoveryArtifact, int]:
    """Apply persisted release metadata to every track stored in an artifact.

    Returns the hydrated artifact and the count of canonical tracks which have
    not completed a metadata lookup yet. Authoritative MusicBrainz misses are
    allowed to remain without a fabricated year.
    """

    recording_ids: List[str] = []
    source_ids: List[str] = []

    def playback_source_id(raw: Dict[str, Any]) -> str:
        playback = raw.get("playback")
        return _text(
            (playback.get("source_id") if isinstance(playback, dict) else "")
            or raw.get("playable_source_id")
            or raw.get("videoId")
            or raw.get("video_id")
        )

    def collect_sources(raw: Dict[str, Any]) -> None:
        source_id = playback_source_id(raw)
        if source_id:
            source_ids.append(source_id)
        for key in ("tracks", "items", "recommendations", "canonical_tracks"):
            nested = raw.get(key)
            if isinstance(nested, list):
                for child in nested:
                    if isinstance(child, dict):
                        collect_sources(child)

    for row in artifact.rows or []:
        for item in row.items or []:
            if isinstance(item, dict):
                collect_sources(item)

    source_recording_ids = _recording_ids_for_playback_sources(server, source_ids)

    def collect(raw: Dict[str, Any]) -> None:
        recording_id = _text(
            raw.get("musicbrainz_recording_id") or raw.get("recording_mbid")
        ).casefold()
        if not recording_id:
            recording_id = source_recording_ids.get(playback_source_id(raw), "")
            if recording_id:
                raw["musicbrainz_recording_id"] = recording_id
        if recording_id:
            recording_ids.append(recording_id)
        for key in ("tracks", "items", "recommendations", "canonical_tracks"):
            nested = raw.get(key)
            if isinstance(nested, list):
                for child in nested:
                    if isinstance(child, dict):
                        collect(child)

    for row in artifact.rows or []:
        for item in row.items or []:
            if isinstance(item, dict):
                collect(item)

    cached = _load_release_metadata(server, recording_ids)
    now = time.time()
    missing_ids = [
        recording_id
        for recording_id in dict.fromkeys(recording_ids)
        if recording_id not in cached
    ][:RELEASE_METADATA_BATCH_SIZE]
    discovered, _lookup_failed = (
        _lookup_release_metadata(server, missing_ids, now=now)
        if allow_remote_lookup
        else ({}, False)
    )
    _store_release_metadata(server, discovered)
    cached = {**cached, **discovered}
    known_by_release_group: Dict[str, Dict[str, Any]] = {}
    for metadata in cached.values():
        release_group_id = _text(
            metadata.get("musicbrainz_release_group_id")
        ).casefold()
        if release_group_id and _release_year(metadata):
            known_by_release_group.setdefault(release_group_id, metadata)

    pending_ids: set[str] = set()

    def hydrate(raw: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(raw)
        for key in ("tracks", "items", "recommendations", "canonical_tracks"):
            nested = item.get(key)
            if isinstance(nested, list):
                item[key] = [
                    hydrate(child) if isinstance(child, dict) else child
                    for child in nested
                ]
        if _release_year(item):
            item.setdefault("release_year", _release_year(item))
            return item
        recording_id = _text(
            item.get("musicbrainz_recording_id") or item.get("recording_mbid")
        ).casefold()
        release_group_id = _text(
            item.get("musicbrainz_release_group_id") or item.get("release_group_mbid")
        ).casefold()
        metadata = cached.get(recording_id) or known_by_release_group.get(release_group_id)
        if metadata and _release_year(metadata):
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
        elif recording_id and recording_id not in cached:
            pending_ids.add(recording_id)
        return item

    rows = [
        replace(
            row,
            items=[hydrate(item) for item in row.items or [] if isinstance(item, dict)],
        )
        for row in artifact.rows or []
    ]
    diagnostics = dict(artifact.diagnostics or {})
    diagnostics["published_release_metadata_pending"] = len(pending_ids)
    return replace(artifact, rows=rows, diagnostics=diagnostics), len(pending_ids)


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
            albums: List[Dict[str, Any]] = []
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
                albums.append({**dict(album), "canonical_tracks": list(album_tracks.values())})
            output[pool] = albums
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
    return f"{request.kind}:{request.pool}:{request.key.casefold()}:{cursor}"


def _request_cursor(
    progress: Dict[str, Dict[str, Any]],
    *,
    kind: str,
    pool: str,
    key: str,
) -> int:
    legacy_signature = f"{kind}:{pool}:{key.casefold()}"
    prefix = f"{legacy_signature}:"
    return max(
        (
            int(value.get("cursor") or 0)
            for signature, value in progress.items()
            if (signature == legacy_signature or signature.startswith(prefix))
            and isinstance(value, dict)
        ),
        default=0,
    )


def _select_scheduled_requests(
    pending: List[EnrichmentRequest],
    limit: int,
) -> List[EnrichmentRequest]:
    if limit <= 0:
        return []
    pool_order = (
        "similarity",
        "profile_spine",
        "radio_artist_catalog",
        "artist_graph",
        "genre_mood",
        "collaborative",
        "popularity",
        "album",
    )
    buckets: Dict[str, List[EnrichmentRequest]] = {}
    for request in pending:
        buckets.setdefault(request.pool, []).append(request)
    special_pools = {"radio_artist_catalog", "album"}
    ordered_pools = [
        pool for pool in pool_order if pool not in special_pools and buckets.get(pool)
    ]
    ordered_pools.extend(
        pool for pool in buckets if pool not in special_pools and pool not in ordered_pools
    )
    selected: List[EnrichmentRequest] = []

    general_capacity = 1 if any(buckets.get(pool) for pool in ordered_pools) else 0
    radio_bucket = buckets.get("radio_artist_catalog") or []
    radio_reserve = min(3, max(limit - general_capacity, 0))
    while radio_bucket and len(selected) < radio_reserve:
        selected.append(radio_bucket.pop(0))

    # Album work used to be omitted whenever any regular candidate work was
    # pending.  Reserve two bounded jobs so canonical album pages advance and
    # complete albums accumulate without increasing the six-job batch.
    album_bucket = buckets.get("album") or []
    album_reserve = min(2, max(limit - len(selected) - general_capacity, 0))
    while album_bucket and album_reserve > 0:
        selected.append(album_bucket.pop(0))
        album_reserve -= 1

    while len(selected) < limit:
        added = False
        for pool in ordered_pools:
            bucket = buckets.get(pool) or []
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break

    # If there was no regular work, use the remaining capacity for additional
    # radio/album pages instead of returning a short batch.
    while len(selected) < limit and (radio_bucket or album_bucket):
        if radio_bucket and len(selected) < limit:
            selected.append(radio_bucket.pop(0))
        if album_bucket and len(selected) < limit:
            selected.append(album_bucket.pop(0))
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


def build_enrichment_plan(
    taste: TasteProfile,
    *,
    acquisition_ledger: Dict[str, Any] | None = None,
    allowed_pools: set[str] | None = None,
    radio_discovery_artist_seeds: List[Dict[str, Any]] | None = None,
    radio_discovery_deficit: int = 0,
) -> CandidateEnrichmentPlan:
    ledger = dict(acquisition_ledger or {})
    request_progress = {
        str(key): dict(value)
        for key, value in dict(ledger.get("request_progress") or {}).items()
        if isinstance(value, dict)
    }
    cursor = max(int(ledger.get("anchor_cursor") or 0), 0)
    anchor_tracks, cursor_next = _diverse_history_anchors(taste, cursor)
    all_anchor_artists = _unique(
        [
            *taste.artist_hints,
            *taste.top_artists,
            *taste.listened_artists,
            *[_artist(track) for track in anchor_tracks],
        ],
        limit=32,
    )
    artist_cursor = max(int(ledger.get("artist_cursor") or 0), 0)
    rotated_artists = [
        *all_anchor_artists[artist_cursor:],
        *all_anchor_artists[:artist_cursor],
    ]
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
        )[:3]
        # Discovery seeds come from persisted, profile-compatible artist
        # evidence. They must take precedence over repairing familiar seeds
        # while the radio discovery reserve is deficient.
        discovery = []
        for seed in radio_discovery_artist_seeds or []:
            name = _text(seed.get("name") if isinstance(seed, dict) else seed)
            if name and name.casefold() not in {a.casefold() for a in all_anchor_artists}:
                discovery.append(name)
        anchor_artists = [*discovery[:4], *direct_repairs]
        anchor_artists = _unique(anchor_artists, limit=4 if int(radio_discovery_deficit or 0) > 0 else 3)
    artist_cursor_next = (
        (artist_cursor + max(len(anchor_artists), 1)) % len(all_anchor_artists)
        if all_anchor_artists
        else 0
    )
    if allowed_pools == {"radio_artist_catalog"} and radio_discovery_artist_seeds:
        discovery_cursor = max(int(ledger.get("radio_discovery_cursor") or 0), 0)
        artist_cursor_next = (
            discovery_cursor + len(anchor_artists)
        ) % max(len(radio_discovery_artist_seeds), 1)
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
    radio_work_needed = (
        int(optional_row_counts.get("popular_radio") or 0) < 8
        or (allowed_pools == {"radio_artist_catalog"} and (
            int(radio_discovery_deficit or 0) > 0 or bool(radio_discovery_artist_seeds)
        ))
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
            "user_scope_id": taste.user_scope_id,
        }
        requests.extend(
            [
                EnrichmentRequest(
                    kind="listenbrainz_artist_recordings",
                    key=artist_mbid or artist,
                    pool="profile_spine",
                    relation="same_artist_catalog",
                    limit=16,
                    metadata=metadata,
                ),
                *(
                    [
                        EnrichmentRequest(
                            kind="canonical_artist_radio_catalog",
                            key=artist_mbid or artist,
                            pool="radio_artist_catalog",
                            relation="same_artist_catalog",
                            limit=48,
                            metadata={
                                **metadata,
                                "radio_seed_artist": artist,
                                "release_group_limit": 4,
                                "release_group_offset": _request_cursor(
                                    request_progress,
                                    kind="canonical_artist_radio_catalog",
                                    pool="radio_artist_catalog",
                                    key=artist_mbid or artist,
                                ),
                            },
                        )
                    ]
                    if radio_work_needed
                    else []
                ),
                *[
                    EnrichmentRequest(
                        kind="lastfm_artist_similar",
                        key=artist_mbid or artist,
                        pool="artist_graph",
                        relation="artist_neighbor",
                        limit=8,
                        metadata={**metadata, "offset": neighbor_offset},
                    )
                    for neighbor_offset in (0, 3)
                ],
            ]
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
    )
    if album_work_needed:
        album_artist_counts = {
            str(key): int(value or 0)
            for key, value in dict(ledger.get("album_artist_counts") or {}).items()
        }
        album_seed_artists = _unique(
            [
                *all_anchor_artists,
                *list(ledger.get("album_expansion_artist_seeds") or []),
                *list(ledger.get("backbone_artist_seeds") or []),
            ],
            limit=48,
        )
        seed_order = {
            " ".join(artist.casefold().split()): index
            for index, artist in enumerate(album_seed_artists)
        }
        album_seed_artists.sort(
            key=lambda artist: (
                album_artist_counts.get(" ".join(artist.casefold().split()), 0) >= 3,
                album_artist_counts.get(" ".join(artist.casefold().split()), 0),
                seed_order.get(" ".join(artist.casefold().split()), 0),
            )
        )
        for artist in album_seed_artists[:16]:
            artist_key = " ".join(artist.casefold().split())
            artist_mbid = _text(artist_ids.get(artist_key))
            request_key = artist_mbid or artist
            requests.append(
                EnrichmentRequest(
                    kind="canonical_album_catalog",
                    key=request_key,
                    pool="album",
                    relation="artist_album_catalog",
                    limit=4,
                    metadata={
                        "profile_seed_artist": artist,
                        "musicbrainz_artist_id": artist_mbid,
                        "user_scope_id": taste.user_scope_id,
                        "album_artist_qualified_count": album_artist_counts.get(
                            artist_key,
                            0,
                        ),
                        "release_group_offset": _request_cursor(
                            request_progress,
                            kind="canonical_album_catalog",
                            pool="album",
                            key=request_key,
                        ),
                    },
                )
            )
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

    def needs_reserve(*domains: str) -> bool:
        return any(int(reserve_shortages.get(domain) or 0) > 0 for domain in domains)

    def targets_shortage(request: EnrichmentRequest) -> bool:
        if not has_inventory_coverage:
            return True
        if request.kind == "canonical_album_catalog":
            # Optional album shortages are tracked separately from the core
            # feed contracts and still need guaranteed scheduler capacity.
            return album_work_needed
        if request.kind == "canonical_artist_radio_catalog":
            return "popular_radio" in failed_domains or (
                not failed_domains and radio_work_needed
            )
        if request.kind == "lastfm_artist_similar":
            return bool(
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
        progress = request_progress.get(signature) or {}
        if signature in seen or progress.get("exhausted") is True or not targets_shortage(request):
            continue
        seen.add(signature)
        deduped.append(request)
    return CandidateEnrichmentPlan(
        user_scope_id=taste.user_scope_id,
        requests=deduped,
        anchor_track_count=len(anchor_tracks),
        anchor_artist_count=len(anchor_artists),
        anchor_cursor_start=cursor,
        anchor_cursor_next=cursor_next,
        artist_cursor_next=artist_cursor_next,
        prior_request_progress=request_progress,
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
        "pool": request.pool,
        "key": request.key,
        "updated_at": now,
        "expires_at": now + ACQUISITION_RESULT_TTL_SECONDS,
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


def _resolve_artist_mbid(server: Any, artist: str, artist_mbid: str = "") -> str:
    if _text(artist_mbid):
        return _text(artist_mbid)
    from ..search.musicbrainz import MusicBrainzClient

    client = getattr(server, "musicbrainz_client", None) or MusicBrainzClient()
    matches = client.search_artists(artist, limit=8)
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
                "source_provenance": f"structured:{request.kind}",
            }
        )
        if len(output) >= request.limit:
            break
    return output


def _resolve_album_rows(server: Any, request: EnrichmentRequest) -> List[Dict[str, Any]]:
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
    albums = browse_musicbrainz_artist_album_items(
        artist,
        artist_id=artist_mbid,
        client=getattr(server, "musicbrainz_client", None),
        limit=request.limit,
        offset=max(int(request.metadata.get("release_group_offset") or 0), 0),
    )
    output: List[Dict[str, Any]] = []
    for album in albums:
        release_group_id = _text(album.get("musicbrainz_release_group_id"))
        canonical_tracks = browse_musicbrainz_release_group_tracks(
            release_group_id,
            client=getattr(server, "musicbrainz_client", None),
            limit=40,
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
                "id": f"musicbrainz:release-group:{release_group_id}",
                "browseId": f"musicbrainz:release-group:{release_group_id}",
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
                "source_provenance": "structured:canonical_album_catalog",
            }
        output.append(album_candidate)
    return output


def _resolve_artist_radio_rows(server: Any, request: EnrichmentRequest) -> List[Dict[str, Any]]:
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
    albums = browse_musicbrainz_artist_album_items(
        artist,
        artist_id=artist_mbid,
        client=getattr(server, "musicbrainz_client", None),
        limit=max(int(request.metadata.get("release_group_limit") or 4), 1),
        offset=max(int(request.metadata.get("release_group_offset") or 0), 0),
    )
    rows: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    direct_limit = min(30, request.limit)
    for album in albums:
        release_group_id = _text(album.get("musicbrainz_release_group_id"))
        for track in browse_musicbrainz_release_group_tracks(
            release_group_id,
            client=getattr(server, "musicbrainz_client", None),
            limit=40,
        ):
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
                    "relationship_provider": "musicbrainz",
                    "relationship_evidence": "canonical_artist_discography",
                    "relationship_score": 1.0,
                }
            )
            if len(rows) >= direct_limit:
                break
        if len(rows) >= direct_limit:
            break
    if len(rows) < request.limit:
        neighbors = LastFmClient(server).similar_artists(
            artist,
            artist_mbid=artist_mbid,
            limit=3,
        )
        listenbrainz = ListenBrainzClient(server)
        remaining = request.limit - len(rows)
        per_neighbor = max(4, (remaining + max(len(neighbors), 1) - 1) // max(len(neighbors), 1))
        for neighbor in neighbors:
            neighbor_name = _artist(neighbor)
            neighbor_mbid = _resolve_artist_mbid(
                server,
                neighbor_name,
                _text(neighbor.get("musicbrainz_artist_id")),
            )
            if not neighbor_mbid:
                continue
            for track in listenbrainz.top_recordings(neighbor_mbid, limit=per_neighbor):
                rows.append(
                    {
                        **track,
                        "related_to_artist": artist,
                        "radio_seed_artist": artist,
                        "radio_catalog_role": "neighbor",
                        "relationship_provider": "lastfm+listenbrainz",
                        "relationship_evidence": "canonical_neighbor_catalog",
                        "relationship_score": neighbor.get("relationship_score") or 0.7,
                    }
                )
                if len(rows) >= request.limit:
                    break
            if len(rows) >= request.limit:
                break
    return _canonical_recording_rows(server, rows, request=request)


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
        per_artist = max(2, min(6, (request.limit + max(len(neighbors), 1) - 1) // max(len(neighbors), 1)))
        listenbrainz = ListenBrainzClient(server)
        for neighbor in neighbors:
            neighbor_name = _artist(neighbor)
            neighbor_mbid = _resolve_artist_mbid(
                server,
                neighbor_name,
                _text(neighbor.get("musicbrainz_artist_id")),
            )
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
        return _canonical_recording_rows(server, rows, request=request)
    if request.kind == "lastfm_tag_tracks":
        rows = LastFmClient(server).tag_tracks(request.key, limit=request.limit)
        return _canonical_recording_rows(server, rows, request=request)
    if request.kind == "listenbrainz_artist_recordings":
        artist_mbid = _resolve_artist_mbid(server, artist, artist_mbid_hint) if artist else ""
        rows = ListenBrainzClient(server).top_recordings(artist_mbid, limit=request.limit)
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
    if request.kind == "canonical_album_catalog":
        return _resolve_album_rows(server, request)
    if request.kind == "canonical_artist_radio_catalog":
        return _resolve_artist_radio_rows(server, request)
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
    for request in plan.requests:
        job = _load_job(server, plan.user_scope_id, request)
        if job and float(job.get("expires_at") or 0) > time.time():
            state = _text(job.get("state"))
            if state == "resolved":
                results = [dict(value) for value in job.get("results") or [] if isinstance(value, dict)]
                if results:
                    pools.setdefault(request.pool, []).extend(results)
                    cached += 1
                    completed += 1
                    continue
                if job.get("exhausted") is True:
                    completed += 1
                    continue
            if state == "exhausted":
                completed += 1
                continue
            if state == "retryable" and float(job.get("retry_at") or 0) > time.time():
                continue
        pending.append(request)

    pending_total = len(pending)
    if max_pending_jobs is None:
        scheduled = pending
    else:
        scheduled = _select_scheduled_requests(
            pending,
            max(int(max_pending_jobs), 0),
        )
    deferred_count = pending_total - len(scheduled)
    print(
        "[EBB:acquisition] "
        f"planned={len(plan.requests)} cached={cached} "
        f"scheduled={len(scheduled)} deferred={deferred_count} "
        f"pools={','.join(request.pool for request in scheduled)}"
    )

    for request in scheduled:
        signature = _request_signature(request)
        prior = dict(request_progress.get(signature) or {})
        pending_progress = {
            **prior,
            "state": "pending",
            "queued_at": time.time(),
        }
        request_progress[signature] = pending_progress
        _store_job(server, plan.user_scope_id, request, pending_progress)

    executor = ThreadPoolExecutor(
        max_workers=max(min(int(max_workers or 1), 8), 1),
        thread_name_prefix="auralis-structured-acquisition",
    )
    futures: Dict[Future[List[Dict[str, Any]]], EnrichmentRequest] = {
        executor.submit(_fetch_request, server, request): request
        for request in scheduled
    }
    deadline = started + max(float(time_budget_seconds), 1.0) if time_budget_seconds else None
    repeated_batch_count = 0
    seen_by_pool: Dict[str, set[str]] = {
        pool: {_track_id(item) for item in values if _track_id(item)}
        for pool, values in pools.items()
    }
    for request in plan.requests:
        progress = request_progress.get(_request_signature(request)) or {}
        seen_by_pool.setdefault(request.pool, set()).update(
            _text(identity)
            for identity in progress.get("returned_identities") or []
            if _text(identity)
        )
    try:
        for future in as_completed(futures):
            request = futures[future]
            signature = _request_signature(request)
            prior = dict(request_progress.get(signature) or {})
            try:
                results = future.result()
            except Exception as exc:
                failures[f"{request.kind}:{request.key}"] = type(exc).__name__
                attempts = int(prior.get("attempts") or 0) + 1
                retry_at = time.time() + min(60 * (2 ** min(attempts, 5)), 1800)
                request_progress[signature] = {
                    **prior,
                    "state": "retryable",
                    "attempts": attempts,
                    "retry_at": retry_at,
                    "last_error": type(exc).__name__,
                }
                _store_job(
                    server,
                    plan.user_scope_id,
                    request,
                    request_progress[signature],
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
            exhausted = no_new >= 2
            cursor_base = int(
                request.metadata.get("release_group_offset")
                or request.metadata.get("offset")
                or 0
            )
            if request.kind in {"canonical_album_catalog", "canonical_artist_radio_catalog"}:
                cursor_step = int(
                    request.metadata.get("release_group_limit")
                    or (request.limit if request.kind == "canonical_album_catalog" else 1)
                )
            else:
                cursor_step = len(results)
            progress = {
                "state": "exhausted" if exhausted else "resolved",
                "attempts": int(prior.get("attempts") or 0) + 1,
                "cursor": cursor_base + max(cursor_step, 0),
                "returned_identities": returned[-128:],
                "last_raw_count": len(results),
                "last_accepted_count": len(unique_results),
                "consecutive_no_new": no_new,
                "exhausted": exhausted,
                "results": unique_results,
            }
            request_progress[signature] = progress
            _store_job(server, plan.user_scope_id, request, progress)
            if deadline is not None and time.perf_counter() >= deadline:
                break
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
            "planned_request_count": len(plan.requests),
            "completed_request_count": completed,
            "cached_request_count": cached,
            "scheduled_request_count": len(scheduled),
            "scheduled_pool_counts": {
                pool: sum(1 for request in scheduled if request.pool == pool)
                for pool in dict.fromkeys(request.pool for request in scheduled)
            },
            "deferred_request_count": deferred_count,
            "failed_request_count": len(failures),
            "failure_types": failures,
            "anchor_track_count": plan.anchor_track_count,
            "anchor_artist_count": plan.anchor_artist_count,
            "anchor_cursor_start": plan.anchor_cursor_start,
            "anchor_cursor_next": plan.anchor_cursor_next,
            "artist_cursor_next": plan.artist_cursor_next,
            "repeated_batch_count": repeated_batch_count,
            **canonical_stats,
            "request_progress": request_progress,
            "raw_pool_counts": raw_pool_counts,
            "canonical_pool_counts": {name: len(values) for name, values in pools.items()},
            "elapsed_ms": elapsed_ms,
        },
    )
