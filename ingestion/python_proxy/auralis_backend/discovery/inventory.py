from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Tuple
import json
import threading
import time
import uuid

from ..storage.session_store import get_session_store
from .candidates import build_candidate_pools
from .schema import DiscoveryCandidate, TasteProfile

if TYPE_CHECKING:
    from .enrichment import MaterializedCandidateSupply


INVENTORY_NAMESPACE = "discovery_candidate_inventory"
# This remains the single current inventory format.  Retaining its persisted
# identifier lets an accepted inventory survive a process/app upgrade.
INVENTORY_MODEL_VERSION = "candidate-inventory-v6"
INVENTORY_TTL_SECONDS = 60 * 60 * 8
INVENTORY_INTENT_NAMESPACE = "discovery_candidate_inventory_intent"
INVENTORY_INTENT_TTL_SECONDS = 60 * 60 * 2

ESTABLISHED_MIN_UNIQUE_TRACKS = 120
ESTABLISHED_MIN_UNPLAYED_TRACKS = 48
COLD_START_MIN_UNIQUE_TRACKS = 48
COLD_START_MIN_UNPLAYED_TRACKS = 20
# The two visible album shelves need 20 distinct albums between them.  Keeping
# only 20 qualified albums leaves no room for artist diversity, refresh
# rotation, or an album becoming unavailable.  Acquisition therefore keeps a
# modest post-ranking reserve instead of stopping at the visible minimum.
QUALIFIED_ALBUM_RESERVE_TARGET = 32
ROW_RESERVE_TARGETS = {
    "made_for_you_tracks": 80,
    "because_you_played": 48,
    "recommended_artists": 30,
    "quiet_picks": 160,
}


_INVENTORY_LOCKS: Dict[str, threading.RLock] = {}
_INVENTORY_LOCKS_GUARD = threading.Lock()


_RELATION_STRENGTH = {
    "direct_history": 1.0,
    "same_artist_catalog": 0.94,
    "same_album": 0.92,
    "track_radio": 0.86,
    "artist_neighbor": 0.82,
    "collaborative_neighbor": 0.74,
    "structured_tag": 0.58,
    "broad_global": 0.25,
    "unproven": 0.0,
}


@dataclass
class CandidateInventory:
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float
    pools: Dict[str, List[DiscoveryCandidate]] = field(default_factory=dict)
    candidate_counts: Dict[str, int] = field(default_factory=dict)
    provider_timings_ms: Dict[str, int] = field(default_factory=dict)
    generation_id: str = ""
    base_generation_id: str = ""
    intent_version: int = 0
    coverage: Dict[str, Any] = field(default_factory=dict)
    acquisition_ledger: Dict[str, Any] = field(default_factory=dict)
    row_coverage: Dict[str, Any] = field(default_factory=dict)
    canonical_stats: Dict[str, int] = field(default_factory=dict)

    @property
    def is_fresh(self) -> bool:
        return bool(self.expires_at > time.time())

    @property
    def model_version(self) -> str:
        return INVENTORY_MODEL_VERSION

    @property
    def is_ready(self) -> bool:
        return self.coverage.get("ready") is True


def _key(user_scope_id: str) -> str:
    scope = str(user_scope_id or "guest").strip() or "guest"
    return f"candidate-inventory:{scope}"


def _intent_key(user_scope_id: str) -> str:
    scope = str(user_scope_id or "guest").strip() or "guest"
    return f"candidate-inventory-intent:{scope}"


def _inventory_lock(user_scope_id: str) -> threading.RLock:
    scope = str(user_scope_id or "guest").strip() or "guest"
    with _INVENTORY_LOCKS_GUARD:
        return _INVENTORY_LOCKS.setdefault(scope, threading.RLock())


def _candidate_to_dict(candidate: DiscoveryCandidate) -> Dict[str, Any]:
    return {
        "item": dict(candidate.item or {}),
        "source": str(candidate.source or ""),
        "score": float(candidate.score or 0.0),
        "reasons": list(candidate.reasons or []),
        "item_type": str(candidate.item_type or "track"),
    }


def _candidate_from_dict(payload: Dict[str, Any]) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        item=dict(payload.get("item") or {}),
        source=str(payload.get("source") or ""),
        score=float(payload.get("score") or 0.0),
        reasons=[str(reason) for reason in payload.get("reasons") or []],
        item_type=str(payload.get("item_type") or "track"),
    )


def _to_payload(inventory: CandidateInventory) -> Dict[str, Any]:
    return {
        "inventory_version": INVENTORY_MODEL_VERSION,
        "user_scope_id": inventory.user_scope_id,
        "profile_fingerprint": inventory.profile_fingerprint,
        "generated_at": inventory.generated_at,
        "expires_at": inventory.expires_at,
        "pools": {
            name: [_candidate_to_dict(candidate) for candidate in candidates]
            for name, candidates in inventory.pools.items()
        },
        "candidate_counts": dict(inventory.candidate_counts or {}),
        "provider_timings_ms": dict(inventory.provider_timings_ms or {}),
        "generation_id": str(inventory.generation_id or ""),
        "base_generation_id": str(inventory.base_generation_id or ""),
        "intent_version": max(int(inventory.intent_version or 0), 0),
        "coverage": dict(inventory.coverage or {}),
        "acquisition_ledger": dict(inventory.acquisition_ledger or {}),
        "row_coverage": dict(inventory.row_coverage or {}),
        "canonical_stats": dict(inventory.canonical_stats or {}),
    }


def _from_payload(payload: Dict[str, Any] | None) -> CandidateInventory | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("inventory_version") or "") != INVENTORY_MODEL_VERSION:
        return None
    pools: Dict[str, List[DiscoveryCandidate]] = {}
    for name, values in (payload.get("pools") or {}).items():
        if not isinstance(values, list):
            continue
        pools[str(name)] = [
            _candidate_from_dict(dict(value))
            for value in values
            if isinstance(value, dict)
        ]
    return CandidateInventory(
        user_scope_id=str(payload.get("user_scope_id") or "guest"),
        profile_fingerprint=str(payload.get("profile_fingerprint") or ""),
        generated_at=float(payload.get("generated_at") or 0.0),
        expires_at=float(payload.get("expires_at") or 0.0),
        pools=pools,
        candidate_counts=dict(payload.get("candidate_counts") or {}),
        provider_timings_ms=dict(payload.get("provider_timings_ms") or {}),
        generation_id=str(payload.get("generation_id") or ""),
        base_generation_id=str(payload.get("base_generation_id") or ""),
        intent_version=max(int(payload.get("intent_version") or 0), 0),
        coverage=dict(payload.get("coverage") or {}),
        acquisition_ledger=dict(payload.get("acquisition_ledger") or {}),
        row_coverage=dict(payload.get("row_coverage") or {}),
        canonical_stats={
            str(key): int(value or 0)
            for key, value in dict(payload.get("canonical_stats") or {}).items()
        },
    )


def _playable_album_catalog_rows(
    inventory: CandidateInventory,
    *,
    updated_at: float,
) -> List[List[Any]]:
    rows: Dict[str, List[Any]] = {}
    for candidate in inventory.pools.get("album", []) or []:
        if candidate.item_type != "album":
            continue
        item = dict(candidate.item or {})
        release_group_id = str(
            item.get("musicbrainz_release_group_id")
            or item.get("mb_release_group_id")
            or ""
        ).strip()
        tracks: List[Dict[str, Any]] = []
        for raw_track in item.get("tracks") or item.get("canonical_tracks") or []:
            if not isinstance(raw_track, dict) or not str(raw_track.get("track_key") or "").strip():
                continue
            track = dict(raw_track)
            source_id = str(track.get("videoId") or track.get("id") or "").strip()
            if not str(track.get("thumbnail") or "").strip() and len(source_id) == 11:
                track["thumbnail"] = f"https://i.ytimg.com/vi/{source_id}/hqdefault.jpg"
            tracks.append(track)
        if not release_group_id or item.get("playable") is not True or not tracks:
            continue
        thumbnail = str(item.get("thumbnail") or item.get("image") or "").strip()
        if not thumbnail:
            thumbnail = next(
                (
                    str(track.get("thumbnail") or track.get("image") or "").strip()
                    for track in tracks
                    if str(track.get("thumbnail") or track.get("image") or "").strip()
                ),
                "",
            )
        payload = {
            **item,
            "id": f"musicbrainz:release-group:{release_group_id}",
            "musicbrainz_release_group_id": release_group_id,
            "tracks": tracks,
            "canonical_tracks": tracks,
            "track_count": len(tracks),
            "thumbnail": thumbnail,
            "image": thumbnail,
            "playable": True,
        }
        title = str(payload.get("title") or payload.get("name") or "").strip()
        artist = str(payload.get("artist") or payload.get("artist_name") or "").strip()
        entity_key = f"musicbrainz:release:{release_group_id}"
        rows[entity_key] = [
            "album",
            entity_key,
            title,
            artist,
            title,
            1.0,
            float(payload.get("popularity") or 0.0),
            0.0,
            json.dumps(payload, ensure_ascii=False),
            updated_at,
        ]
    return list(rows.values())


def _persistent_get(server: Any, key: str) -> Dict[str, Any] | None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            """
            SELECT payload_json
            FROM recommendation_feature_store
            WHERE namespace = ? AND entity_id = ?
            """,
            [INVENTORY_NAMESPACE, key],
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["payload_json"] or "{}")
        return dict(decoded) if isinstance(decoded, dict) else None
    except Exception:
        return None
    finally:
        connection.close()


def store_candidate_inventory(
    server: Any,
    inventory: CandidateInventory,
    *,
    expected_ready_generation_id: str | None = None,
) -> bool:
    from ..recommend.store_runtime import open_recommendation_store_connection

    payload = _to_payload(inventory)
    key = _key(inventory.user_scope_id)
    with _inventory_lock(inventory.user_scope_id):
        try:
            connection = open_recommendation_store_connection(server)
        except Exception:
            connection = None
        if connection is not None:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM recommendation_feature_store
                    WHERE namespace = ? AND entity_id = ?
                    """,
                    [INVENTORY_NAMESPACE, key],
                ).fetchone()
                current_generation = ""
                if row is not None:
                    decoded = json.loads(row["payload_json"] or "{}")
                    if (
                        isinstance(decoded, dict)
                        and str(decoded.get("inventory_version") or "")
                        == INVENTORY_MODEL_VERSION
                        and str(decoded.get("profile_fingerprint") or "")
                        == inventory.profile_fingerprint
                    ):
                        current_generation = str(decoded.get("generation_id") or "")
                if (
                    expected_ready_generation_id is not None
                    and current_generation != str(expected_ready_generation_id or "")
                ):
                    connection.rollback()
                    return False
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
                INVENTORY_NAMESPACE,
                key,
                INVENTORY_MODEL_VERSION,
                json.dumps(payload, ensure_ascii=False),
                time.time(),
            ],
                )
                album_rows = _playable_album_catalog_rows(
                    inventory,
                    updated_at=time.time(),
                )
                if album_rows:
                    connection.executemany(
                        """
                        INSERT INTO catalog_entities(
                            entity_type, entity_key, display_title, display_artist,
                            display_album, confidence, popularity, learned_popularity,
                            payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                            display_title = excluded.display_title,
                            display_artist = excluded.display_artist,
                            display_album = excluded.display_album,
                            confidence = max(catalog_entities.confidence, excluded.confidence),
                            popularity = max(catalog_entities.popularity, excluded.popularity),
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        album_rows,
                    )
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                return False
            finally:
                connection.close()
        elif expected_ready_generation_id is not None:
            cached = None
            try:
                cached = get_session_store().get(key)
            except Exception:
                cached = None
            cached_generation = (
                str(cached.get("generation_id") or "")
                if isinstance(cached, dict)
                else ""
            )
            if cached_generation != str(expected_ready_generation_id or ""):
                return False
        try:
            get_session_store().set(key, payload, INVENTORY_TTL_SECONDS)
        except Exception:
            return connection is not None
        return True


def load_candidate_inventory(
    server: Any,
    user_scope_id: str,
    *,
    profile_fingerprint: str = "",
    require_fresh: bool = True,
) -> CandidateInventory | None:
    key = _key(user_scope_id)
    payload = None
    try:
        payload = get_session_store().get(key)
    except Exception:
        payload = None
    inventory = _from_payload(payload if isinstance(payload, dict) else None)
    if inventory is None:
        inventory = _from_payload(_persistent_get(server, key))
    if inventory is None:
        return None
    if profile_fingerprint and inventory.profile_fingerprint != profile_fingerprint:
        return None
    if require_fresh and not inventory.is_fresh:
        return None
    return inventory


def inventory_with_row_shortages(
    inventory: CandidateInventory,
    shortages: List[str],
    *,
    quality_reasons: List[str] | None = None,
) -> CandidateInventory:
    normalized = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in shortages or []
            if str(value or "").strip()
        )
    )
    ledger = dict(inventory.acquisition_ledger or {})
    ledger["failed_domains"] = normalized
    ledger["last_row_shortages"] = normalized
    ledger["last_artifact_quality_reasons"] = list(quality_reasons or [])
    ledger["replenishment_cycle"] = int(ledger.get("replenishment_cycle") or 0) + 1
    coverage = dict(inventory.coverage or {})
    coverage["ready"] = False
    coverage["row_failed_contracts"] = normalized
    row_coverage = dict(inventory.row_coverage or {})
    row_coverage["ready"] = False
    row_coverage["failed_contracts"] = normalized
    return replace(
        inventory,
        coverage=coverage,
        row_coverage=row_coverage,
        acquisition_ledger=ledger,
    )


def canonical_item_identity(item: Dict[str, Any], *, item_type: str = "track") -> str:
    def normalized(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    identity = str(
        item.get("musicbrainz_recording_id")
        or item.get("recording_mbid")
        or item.get("isrc")
        or item.get("canonical_entity_id")
        or item.get("canonical_track_identity")
        or ""
    ).strip()
    if identity:
        return f"{item_type}:{identity.casefold()}"
    title = normalized(item.get("title") or item.get("name") or item.get("album"))
    artist = item.get("artist") or item.get("author") or item.get("channel") or ""
    if isinstance(artist, dict):
        artist = artist.get("name") or artist.get("title") or ""
    artist_key = normalized(artist)
    duration = item.get("duration") or item.get("duration_seconds") or item.get("length") or 0
    try:
        duration_bucket = int(float(duration or 0) / 3.0)
    except (TypeError, ValueError):
        duration_bucket = 0
    if title and artist_key:
        suffix = f"|{duration_bucket}" if item_type == "track" and duration_bucket else ""
        return f"{item_type}:{title}|{artist_key}{suffix}"
    provider_id = str(item.get("id") or item.get("videoId") or "").strip()
    return f"{item_type}:provider:{provider_id.casefold()}" if provider_id else ""


def playable_source_rank(item: Dict[str, Any]) -> int:
    provider = " ".join(
        str(item.get("provider") or item.get("source_provider") or item.get("source") or "")
        .strip()
        .casefold()
        .split()
    )
    authority = " ".join(str(item.get("source_authority") or "").strip().casefold().split())
    channel = " ".join(
        str(item.get("channel") or item.get("channel_name") or item.get("author") or "")
        .strip()
        .casefold()
        .split()
    )
    if item.get("is_official") is True or authority == "official":
        return 400
    if item.get("is_topic") is True or channel.endswith(" - topic") or authority in {"label", "topic"}:
        return 300
    if authority in {"canonical", "verified_catalog", "profile_verified"}:
        return 220
    if provider in {"ytmusic", "youtube_music", "youtube"}:
        return 180
    return 100 if str(item.get("id") or item.get("videoId") or "").strip() else 0


def _candidate_identity(candidate: DiscoveryCandidate) -> str:
    canonical = canonical_item_identity(candidate.item or {}, item_type=candidate.item_type)
    if canonical:
        return canonical
    item = candidate.item or {}
    identity = str(
        item.get("canonical_entity_id")
        or item.get("canonical_track_identity")
        or item.get("canonical_source_identity")
        or item.get("id")
        or item.get("videoId")
        or ""
    ).strip()
    if identity:
        return identity.casefold()
    title = " ".join(str(item.get("title") or item.get("name") or "").casefold().split())
    artist = item.get("artist") or item.get("author") or item.get("channel") or ""
    if isinstance(artist, dict):
        artist = artist.get("name") or artist.get("title") or ""
    artist_key = " ".join(str(artist).casefold().split())
    return f"{title}|{artist_key}" if title and artist_key else ""


def canonicalize_candidate_pools(
    pools: Dict[str, List[DiscoveryCandidate]],
) -> Tuple[Dict[str, List[DiscoveryCandidate]], Dict[str, int]]:
    raw_track_count = 0
    unplayable_count = 0
    best_by_identity: Dict[str, DiscoveryCandidate] = {}
    for candidates in pools.values():
        for candidate in candidates or []:
            identity = _candidate_identity(candidate)
            if not identity:
                continue
            if candidate.item_type == "track":
                raw_track_count += 1
                provider = str(
                    candidate.item.get("provider")
                    or candidate.item.get("source_provider")
                    or ""
                ).strip().casefold()
                playable_id = str(
                    candidate.item.get("playable_source_id")
                    or candidate.item.get("videoId")
                    or candidate.item.get("id")
                    or ""
                ).strip()
                if (
                    not playable_id
                    or provider == "musicbrainz"
                    or playable_id.casefold().startswith("musicbrainz:")
                    or candidate.item.get("playable") is False
                ):
                    unplayable_count += 1
                    continue
            existing = best_by_identity.get(identity)
            if existing is None or playable_source_rank(candidate.item) > playable_source_rank(existing.item):
                best_by_identity[identity] = candidate

    output: Dict[str, List[DiscoveryCandidate]] = {}
    for source, candidates in pools.items():
        seen: set[str] = set()
        resolved: List[DiscoveryCandidate] = []
        for candidate in candidates or []:
            identity = _candidate_identity(candidate)
            best = best_by_identity.get(identity)
            if not identity or best is None or identity in seen:
                continue
            seen.add(identity)
            relation_fields = {
                key: value
                for key, value in (candidate.item or {}).items()
                if key.startswith("recommendation_")
                or key.startswith("related_")
                or key.startswith("radio_")
                or key in {"relation_type", "relation_strength", "radio_partition", "profile_spine"}
            }
            item = {**dict(best.item or {}), **relation_fields}
            item["canonical_entity_id"] = identity.removeprefix(f"{candidate.item_type}:")
            item["playable_source_rank"] = playable_source_rank(item)
            resolved.append(
                DiscoveryCandidate(
                    item=item,
                    source=source,
                    score=max(float(candidate.score or 0.0), float(best.score or 0.0)),
                    reasons=list(dict.fromkeys([*candidate.reasons, *best.reasons])),
                    item_type=candidate.item_type,
                )
            )
        output[source] = resolved
    unique_tracks = sum(
        1
        for identity, candidate in best_by_identity.items()
        if candidate.item_type == "track" and identity.startswith("track:")
    )
    return output, {
        "raw_track_count": raw_track_count,
        "canonical_unique_track_count": unique_tracks,
        "duplicate_track_count": max(raw_track_count - unique_tracks - unplayable_count, 0),
        "unplayable_track_count": unplayable_count,
    }


def _candidate_is_retained(candidate: DiscoveryCandidate) -> bool:
    item = candidate.item or {}
    provenance = str(item.get("source_provenance") or "").strip().casefold()
    relation = str(
        item.get("materialized_relation")
        or item.get("recommendation_path")
        or item.get("relation_type")
        or ""
    ).strip().casefold()
    if provenance == "enrichment:track_search" or relation in {
        "lane_query",
        "genre_mood_query",
    }:
        return False
    if candidate.item_type == "album" and (
        not str(item.get("musicbrainz_release_group_id") or "").strip()
        or not str(item.get("id") or item.get("browseId") or "").strip()
        or item.get("playable") is not True
        or int(item.get("track_count") or 0) < 2
    ):
        return False
    return str(item.get("negative_feedback_state") or "none").strip().lower() not in {
        "hidden",
        "removed",
        "blocked",
        "hard_suppressed",
    }


def candidate_inventory_coverage(
    pools: Dict[str, List[DiscoveryCandidate]],
    *,
    taste: TasteProfile,
) -> Dict[str, Any]:
    from .admission import candidate_profile_compatibility

    history_ids = {
        canonical_item_identity(track, item_type="track")
        for track in (
            taste.full_history_tracks
            + taste.recent_tracks
            + taste.top_tracks
            + taste.last_played_tracks
            + taste.anchor_tracks
        )
        if isinstance(track, dict)
    }
    history_ids.discard("")
    admitted_by_source: Dict[str, set[str]] = {}
    source_counts: Dict[str, int] = {}
    rejected: Dict[str, int] = {}
    for source, candidates in pools.items():
        if source.startswith("coverage_") or source == "popular_radio_cards":
            continue
        source_seen: set[str] = set()
        for candidate in candidates:
            if candidate.item_type != "track" or not _candidate_is_retained(candidate):
                continue
            identity = _candidate_identity(candidate)
            if not identity or identity in source_seen:
                continue
            relation = str(
                candidate.item.get("relation_type")
                or candidate.item.get("recommendation_path")
                or (candidate.reasons[0] if candidate.reasons else "")
            )
            compatibility = candidate_profile_compatibility(
                candidate,
                taste,
                row_kind="home_lane",
                relation_context=relation,
                taste_match=(
                    taste.is_cold_start
                    and (source == "genre_mood" or source.startswith("lane_"))
                ),
                strong_personal_match=source in {"history", "profile_spine"},
            )
            if not compatibility.allowed:
                reason = compatibility.rejection_reason or compatibility.reason or "rejected"
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            source_seen.add(identity)
            source_counts[source] = source_counts.get(source, 0) + 1
        admitted_by_source[source] = source_seen

    def union_count(*sources: str, unplayed: bool = False) -> int:
        identities: set[str] = set()
        for source in sources:
            identities.update(admitted_by_source.get(source, set()))
        if unplayed:
            identities.difference_update(history_ids)
        return len(identities)

    lane_sources = tuple(source for source in pools if source.startswith("lane_"))
    discovery_sources = ("similarity", "artist_graph", "genre_mood", "collaborative", *lane_sources)
    all_track_ids: set[str] = set()
    for identities in admitted_by_source.values():
        all_track_ids.update(identities)
    unplayed_ids = all_track_ids - history_ids

    album_ids = {
        _candidate_identity(candidate)
        for source, candidates in pools.items()
        if source == "album" or source.endswith("_albums")
        for candidate in candidates or []
        if candidate.item_type == "album" and _candidate_identity(candidate)
    }
    # Readiness must use the same album admission and diversity rules as the
    # published rows. Raw album identities previously reported 147 available
    # while both visible shelves admitted zero.
    from .config import ROW_RECIPES
    from .ranking import build_personal_mixes, rank_albums

    featured_albums, _ = rank_albums(
        pools,
        taste,
        ROW_RECIPES["featured_new_albums"],
    )
    featured_album_keys = {
        " ".join(str(item.get("id") or "").strip().casefold().split())
        for item in featured_albums
    } | {
        " ".join(
            f"{item.get('title') or ''}|{item.get('artist') or ''}"
            .strip()
            .casefold()
            .split()
        )
        for item in featured_albums
    }
    featured_album_keys.discard("")
    recommended_albums, _ = rank_albums(
        pools,
        taste,
        ROW_RECIPES["recommended_albums"],
        exclude_album_keys=featured_album_keys,
    )
    personal_mixes, personal_mix_diagnostics = build_personal_mixes(pools, taste)
    complete_personal_mixes = [
        mix
        for mix in personal_mixes
        if len(mix.get("tracks") or mix.get("items") or []) >= 8
    ]
    reserved_personal_mix_tracks = sum(
        min(len(mix.get("tracks") or mix.get("items") or []), 8)
        for mix in complete_personal_mixes
    )
    artist_ids = {
        " ".join(
            str(candidate.item.get("name") or candidate.item.get("artist") or "")
            .strip()
            .casefold()
            .split()
        )
        for source in ("artist_graph", "similarity", "collaborative", "profile_spine", "popularity")
        for candidate in pools.get(source, []) or []
        if candidate.item_type == "artist"
        or str(candidate.item.get("name") or candidate.item.get("artist") or "").strip()
    }
    artist_ids.discard("")
    radio_card_count = len(pools.get("popular_radio_cards") or [])
    history_count = len(
        {
            canonical_item_identity(track, item_type="track")
            for track in taste.last_played_tracks
            if isinstance(track, dict)
        }
        - {""}
    )
    frequent_count = len(
        {
            canonical_item_identity(track, item_type="track")
            for track in taste.frequent_tracks
            if int(track.get("play_count") or 0) > 0
        }
        - {""}
    )
    minimums = {
        "todays_pick": 6,
        "made_for_you_tracks": 40,
        "recommended_artists": 10,
        "quiet_picks": 20,
    }
    actual = {
        "unique_tracks": len(all_track_ids),
        "unplayed_tracks": len(unplayed_ids),
        "todays_pick": union_count("similarity", "artist_graph", "genre_mood", "profile_spine", "popularity", unplayed=True),
        "featured_new_albums": len(featured_albums),
        "made_for_you_tracks": reserved_personal_mix_tracks,
        "made_for_you_mix_count": len(complete_personal_mixes),
        "because_you_played": union_count("similarity", "artist_graph", "profile_spine", unplayed=True),
        "popular_radio": radio_card_count,
        "recommended_albums": len(recommended_albums),
        "qualified_album_inventory": len(featured_albums) + len(recommended_albums),
        "raw_album_identities": len(album_ids),
        "recommended_artists": len(artist_ids),
        "quiet_picks": union_count(*discovery_sources, "profile_spine", unplayed=True),
        "last_played": history_count,
        "frequently_listened": frequent_count,
        "by_source": source_counts,
    }
    if history_count >= 8:
        minimums["last_played"] = 8
    if frequent_count >= 8:
        minimums["frequently_listened"] = 8
    if taste.full_history_tracks or taste.recent_tracks or taste.anchor_tracks:
        minimums["because_you_played"] = 12
    failed = [
        key
        for key, required in minimums.items()
        if int(actual.get(key) or 0) < required
    ]
    return {
        "ready": not failed,
        "mode": "cold_start" if taste.is_cold_start else "established",
        "actual": actual,
        "minimums": minimums,
        "failed_contracts": failed,
        "admission_rejections": rejected,
        "made_for_you_diagnostics": personal_mix_diagnostics,
    }


def refresh_candidate_inventory_coverage(
    inventory: CandidateInventory,
    *,
    taste: TasteProfile,
) -> CandidateInventory:
    coverage = candidate_inventory_coverage(
        inventory.pools,
        taste=taste,
    )
    counts = dict(inventory.candidate_counts or {})
    counts["coverage_unique_tracks"] = int(
        (coverage.get("actual") or {}).get("unique_tracks") or 0
    )
    counts["coverage_ready"] = 1 if coverage.get("ready") is True else 0
    ledger = dict(inventory.acquisition_ledger or {})
    ledger["failed_domains"] = list(coverage.get("failed_contracts") or [])
    actual = dict(coverage.get("actual") or {})
    from .config import ROW_RECIPES

    ledger["album_shelf_counts"] = {
        kind: int(actual.get(kind) or 0)
        for kind in ("featured_new_albums", "recommended_albums")
    }
    ledger["album_shelf_shortages"] = {
        kind: max(ROW_RECIPES[kind].min_items - int(actual.get(kind) or 0), 0)
        for kind in ("featured_new_albums", "recommended_albums")
    }
    ledger["qualified_album_reserve_shortage"] = max(
        QUALIFIED_ALBUM_RESERVE_TARGET
        - int(actual.get("qualified_album_inventory") or 0),
        0,
    )
    ledger["row_reserve_targets"] = dict(ROW_RESERVE_TARGETS)
    ledger["row_reserve_shortages"] = {
        kind: max(target - int(actual.get(kind) or 0), 0)
        for kind, target in ROW_RESERVE_TARGETS.items()
    }
    ledger["row_reserves_ready"] = not any(
        ledger["row_reserve_shortages"].values()
    )
    ledger["optional_row_counts"] = {
        kind: int(actual.get(kind) or 0)
        for kind in (
            "featured_new_albums",
            "popular_radio",
            "recommended_albums",
        )
    }
    album_artist_counts: Dict[str, int] = {}
    for candidate in inventory.pools.get("album", []) or []:
        if candidate.item_type != "album" or candidate.item.get("playable") is not True:
            continue
        artist = " ".join(str(candidate.item.get("artist") or "").strip().casefold().split())
        if artist:
            album_artist_counts[artist] = album_artist_counts.get(artist, 0) + 1
    ledger["album_artist_counts"] = album_artist_counts

    expansion_artists: List[str] = []
    expansion_seen: set[str] = set()
    for pool_name in ("artist_graph", "similarity", "genre_mood", "popularity"):
        for candidate in inventory.pools.get(pool_name, []) or []:
            item = candidate.item or {}
            artist = str(item.get("name") or item.get("artist") or "").strip()
            key = " ".join(artist.casefold().split())
            if not key or key in expansion_seen:
                continue
            expansion_seen.add(key)
            expansion_artists.append(artist)
            if len(expansion_artists) >= 32:
                break
        if len(expansion_artists) >= 32:
            break
    ledger["album_expansion_artist_seeds"] = expansion_artists
    return replace(
        inventory,
        candidate_counts=counts,
        coverage=coverage,
        row_coverage={
            "ready": coverage.get("ready") is True,
            "actual": dict(coverage.get("actual") or {}),
            "minimums": dict(coverage.get("minimums") or {}),
            "failed_contracts": list(coverage.get("failed_contracts") or []),
        },
        acquisition_ledger=ledger,
    )


def merge_candidate_inventories(
    current: CandidateInventory,
    previous: CandidateInventory | None,
) -> CandidateInventory:
    if previous is None or previous.profile_fingerprint != current.profile_fingerprint:
        return current
    merged_pools: Dict[str, List[DiscoveryCandidate]] = {}
    for pool_name in dict.fromkeys([*current.pools.keys(), *previous.pools.keys()]):
        output: List[DiscoveryCandidate] = []
        seen: set[str] = set()
        for candidate in [
            *list(current.pools.get(pool_name) or []),
            *list(previous.pools.get(pool_name) or []),
        ]:
            if not _candidate_is_retained(candidate):
                continue
            identity = _candidate_identity(candidate)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            output.append(candidate)
            # Radio alone may require 12 x 24 canonical tracks.  The old 192
            # cap truncated an artist catalog mid-batch before eight cards
            # could qualify.
            if len(output) >= 512:
                break
        merged_pools[pool_name] = output
    counts = dict(current.candidate_counts or {})
    counts.update({name: len(values) for name, values in merged_pools.items()})
    counts["inventory_merged_previous_generation"] = 1
    return replace(
        current,
        pools=merged_pools,
        candidate_counts=counts,
        base_generation_id=previous.generation_id,
        acquisition_ledger={
            **dict(previous.acquisition_ledger or {}),
            **dict(current.acquisition_ledger or {}),
        },
        canonical_stats={
            key: int(previous.canonical_stats.get(key) or 0) + int(current.canonical_stats.get(key) or 0)
            for key in set(previous.canonical_stats) | set(current.canonical_stats)
        },
    )


def _intent_payload_get(server: Any, user_scope_id: str) -> Dict[str, Any] | None:
    key = _intent_key(user_scope_id)
    cached = None
    try:
        cached = get_session_store().get(key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return dict(cached)
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [INVENTORY_INTENT_NAMESPACE, key],
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["payload_json"] or "{}")
        return dict(decoded) if isinstance(decoded, dict) else None
    except Exception:
        return None
    finally:
        connection.close()


def _intent_payload_set(server: Any, user_scope_id: str, payload: Dict[str, Any]) -> bool:
    key = _intent_key(user_scope_id)
    from ..recommend.store_runtime import open_recommendation_store_connection

    persisted = False
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        connection = None
    if connection is not None:
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
                    INVENTORY_INTENT_NAMESPACE,
                    key,
                    INVENTORY_MODEL_VERSION,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ],
            )
            connection.commit()
            persisted = True
        except Exception:
            persisted = False
        finally:
            connection.close()
    try:
        get_session_store().set(key, payload, INVENTORY_INTENT_TTL_SECONDS)
        return True
    except Exception:
        return persisted


def append_inventory_intent_delta(
    server: Any,
    *,
    user_scope_id: str,
    item: Dict[str, Any],
    entity_type: str = "track",
    query: str = "",
) -> int:
    if not isinstance(item, dict) or not item:
        return 0
    with _inventory_lock(user_scope_id):
        current = _intent_payload_get(server, user_scope_id) or {}
        version = max(int(current.get("version") or 0), 0) + 1
        entries = [entry for entry in current.get("entries") or [] if isinstance(entry, dict)]
        entries.append(
            {
                "version": version,
                "entity_type": str(entity_type or "track"),
                "query": str(query or ""),
                "item": dict(item),
                "created_at": time.time(),
            }
        )
        payload = {
            "version": version,
            "entries": entries[-12:],
            "expires_at": time.time() + INVENTORY_INTENT_TTL_SECONDS,
        }
        return version if _intent_payload_set(server, user_scope_id, payload) else 0


def load_inventory_intent_delta(server: Any, user_scope_id: str) -> Dict[str, Any]:
    payload = _intent_payload_get(server, user_scope_id) or {}
    if float(payload.get("expires_at") or 0.0) <= time.time():
        return {}
    return payload


def apply_inventory_intent_delta(
    inventory: CandidateInventory,
    payload: Dict[str, Any] | None,
) -> CandidateInventory:
    if not isinstance(payload, dict):
        return inventory
    version = max(int(payload.get("version") or 0), 0)
    if version <= 0:
        return inventory
    candidates: List[DiscoveryCandidate] = []
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
            continue
        item = dict(entry["item"])
        item.update(
            {
                "source_authority": str(item.get("source_authority") or "canonical"),
                "source_provenance": "search_intent_delta",
                "recommendation_path": "direct_history",
                "relation_type": "direct_history",
                "relation_strength": max(float(item.get("relation_strength") or 0.0), 0.95),
                "session_intent_version": version,
            }
        )
        candidates.append(
            DiscoveryCandidate(
                item=item,
                source="search_intent_delta",
                score=10.0,
                reasons=["search_intent"],
                item_type=str(entry.get("entity_type") or "track"),
            )
        )
        for key in ("similar_tracks", "artist_tracks", "tracks"):
            for neighbor in item.get(key) or []:
                if not isinstance(neighbor, dict):
                    continue
                neighbors_item = dict(neighbor)
                neighbors_item.update(
                    {
                        "source_provenance": "search_intent_neighbor",
                        "recommendation_path": "artist_neighbor",
                        "relation_type": "artist_neighbor",
                        "relation_strength": max(float(neighbors_item.get("relation_strength") or 0.0), 0.82),
                        "session_intent_version": version,
                    }
                )
                candidates.append(
                    DiscoveryCandidate(
                        item=neighbors_item,
                        source="search_intent_delta",
                        score=8.0,
                        reasons=["artist_neighbor"],
                    )
                )
    if not candidates:
        return inventory
    pools = {name: list(values or []) for name, values in inventory.pools.items()}
    pools["profile_spine"] = [*candidates, *pools.get("profile_spine", [])]
    pools["similarity"] = [*candidates, *pools.get("similarity", [])]
    pools["search_intent_delta"] = candidates
    album_candidates = [candidate for candidate in candidates if candidate.item_type == "album"]
    artist_candidates = [candidate for candidate in candidates if candidate.item_type == "artist"]
    if album_candidates:
        pools["album"] = [*album_candidates, *pools.get("album", [])]
    if artist_candidates:
        pools["artist_graph"] = [*artist_candidates, *pools.get("artist_graph", [])]
    counts = dict(inventory.candidate_counts or {})
    counts["search_intent_delta"] = len(candidates)
    return replace(inventory, pools=pools, candidate_counts=counts, intent_version=version)


def clear_inventory_intent_delta(
    server: Any,
    user_scope_id: str,
    *,
    consumed_version: int,
) -> bool:
    with _inventory_lock(user_scope_id):
        current = _intent_payload_get(server, user_scope_id) or {}
        if max(int(current.get("version") or 0), 0) != max(int(consumed_version or 0), 0):
            return False
        return _intent_payload_set(
            server,
            user_scope_id,
            {"version": 0, "entries": [], "expires_at": 0.0},
        )


def build_candidate_inventory(
    server: Any,
    taste: TasteProfile,
    *,
    previous: CandidateInventory | None = None,
    materialized_supply: "MaterializedCandidateSupply",
) -> CandidateInventory:
    pools, counts, timings = build_candidate_pools(
        server,
        taste,
        materialized_supply=materialized_supply,
    )
    diagnostics = dict(materialized_supply.diagnostics or {})
    counts.update(
        {
            "enrichment_planned_requests": int(diagnostics.get("planned_request_count") or 0),
            "enrichment_completed_requests": int(diagnostics.get("completed_request_count") or 0),
            "enrichment_failed_requests": int(diagnostics.get("failed_request_count") or 0),
        }
    )
    timings["enrichment_materialize"] = int(diagnostics.get("elapsed_ms") or 0)
    external_prefixes = (
        "similarity",
        "artist_graph",
        "genre_mood",
        "ytmusic_home",
        "popularity",
        "collaborative",
    )
    counts["inventory_external_call_groups"] = sum(
        1
        for name, elapsed in timings.items()
        if int(elapsed or 0) > 0 and str(name).startswith(external_prefixes)
    )
    history_ids = {
        str(track.get("id") or track.get("videoId") or "").strip()
        for track in (
            taste.recent_tracks
            + taste.top_tracks
            + taste.last_played_tracks
            + taste.anchor_tracks
        )
        if str(track.get("id") or track.get("videoId") or "").strip()
    }
    for source, candidates in pools.items():
        for candidate in candidates or []:
            item = candidate.item
            track_identity = str(item.get("id") or item.get("videoId") or "").strip()
            relation = str(
                item.get("recommendation_path")
                or item.get("recommendation_relation")
                or (candidate.reasons[0] if candidate.reasons else "")
                or "unproven"
            ).strip()
            canonical_identity = str(
                item.get("canonical_entity_id")
                or item.get("canonical_track_identity")
                or item.get("canonical_source_identity")
                or track_identity
            ).strip()
            source_authority = str(item.get("source_authority") or "unknown").strip()
            relation_strength = float(
                item.get("relation_strength")
                or _RELATION_STRENGTH.get(relation, _RELATION_STRENGTH.get(str(item.get("recommendation_path") or ""), 0.0))
            )
            item.update(
                {
                    "canonical_entity_id": canonical_identity,
                    "playable_source_id": str(item.get("playable_source_id") or track_identity),
                    "source_authority": source_authority,
                    "source_provenance": str(item.get("source_provenance") or source),
                    "relation_type": relation,
                    "relation_strength": round(relation_strength, 4),
                    "novelty_score": 0.0 if track_identity in history_ids else max(float(item.get("novelty_score") or 0.72), 0.0),
                    "negative_feedback_state": str(item.get("negative_feedback_state") or "none"),
                    "catalog_updated_at": float(item.get("catalog_updated_at") or time.time()),
                }
            )

    pools, canonical_stats = canonicalize_candidate_pools(pools)
    counts.update({name: len(values or []) for name, values in pools.items()})
    counts.update(canonical_stats)

    # This pool is acquired from canonical per-artist discographies. Copying
    # ordinary feed candidates into radio-shaped aliases made the old inventory
    # look deep without giving any seed artist a complete catalog.
    counts["radio_artist_catalog"] = len(pools.get("radio_artist_catalog", []) or [])
    backbone_artist_counts: Dict[str, int] = {}
    backbone_artist_labels: Dict[str, str] = {}
    for source in (
        "genre_mood",
        "ytmusic_home",
        "popularity",
        *[name for name in pools if name.startswith("lane_")],
    ):
        for candidate in pools.get(source, []) or []:
            if candidate.item_type != "track":
                continue
            artist = str(
                candidate.item.get("artist")
                or candidate.item.get("author")
                or candidate.item.get("channel")
                or ""
            ).strip()
            key = " ".join(artist.casefold().split())
            if not key:
                continue
            backbone_artist_labels.setdefault(key, artist)
            backbone_artist_counts[key] = backbone_artist_counts.get(key, 0) + 1
    backbone_artist_seeds = [
        backbone_artist_labels[key]
        for key, _count in sorted(
            backbone_artist_counts.items(),
            key=lambda entry: (-entry[1], entry[0]),
        )[:16]
    ]
    canonical_artist_ids: Dict[str, str] = {}
    for candidates in pools.values():
        for candidate in candidates or []:
            artist = str(
                candidate.item.get("name") or candidate.item.get("artist") or ""
            ).strip()
            artist_id = str(
                candidate.item.get("musicbrainz_artist_id")
                or candidate.item.get("artist_mbid")
                or ""
            ).strip()
            if artist and artist_id:
                canonical_artist_ids.setdefault(
                    " ".join(artist.casefold().split()),
                    artist_id,
                )
    now = time.time()
    inventory = CandidateInventory(
        user_scope_id=taste.user_scope_id,
        profile_fingerprint=taste.profile_key,
        generated_at=now,
        expires_at=now + INVENTORY_TTL_SECONDS,
        pools=pools,
        candidate_counts=counts,
        provider_timings_ms=timings,
        generation_id=str(uuid.uuid4()),
        base_generation_id=str(previous.generation_id if previous is not None else ""),
        acquisition_ledger={
            "cycle": int((previous.acquisition_ledger if previous else {}).get("cycle") or 0) + 1,
            "anchor_cursor": int(
                dict(materialized_supply.diagnostics or {}).get("anchor_cursor_next")
                or (previous.acquisition_ledger if previous else {}).get("anchor_cursor")
                or 0
            ),
            "artist_cursor": int(
                dict(materialized_supply.diagnostics or {}).get("artist_cursor_next")
                or (previous.acquisition_ledger if previous else {}).get("artist_cursor")
                or 0
            ),
            "planned_request_count": int(
                dict(materialized_supply.diagnostics or {}).get("planned_request_count")
                or 0
            ),
            "completed_request_count": int(
                dict(materialized_supply.diagnostics or {}).get("completed_request_count")
                or 0
            ),
            "repeated_batch_count": int(
                dict(materialized_supply.diagnostics or {}).get("repeated_batch_count")
                or 0
            ),
            "request_progress": dict(
                dict(materialized_supply.diagnostics or {}).get("request_progress")
                or (previous.acquisition_ledger if previous else {}).get("request_progress")
                or {}
            ),
            "backbone_artist_seeds": backbone_artist_seeds,
            "canonical_artist_ids": {
                **dict((previous.acquisition_ledger if previous else {}).get("canonical_artist_ids") or {}),
                **canonical_artist_ids,
            },
        },
        canonical_stats=canonical_stats,
    )
    merged = merge_candidate_inventories(inventory, previous)
    return refresh_candidate_inventory_coverage(merged, taste=taste)
