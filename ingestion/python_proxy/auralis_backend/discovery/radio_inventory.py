from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
import json
import time
import uuid

from ..recommend.store_runtime import open_recommendation_store_connection
from ..search.intelligence import catalog_entity_key, load_catalog_artist_records
from ..storage.artist_artwork import attach_persisted_artist_artwork, artist_artwork_token
from .candidates import artist_name
from .config import POPULAR_RADIO_CARD_MIN_TRACKS, POPULAR_RADIO_CARD_TARGET_TRACKS
from .inventory import canonical_item_identity
from .schema import DiscoveryCandidate, TasteProfile


RADIO_INVENTORY_NAMESPACE = "discovery_artist_radio_inventory"
RADIO_INVENTORY_VERSION = "artist-radio-inventory-v3"
RADIO_INVENTORY_TTL_SECONDS = 60 * 60 * 24
RADIO_CATALOG_INDEX_NAMESPACE = "discovery_radio_catalog_index"
RADIO_CATALOG_INDEX_VERSION = "radio-catalog-index-v1"
RADIO_CATALOG_INDEX_MAX_TRACKS = 96
# Keep at least 24 cards available; normal background replenishment aims for
# a deeper 36-card reservoir so rotation can retain familiar supply.
RADIO_RESERVOIR_TARGET_CARDS = 36
RADIO_RESERVOIR_MAX_CARDS = 36
RADIO_DISCOVERY_RESERVE_TARGET_CARDS = 12
RADIO_VISIBLE_DISCOVERY_MINIMUM = 4
ACQUISITION_JOB_NAMESPACE = "discovery_acquisition_job"


@dataclass
class ArtistRadioInventory:
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float
    generation_id: str
    cards: List[Dict[str, Any]] = field(default_factory=list)
    # Materialized qualified supply. ``cards`` is the selected visible slice;
    # retaining the reservoir allows deterministic successor rotations without
    # re-running provider composition.
    reservoir_cards: List[Dict[str, Any]] = field(default_factory=list)
    # Ephemeral, unique repair inputs. They are deliberately omitted from the
    # persisted payload and scheduled only after local composition returns.
    artwork_repair_records: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        if len(self.cards) < 12:
            return False
        # Visible radio cards must have a usable seed thumbnail and at least
        # one verified collage image; a track catalog alone is not publishable.
        return all(
            str(card.get("thumbnail") or "").strip().startswith("/artist_artwork/")
            and str(card.get("seed_artist_identity") or "").strip()
            and str(card.get("artwork_owner_token") or "").strip()
            and str(card.get("artwork_owner_token") or "").strip()
            == str(card.get("seed_artist_identity_token") or "").strip()
            and str(card.get("seed_artist_identity_token") or "").strip()
            == artist_artwork_token(card.get("seed_artist_identity"))
            and str(card.get("artwork_owner_identity") or "").strip()
            == str(card.get("seed_artist_identity") or "").strip()
            and bool(
                [
                    str(value or "").strip()
                    for value in (card.get("collage_images") or [])
                    if str(value or "").strip().startswith("/artist_artwork/")
                ]
            )
            and len(card.get("tracks") or card.get("items") or [])
            >= POPULAR_RADIO_CARD_MIN_TRACKS
            for card in self.cards
        )


def _key(user_scope_id: str) -> str:
    return f"artist-radio-inventory:{str(user_scope_id or 'guest').strip() or 'guest'}"


def _artist_key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _track_key(item: Dict[str, Any]) -> str:
    return canonical_item_identity(item, item_type="track")


def _is_playable_radio_record(item: Dict[str, Any]) -> bool:
    """Require explicit playable evidence for customer-facing radio tracks."""
    for key in ("playable", "source_verified", "playback_verified"):
        if item.get(key) is True:
            return True
        if key in item and item.get(key) is False:
            return False
    return bool(
        str(item.get("playable_source_id") or item.get("videoId") or item.get("source_id") or "").strip()
    )


def _radio_seed_keys(item: Dict[str, Any], fallback: str = "") -> List[str]:
    values = list(item.get("radio_seed_artists") or [])
    if item.get("radio_seed_artist"):
        values.append(item.get("radio_seed_artist"))
    if not values and fallback:
        values.append(fallback)
    return list(dict.fromkeys(_artist_key(value) for value in values if _artist_key(value)))


def _artist_identity(record: Dict[str, Any]) -> str:
    cached = str(record.get("artwork_cache_identity") or "").strip()
    if cached.startswith(("provider:artist:", "musicbrainz:artist:")):
        return cached
    canonical = str(record.get("canonical_artist_id") or "").strip()
    if canonical.startswith(("provider:artist:", "musicbrainz:artist:")):
        return canonical
    mbid = str(record.get("musicbrainz_artist_id") or record.get("artist_mbid") or "").strip()
    if mbid:
        return mbid if mbid.startswith("musicbrainz:artist:") else f"musicbrainz:artist:{mbid}"
    provider = str(record.get("provider_artist_id") or record.get("artist_id") or record.get("id") or "").strip()
    if provider and not provider.startswith(("provider:artist:", "musicbrainz:artist:")):
        return f"provider:artist:{provider.casefold()}"
    return provider


def _record_matches_requested_artist(record: Dict[str, Any], item: Dict[str, Any], requested_key: str) -> bool:
    intrinsic = _artist_key(record.get("name") or record.get("artist_name") or record.get("artist"))
    if intrinsic and intrinsic != requested_key:
        return False
    requested_ids = {
        str(item.get(key) or "").strip().casefold()
        for key in ("artist_id", "artistId", "artist_browse_id", "musicbrainz_artist_id", "canonical_artist_id")
        if str(item.get(key) or "").strip()
    }
    record_ids = {
        str(record.get(key) or "").strip().casefold()
        for key in ("id", "provider_artist_id", "artist_id", "musicbrainz_artist_id", "canonical_artist_id")
        if str(record.get(key) or "").strip()
    }
    return not requested_ids or not record_ids or bool(requested_ids & record_ids)


def _radio_catalog_index_key(user_scope_id: str, seed_key: str) -> str:
    return f"{str(user_scope_id or 'guest').strip() or 'guest'}:{seed_key}"


def _load_radio_catalog_index(
    server: Any,
    user_scope_id: str,
    current: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Read the compact radio index without rescanning acquisition history.

    Prepared-feed composition is latency-sensitive.  The acquisition-job
    backfill below is a migration/maintenance operation and must not run for
    every successor.  This fast path performs one bounded indexed read,
    merges the already-materialized candidate pool, and only writes seeds
    whose compact contents actually changed.
    """
    merged: Dict[str, List[Dict[str, Any]]] = {
        key: list(values or []) for key, values in current.items()
    }
    connection = None
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return merged
    try:
        scope_prefix = (
            f"{str(user_scope_id or 'guest').strip() or 'guest'}:%"
        )
        rows = connection.execute(
            "SELECT entity_id, payload_json FROM recommendation_feature_store "
            "WHERE namespace = ? AND entity_id LIKE ?",
            [RADIO_CATALOG_INDEX_NAMESPACE, scope_prefix],
        ).fetchall()
        indexed_by_seed: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                continue
            seed_key = _artist_key(payload.get("seed_key"))
            if not seed_key:
                continue
            indexed_by_seed[seed_key] = [
                dict(item)
                for item in payload.get("tracks") or []
                if isinstance(item, dict)
            ]

        changed: Dict[str, List[Dict[str, Any]]] = {}
        for seed_key in set(indexed_by_seed) | set(merged):
            seen: set[str] = set()
            combined: List[Dict[str, Any]] = []
            for item in [
                *indexed_by_seed.get(seed_key, []),
                *merged.get(seed_key, []),
            ]:
                identity = _track_key(item)
                if (
                    not identity
                    or identity in seen
                    or not _is_playable_radio_record(item)
                ):
                    continue
                seen.add(identity)
                combined.append(dict(item))
                if len(combined) >= RADIO_CATALOG_INDEX_MAX_TRACKS:
                    break
            if not combined:
                continue
            merged[seed_key] = combined
            before = [
                _track_key(item) for item in indexed_by_seed.get(seed_key, [])
            ]
            after = [_track_key(item) for item in combined]
            if before != after:
                changed[seed_key] = combined

        now = time.time()
        for seed_key, tracks in changed.items():
            connection.execute(
                """INSERT INTO recommendation_feature_store(
                    namespace, entity_id, model_id, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace, entity_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at""",
                [
                    RADIO_CATALOG_INDEX_NAMESPACE,
                    _radio_catalog_index_key(user_scope_id, seed_key),
                    RADIO_CATALOG_INDEX_VERSION,
                    json.dumps(
                        {
                            "version": RADIO_CATALOG_INDEX_VERSION,
                            "seed_key": seed_key,
                            "tracks": tracks,
                            "updated_at": now,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ],
            )
        if changed:
            connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        connection.close()
    return merged


def _merge_radio_catalog_index(
    server: Any,
    user_scope_id: str,
    current: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Merge current candidate radio supply with persisted acquisition results.

    This is intentionally lazy/idempotent: completed acquisition rows are the
    durable source of truth and are compacted into one bounded row per seed.
    """
    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return current
    merged: Dict[str, List[Dict[str, Any]]] = {key: list(values or []) for key, values in current.items()}
    try:
        rows = connection.execute(
            "SELECT entity_id, payload_json, updated_at FROM recommendation_feature_store "
            "WHERE namespace = ? ORDER BY updated_at DESC LIMIT 500",
            [ACQUISITION_JOB_NAMESPACE],
        ).fetchall()
        source_cache: Dict[str, Dict[str, Any]] = {}
        source_keys: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            for raw in payload.get("results") or []:
                if isinstance(raw, dict):
                    source_keys.update({_track_key(raw), catalog_entity_key("track", raw)})
        source_keys.discard("")
        if source_keys:
            ordered_keys = list(source_keys)
            # Stay below conservative SQLite parameter limits while avoiding
            # the former per-record query pattern.
            for offset in range(0, len(ordered_keys), 400):
                key_batch = ordered_keys[offset : offset + 400]
                placeholders = ",".join("?" for _ in key_batch)
                source_rows = connection.execute(
                    "SELECT entity_key, source_provider, source_key, "
                    "source_authority, payload_json FROM catalog_entity_sources "
                    f"WHERE entity_type='track' AND entity_key IN ({placeholders})",
                    key_batch,
                ).fetchall()
                for source_row in source_rows:
                    try:
                        payload = json.loads(source_row["payload_json"] or "{}")
                    except Exception:
                        payload = {}
                    if (
                        str(payload.get("verification_state") or "").casefold()
                        == "verified"
                        and (
                            not payload.get("expires_at")
                            or float(payload.get("expires_at")) > time.time()
                        )
                    ):
                        source_cache[str(source_row["entity_key"])] = {
                            **payload,
                            "source_id": source_row["source_key"],
                            "playable_source_id": source_row["source_key"],
                            "playable": True,
                            "source_verified": True,
                            "provider": source_row["source_provider"],
                            "source_authority": source_row["source_authority"],
                        }
        touched: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict) or payload.get("pool") != "radio_artist_catalog":
                continue
            if payload.get("kind") not in {"canonical_artist_radio_catalog", "artist_radio_catalog"}:
                continue
            fallback = str(payload.get("key") or "").strip()
            results = payload.get("results")
            if not isinstance(results, list):
                continue
            for raw in results:
                if not isinstance(raw, dict) or not _track_key(raw):
                    continue
                if not _is_playable_radio_record(raw):
                    raw = {**raw, **(source_cache.get(_track_key(raw)) or source_cache.get(catalog_entity_key("track", raw)) or {})}
                    raw = raw if _is_playable_radio_record(raw) else None
                if raw is None:
                    continue
                for seed_key in _radio_seed_keys(raw, fallback):
                    touched.setdefault(seed_key, []).append(dict(raw))
        # Include already-indexed rows even if their source acquisition rows age out.
        seed_keys = set(merged) | set(touched)
        for seed_key in seed_keys:
            existing_row = connection.execute(
                "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
                [RADIO_CATALOG_INDEX_NAMESPACE, _radio_catalog_index_key(user_scope_id, seed_key)],
            ).fetchone()
            indexed: List[Dict[str, Any]] = []
            if existing_row:
                try:
                    value = json.loads(existing_row["payload_json"] or "{}")
                    indexed = [dict(item) for item in value.get("tracks") or [] if isinstance(item, dict)]
                except Exception:
                    indexed = []
            seen: set[str] = set()
            combined: List[Dict[str, Any]] = []
            for item in [*indexed, *merged.get(seed_key, []), *touched.get(seed_key, [])]:
                key = _track_key(item)
                if not key or key in seen or not _is_playable_radio_record(item):
                    continue
                seen.add(key)
                combined.append(dict(item))
                if len(combined) >= RADIO_CATALOG_INDEX_MAX_TRACKS:
                    break
            if combined:
                merged[seed_key] = combined
                connection.execute(
                    """INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, entity_id) DO UPDATE SET
                    model_id=excluded.model_id, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                    [RADIO_CATALOG_INDEX_NAMESPACE, _radio_catalog_index_key(user_scope_id, seed_key),
                     RADIO_CATALOG_INDEX_VERSION, json.dumps({"version": RADIO_CATALOG_INDEX_VERSION, "seed_key": seed_key, "tracks": combined, "updated_at": time.time()}, ensure_ascii=False), time.time()],
                )
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        connection.close()
    return merged


def _rotated(values: List[Any], offset: int) -> List[Any]:
    if len(values) <= 1:
        return list(values)
    normalized = max(int(offset or 0), 0) % len(values)
    return [*values[normalized:], *values[:normalized]]


def _rotated_tracks(
    values: List[Dict[str, Any]],
    history_ids: set[str],
    offset: int,
) -> List[Dict[str, Any]]:
    unplayed = [item for item in values if _track_key(item) not in history_ids]
    familiar = [item for item in values if _track_key(item) in history_ids]
    return [
        *_rotated(unplayed, offset),
        *_rotated(familiar, offset),
    ]


def _payload(inventory: ArtistRadioInventory) -> Dict[str, Any]:
    return {
        "radio_inventory_version": RADIO_INVENTORY_VERSION,
        "user_scope_id": inventory.user_scope_id,
        "profile_fingerprint": inventory.profile_fingerprint,
        "generated_at": inventory.generated_at,
        "expires_at": inventory.expires_at,
        "generation_id": inventory.generation_id,
        "cards": [dict(card) for card in inventory.cards],
        "reservoir_cards": [dict(card) for card in (inventory.reservoir_cards or inventory.cards)],
        "diagnostics": dict(inventory.diagnostics or {}),
    }


def _from_payload(payload: Dict[str, Any] | None) -> ArtistRadioInventory | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("radio_inventory_version") != RADIO_INVENTORY_VERSION:
        return None
    return ArtistRadioInventory(
        user_scope_id=str(payload.get("user_scope_id") or "guest"),
        profile_fingerprint=str(payload.get("profile_fingerprint") or ""),
        generated_at=float(payload.get("generated_at") or 0.0),
        expires_at=float(payload.get("expires_at") or 0.0),
        generation_id=str(payload.get("generation_id") or ""),
        cards=[dict(card) for card in payload.get("cards") or [] if isinstance(card, dict)],
        reservoir_cards=[dict(card) for card in payload.get("reservoir_cards") or payload.get("cards") or [] if isinstance(card, dict)],
        diagnostics=dict(payload.get("diagnostics") or {}),
    )


def load_artist_radio_inventory(
    server: Any,
    user_scope_id: str,
    *,
    profile_fingerprint: str = "",
) -> ArtistRadioInventory | None:
    try:
        connection = open_recommendation_store_connection(server)
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [RADIO_INVENTORY_NAMESPACE, _key(user_scope_id)],
        ).fetchone()
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if row is None:
        return None
    try:
        inventory = _from_payload(json.loads(row["payload_json"] or "{}"))
    except Exception:
        return None
    if inventory is None or (profile_fingerprint and inventory.profile_fingerprint != profile_fingerprint):
        return None
    if inventory.expires_at <= time.time():
        return None
    return inventory


def store_artist_radio_inventory(server: Any, inventory: ArtistRadioInventory) -> bool:
    try:
        connection = open_recommendation_store_connection(server)
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
                RADIO_INVENTORY_NAMESPACE,
                _key(inventory.user_scope_id),
                RADIO_INVENTORY_VERSION,
                json.dumps(_payload(inventory), ensure_ascii=False),
                time.time(),
            ],
        )
        connection.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            connection.close()
        except Exception:
            pass


def merge_store_artist_radio_inventory(
    server: Any,
    incoming: ArtistRadioInventory,
) -> bool:
    """Atomically merge qualified reservoir supply without clobbering state.

    Expansion workers may finish after a successor rotation has selected a
    newer visible slice.  The transaction reads that slice under ``BEGIN
    IMMEDIATE``, preserves it, and only appends/improves qualified reservoir
    cards.  Profile and expiry mismatches are treated as a no-op.
    """
    connection = None
    try:
        connection = open_recommendation_store_connection(server)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store "
            "WHERE namespace = ? AND entity_id = ?",
            [RADIO_INVENTORY_NAMESPACE, _key(incoming.user_scope_id)],
        ).fetchone()
        current = None
        if row is not None:
            try:
                current = _from_payload(json.loads(row["payload_json"] or "{}"))
            except Exception:
                current = None
        if current is not None:
            if (
                current.profile_fingerprint != incoming.profile_fingerprint
                or current.expires_at <= time.time()
            ):
                connection.rollback()
                return False
        if current is None:
            merged = incoming
        else:
            by_id: Dict[str, Dict[str, Any]] = {}
            order: List[str] = []
            current_reservoir = list(current.reservoir_cards or current.cards or [])
            incoming_reservoir = list(incoming.reservoir_cards or incoming.cards or [])
            current_selected_ids = {
                str(card.get("id") or "").strip()
                for card in current.cards or []
                if str(card.get("id") or "").strip()
            }

            def is_discovery(card: Dict[str, Any]) -> bool:
                return str(card.get("seed_affinity") or "").strip() in {
                    "related",
                    "exploratory",
                }

            # Fresh discovery supply gets first claim on bounded reserve slots;
            # the currently selected slice is then pinned before older excess
            # familiar cards. This cannot mutate active feed artifacts, but it
            # prevents a full direct-only reservoir from freezing forever.
            ordered_candidates = [
                *[card for card in incoming_reservoir if is_discovery(card)],
                *[card for card in current_reservoir if is_discovery(card)],
                *[
                    card
                    for card in current_reservoir
                    if str(card.get("id") or "").strip() in current_selected_ids
                ],
                *current_reservoir,
                *incoming_reservoir,
            ]
            for card in ordered_candidates:
                card_id = str(card.get("id") or "").strip()
                if not card_id:
                    continue
                candidate = dict(card)
                tracks = []
                seen_tracks: set[str] = set()
                for track in candidate.get("tracks") or candidate.get("items") or []:
                    if not isinstance(track, dict):
                        continue
                    track_id = _track_key(track)
                    if not track_id or track_id in seen_tracks:
                        continue
                    seen_tracks.add(track_id)
                    tracks.append(dict(track))
                if len(tracks) < POPULAR_RADIO_CARD_MIN_TRACKS:
                    continue
                candidate["tracks"] = tracks
                candidate["items"] = tracks
                if card_id not in by_id:
                    order.append(card_id)
                    by_id[card_id] = candidate
                elif len(tracks) > len(by_id[card_id].get("tracks") or []):
                    by_id[card_id] = candidate
            reservoir = [by_id[card_id] for card_id in order[:RADIO_RESERVOIR_MAX_CARDS]]
            progress = dict((incoming.diagnostics or {}).get("radio_expansion_progress") or {})
            prior_progress = dict((current.diagnostics or {}).get("radio_expansion_progress") or {})
            # Expansion workers carry the revision they observed.  A stale
            # worker may still contribute reservoir cards, but must not
            # overwrite cursor progress produced by a newer worker.
            current_revision = int(prior_progress.get("progress_revision") or 0)
            base_revision = progress.get("progress_base_revision")
            progress_is_current = (
                base_revision is None
                or int(base_revision or 0) == current_revision
            )
            if progress_is_current:
                for key, value in progress.items():
                    if value is not None and key != "progress_base_revision":
                        prior_progress[key] = value
            else:
                # Keep the durable revision and all cursor/request fields
                # from the newer completion; discard stale worker progress.
                prior_progress["progress_revision"] = current_revision
            merged = ArtistRadioInventory(
                user_scope_id=current.user_scope_id,
                profile_fingerprint=current.profile_fingerprint,
                generated_at=current.generated_at,
                expires_at=current.expires_at,
                generation_id=current.generation_id,
                cards=list(current.cards or []),
                reservoir_cards=reservoir,
                diagnostics={
                    **dict(current.diagnostics or {}),
                    "reservoir_size": len(reservoir),
                    "reservoir_merged_size": len(reservoir),
                    "reservoir_expansion_applied": True,
                    "discovery_card_count": sum(
                        str(card.get("seed_affinity") or "").strip()
                        in {"related", "exploratory"}
                        for card in reservoir
                    ),
                    "discovery_deficit": max(
                        RADIO_DISCOVERY_RESERVE_TARGET_CARDS
                        - sum(
                            str(card.get("seed_affinity") or "").strip()
                            in {"related", "exploratory"}
                            for card in reservoir
                        ),
                        0,
                    ),
                    "radio_expansion_progress": prior_progress,
                },
            )
        connection.execute(
            """INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace, entity_id) DO UPDATE SET
                model_id=excluded.model_id, payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
            [
                RADIO_INVENTORY_NAMESPACE,
                _key(merged.user_scope_id),
                RADIO_INVENTORY_VERSION,
                json.dumps(_payload(merged), ensure_ascii=False),
                time.time(),
            ],
        )
        connection.commit()
        return True
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            connection.close()
        except Exception:
            pass


def build_artist_radio_inventory(
    taste: TasteProfile,
    pools: Dict[str, List[DiscoveryCandidate]],
    *,
    server: Any | None = None,
    excluded_card_ids: set[str] | None = None,
    excluded_track_ids: set[str] | None = None,
) -> ArtistRadioInventory:
    index_started = time.perf_counter()
    history_ids = {
        _track_key(track)
        for track in [*taste.full_history_tracks, *taste.recent_tracks, *taste.top_tracks]
        if isinstance(track, dict)
    }
    direct_scores: Dict[str, float] = {}
    labels: Dict[str, str] = {}

    def add_seed(name: str, score: float) -> None:
        key = _artist_key(name)
        if not key:
            return
        labels.setdefault(key, str(name).strip())
        direct_scores[key] = direct_scores.get(key, 0.0) + score

    for track in taste.full_history_tracks:
        name = artist_name(track)
        engagement = (
            (2.5 if track.get("is_favorite") is True else 0.0)
            + (1.5 if track.get("in_library") is True or track.get("saved") is True else 0.0)
            + (1.0 if track.get("completed") is True else 0.0)
        )
        add_seed(
            name,
            2.0 + engagement + min(float(track.get("play_count") or 0), 20.0) * 0.35,
        )
    for name in taste.top_artists:
        add_seed(name, 5.0)
    for name in taste.artist_hints:
        add_seed(name, 2.5)

    feedback = dict((taste.source_profile or {}).get("negative_feedback") or {})
    if isinstance(feedback.get("by_type"), dict):
        feedback = dict(feedback.get("by_type") or {})
    artist_feedback = dict(feedback.get("artist_cluster") or {})
    for key, value in artist_feedback.items():
        artist_key = _artist_key(key)
        try:
            strength = float(value or 0.0)
        except (TypeError, ValueError):
            strength = 0.0
        if artist_key not in direct_scores or strength <= 0:
            continue
        if strength >= 0.85:
            direct_scores.pop(artist_key, None)
        else:
            direct_scores[artist_key] -= strength * 5.0

    catalog_by_seed: Dict[str, List[Dict[str, Any]]] = {}
    seen_seed_tracks: Dict[str, set[str]] = {}
    artist_artwork: Dict[str, str] = {}
    artist_identity_by_key: Dict[str, str] = {}
    artwork_repair_records: Dict[str, Dict[str, Any]] = {}
    radio_candidates = [
        candidate
        for candidate in pools.get("radio_artist_catalog", []) or []
        if candidate.item_type == "track"
    ]
    # Acquisition results are persisted independently of candidate inventory;
    # lazily compact them into the durable per-seed index and consume locally.
    catalog_seed_items: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in radio_candidates:
        item = dict(candidate.item or {})
        seed_keys = _radio_seed_keys(item)
        provenance = str(item.get("radio_catalog_role") or item.get("relationship_provenance") or "").casefold()
        actual_artist = _artist_key(artist_name(item))
        if actual_artist and provenance in {"neighbor", "neighbor_artist", "artist_graph", "related", "relationship"}:
            seed_keys.append(actual_artist)
            item.setdefault("radio_discovery_provenance", provenance)
        for seed_key in dict.fromkeys(seed_keys):
            catalog_seed_items.setdefault(seed_key, []).append(
                {**item, "_radio_inventory_seed_key": seed_key}
            )
    # Reuse already persisted playable candidate supply for direct taste
    # artists. This preserves own-artist identity without inventing relations.
    direct_seed_keys = set(direct_scores)
    for pool_name, candidates in pools.items():
        if pool_name == "radio_artist_catalog":
            continue
        for candidate in candidates or []:
            if getattr(candidate, "item_type", "track") != "track":
                continue
            item = dict(candidate.item or {})
            if not _is_playable_radio_record(item):
                continue
            artist_key = _artist_key(artist_name(item))
            if artist_key in direct_seed_keys:
                catalog_seed_items.setdefault(artist_key, []).append(
                    {**item, "radio_seed_artist": artist_key}
                )
    if server is not None:
        catalog_seed_items = _load_radio_catalog_index(
            server, taste.user_scope_id, catalog_seed_items
        )
    index_load_ms = round((time.perf_counter() - index_started) * 1000.0, 2)
    indexed_candidates = [
        item
        for seed_items in catalog_seed_items.values()
        for item in seed_items
    ]
    # Feed composition is local-only. PostgreSQL artist hydration has a
    # network/connect timeout and belongs in background acquisition, not the
    # successor queue's critical path.
    persisted_artists: Dict[str, Dict[str, Any]] = {}
    persisted_by_name = (
        load_catalog_artist_records(
            server,
            artist_names=(
                artist_name(item)
                for item in indexed_candidates
            ),
        )
        if server is not None
        else {}
    )
    for persisted in persisted_by_name.values():
        persisted_id = str(
            persisted.get("provider_artist_id")
            or persisted.get("id")
            or ""
        ).strip()
        if persisted_id:
            persisted_artists.setdefault(persisted_id, persisted)
    def verified_artist_thumbnail(record: Dict[str, Any]) -> str:
        if server is None or not record:
            return ""
        attached = attach_persisted_artist_artwork(server, record)
        thumbnail = str(attached.get("thumbnail") or "").strip()
        if thumbnail.startswith("/artist_artwork/"):
            # This record was loaded from the durable catalog and is already
            # verified. Rewriting it for every queue composition adds many
            # serialized SQLite commits without improving its authority.
            return thumbnail
        repair_key = str(
            attached.get("canonical_artist_id")
            or attached.get("provider_artist_id")
            or attached.get("id")
            or attached.get("name")
            or ""
        ).strip().casefold()
        if repair_key and repair_key not in artwork_repair_records:
            # Composition is strictly local. Record a unique repair need for
            # the bounded background scheduler; do not perform provider/R2
            # HEADs (or enqueue work) on the queue-critical path.
            artwork_repair_records[repair_key] = dict(record)
        return ""

    for persisted in persisted_artists.values():
        persisted_key = _artist_key(persisted.get("name"))
        persisted_thumbnail = verified_artist_thumbnail(dict(persisted))
        if persisted_key and persisted_thumbnail:
            artist_artwork.setdefault(persisted_key, persisted_thumbnail)
            identity = _artist_identity(persisted)
            if identity:
                artist_identity_by_key.setdefault(persisted_key, identity)

    for item in indexed_candidates:
        track_identity = _track_key(item)
        item_artist_key = _artist_key(artist_name(item))
        item_artist_id = str(
            item.get("artist_id")
            or item.get("artistId")
            or item.get("artist_browse_id")
            or ""
        ).strip()
        persisted_artist = persisted_artists.get(item_artist_id) or {}
        if not persisted_artist:
            by_name = persisted_by_name.get(item_artist_key) or {}
            if _record_matches_requested_artist(by_name, item, item_artist_key):
                persisted_artist = by_name
        artwork_record = {
            **dict(persisted_artist),
            "name": str(
                persisted_artist.get("name") or artist_name(item) or ""
            ).strip(),
            "id": str(
                persisted_artist.get("id") or item_artist_id or ""
            ).strip(),
            "provider_artist_id": str(
                persisted_artist.get("provider_artist_id")
                or item_artist_id
                or ""
            ).strip(),
        }
        candidate_thumbnail = str(item.get("artist_thumbnail") or "").strip()
        if candidate_thumbnail.startswith(("http://", "https://")):
            artwork_record.setdefault("artwork_source_url", candidate_thumbnail)
            artwork_record.setdefault("thumbnail", candidate_thumbnail)
        item_artist_thumbnail = verified_artist_thumbnail(artwork_record)
        if item_artist_key and item_artist_thumbnail:
            artist_artwork.setdefault(item_artist_key, item_artist_thumbnail)
            identity = _artist_identity(artwork_record)
            if identity:
                artist_identity_by_key.setdefault(item_artist_key, identity)
        seed_names = list(item.get("radio_seed_artists") or [])
        if item.get("radio_seed_artist"):
            seed_names.append(item.get("radio_seed_artist"))
        if item.get("_radio_inventory_seed_key"):
            seed_names.append(item.get("_radio_inventory_seed_key"))
        if not seed_names:
            # Index rows are keyed by seed; preserve that relationship when
            # provider payloads omit the seed annotation.
            seed_names = [seed for seed, values in catalog_seed_items.items() if item in values]
        for seed_name in dict.fromkeys(str(value or "").strip() for value in seed_names):
            seed_key = _artist_key(seed_name)
            if not seed_key or not track_identity or track_identity in seen_seed_tracks.setdefault(seed_key, set()):
                continue
            seen_seed_tracks[seed_key].add(track_identity)
            labels.setdefault(seed_key, seed_name)
            catalog_by_seed.setdefault(seed_key, []).append(item)

    direct_seeds = [
        key
        for key, score in sorted(direct_scores.items(), key=lambda entry: entry[1], reverse=True)
        if score > 0
    ]
    rotation_epoch = max(int(getattr(taste, "rotation_epoch", 0) or 0), 0)
    direct_seeds = _rotated(direct_seeds, rotation_epoch * 3)
    enforce_direct_ratio = len(direct_seeds) >= 5
    seeds = [(key, "direct") for key in direct_seeds if key in catalog_by_seed]
    catalog_seeds = [key for key in catalog_by_seed if key not in direct_scores]
    seeds.extend(
        (key, "related" if any(
            _artist_key(item.get("related_to_artist")) or item.get("artist_graph") or item.get("source") == "artist_graph"
            for item in catalog_by_seed.get(key, [])
        ) else "exploratory")
        for key in _rotated(catalog_seeds, rotation_epoch * 2)
    )
    qualified_independent_seed_keys = {
        seed_key
        for seed_key, values in catalog_by_seed.items()
        if len(
            {
                _track_key(item)
                for item in values
                if _artist_key(artist_name(item)) == seed_key and _track_key(item)
            }
        )
        >= POPULAR_RADIO_CARD_MIN_TRACKS
    }
    cards: List[Dict[str, Any]] = []
    visible_used: set[str] = set()
    prior_full_sets: List[set[str]] = []
    direct_cards = 0
    backbone_cards = 0
    similar_cards = 0
    rejected: Dict[str, int] = {}

    for seed_key, affinity in seeds:
        # Keep a bounded multi-rotation reservoir.  A later successor selects
        # from this durable supply while excluding active/queued identities.
        if len(cards) >= RADIO_RESERVOIR_MAX_CARDS:
            break
        catalog = catalog_by_seed.get(seed_key, [])
        own = sorted(
            [item for item in catalog if _artist_key(artist_name(item)) == seed_key],
            key=lambda item: (_track_key(item) in history_ids, -float(item.get("playable_source_rank") or 0.0)),
        )
        related = [
            item
            for item in catalog
            if _artist_key(artist_name(item)) != seed_key
            and _artist_key(item.get("related_to_artist")) == seed_key
            and _artist_key(artist_name(item)) not in qualified_independent_seed_keys
        ]
        own = _rotated_tracks(own, history_ids, rotation_epoch * 5)
        related = _rotated_tracks(related, history_ids, rotation_epoch * 3)
        selected: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for source, limit in (
            (own, 8),
            (related, 4),
            (own, POPULAR_RADIO_CARD_TARGET_TRACKS),
            (related, POPULAR_RADIO_CARD_TARGET_TRACKS),
        ):
            taken = 0
            for item in source:
                identity = _track_key(item)
                if not identity or identity in seen:
                    continue
                if len(selected) < 8 and identity in visible_used:
                    continue
                seen.add(identity)
                selected.append(item)
                taken += 1
                if len(selected) >= POPULAR_RADIO_CARD_TARGET_TRACKS or taken >= limit:
                    break
            if len(selected) >= POPULAR_RADIO_CARD_TARGET_TRACKS:
                break
        if len(selected) < POPULAR_RADIO_CARD_MIN_TRACKS:
            reason = f"below_{POPULAR_RADIO_CARD_MIN_TRACKS}_tracks"
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        full_set = {_track_key(item) for item in selected if _track_key(item)}
        card_id = f"radio:artist_inventory:{seed_key}"
        if excluded_card_ids and card_id in excluded_card_ids:
            continue
        if excluded_track_ids and full_set & excluded_track_ids:
            continue
        if any(len(full_set & prior) / max(len(full_set), 1) > 0.20 for prior in prior_full_sets):
            rejected["full_overlap"] = rejected.get("full_overlap", 0) + 1
            continue
        label = labels.get(seed_key) or artist_name(selected[0]) or seed_key.title()
        collage_images: List[str] = []
        collage_artists: List[str] = []
        artwork_artist_keys: set[str] = set()
        seed_artist_thumbnail = ""
        for track in selected:
            track_artist = artist_name(track)
            track_artist_key = _artist_key(track_artist)
            if not track_artist_key or track_artist_key in artwork_artist_keys:
                continue
            thumbnail = str(artist_artwork.get(track_artist_key) or "").strip()
            if not thumbnail:
                continue
            if track_artist_key == seed_key:
                seed_artist_thumbnail = thumbnail
            artwork_artist_keys.add(track_artist_key)
            collage_artists.append(track_artist or labels.get(track_artist_key) or track_artist_key.title())
            collage_images.append(thumbnail)
            if len(collage_images) >= 5:
                break
        # Seed artwork is an identity-bound contract. Related/collage artwork
        # is presentation-only and must never satisfy the seed thumbnail.
        card_thumbnail = seed_artist_thumbnail
        seed_identity = artist_identity_by_key.get(seed_key, "")
        if not seed_identity:
            seed_identity = str(selected[0].get("canonical_artist_id") or selected[0].get("musicbrainz_artist_id") or selected[0].get("artist_id") or "").strip()
        seed_token = artist_artwork_token(seed_identity) if seed_identity else ""
        thumbnail_token = card_thumbnail.rsplit("/", 1)[-1] if "/" in card_thumbnail else ""
        if (
            not card_thumbnail.startswith("/artist_artwork/")
            or not seed_token
            or thumbnail_token != seed_token
            or not collage_images
        ):
            rejected["missing_verified_artwork"] = (
                rejected.get("missing_verified_artwork", 0) + 1
            )
            continue
        visible_ids = [_track_key(item) for item in selected[:8] if _track_key(item)]
        visible_used.update(visible_ids)
        prior_full_sets.append(full_set)
        cards.append(
            {
                "id": card_id,
                "title": f"{label} Radio",
                "radio_title": f"{label} Radio",
                "artist_name": label,
                "seed_artist_key": seed_key,
                "seed_artist_identity": seed_identity,
                "seed_artist_identity_token": seed_token,
                "artwork_owner_identity": seed_identity,
                "artwork_owner_token": thumbnail_token,
                "seed_affinity": affinity,
                "radio_mode": "artist_inventory",
                "tracks": selected,
                "items": selected,
                "track_count": len(selected),
                "thumbnail": card_thumbnail,
                "collage_images": collage_images,
                "collage_artists": collage_artists,
                "related_artists": [
                    artist
                    for artist in collage_artists
                    if _artist_key(artist) != seed_key
                ],
            }
        )
        if affinity == "direct":
            direct_cards += 1
        elif affinity == "related":
            backbone_cards += 1
        else:
            similar_cards += 1

    if enforce_direct_ratio and cards and direct_cards / len(cards) < 0.60:
        direct_only = [card for card in cards if card.get("seed_affinity") == "direct"]
        non_direct = [card for card in cards if card.get("seed_affinity") != "direct"]
        max_total = min(
            int(len(direct_only) / 0.60),
            RADIO_RESERVOIR_MAX_CARDS,
        )
        cards = [*direct_only, *non_direct[: max(max_total - len(direct_only), 0)]]
        direct_cards = len(direct_only)
        backbone_cards = sum(card.get("seed_affinity") == "related" for card in cards)
        similar_cards = sum(card.get("seed_affinity") == "exploratory" for card in cards)
    now = time.time()
    discovery_cards = sum(
        card.get("seed_affinity") in {"related", "exploratory"}
        for card in cards
    )
    discovery_deficit = max(RADIO_DISCOVERY_RESERVE_TARGET_CARDS - discovery_cards, 0)
    return ArtistRadioInventory(
        user_scope_id=taste.user_scope_id,
        profile_fingerprint=taste.profile_key,
        generated_at=now,
        expires_at=now + RADIO_INVENTORY_TTL_SECONDS,
        generation_id=str(uuid.uuid4()),
        cards=cards,
        reservoir_cards=list(cards),
        artwork_repair_records=list(artwork_repair_records.values())[:16],
        diagnostics={
            "card_count": len(cards),
            "reservoir_size": len(cards),
            "reservoir_target_size": RADIO_RESERVOIR_TARGET_CARDS,
            "reservoir_replenishment_needed": (
                len(cards) < RADIO_RESERVOIR_TARGET_CARDS
                or discovery_deficit > 0
            ),
            "direct_card_count": direct_cards,
            "related_card_count": sum(card.get("seed_affinity") == "related" for card in cards),
            "exploratory_card_count": sum(card.get("seed_affinity") == "exploratory" for card in cards),
            "discovery_card_count": discovery_cards,
            "discovery_target_count": RADIO_DISCOVERY_RESERVE_TARGET_CARDS,
            "discovery_deficit": discovery_deficit,
            "backbone_card_count": backbone_cards,
            "similar_card_count": similar_cards,
            "rotation_epoch": rotation_epoch,
            "visible_track_count": len(visible_used),
            "minimum_tracks_per_card": POPULAR_RADIO_CARD_MIN_TRACKS,
            "target_tracks_per_card": POPULAR_RADIO_CARD_TARGET_TRACKS,
            "verified_artwork_card_count": sum(
                1
                for card in cards
                if str(card.get("thumbnail") or "").startswith(
                    "/artist_artwork/"
                )
            ),
            "artwork_repair_scheduled_count": len(artwork_repair_records),
            "artwork_repair_pending_count": len(artwork_repair_records),
            "artwork_contract_ready": len(cards) >= 12
            and all(
                str(card.get("thumbnail") or "").startswith(
                    "/artist_artwork/"
                )
                for card in cards
            ),
            "rejected": rejected,
            "radio_index_load_ms": index_load_ms,
            "radio_index_seed_count": len(catalog_seed_items),
            "radio_index_qualified_seed_count": sum(
                1 for values in catalog_seed_items.values()
                if len({_track_key(item) for item in values if _track_key(item)})
                >= POPULAR_RADIO_CARD_MIN_TRACKS
            ),
            "shortage_by_seed": {
                seed: max(
                    POPULAR_RADIO_CARD_MIN_TRACKS
                    - len({_track_key(item) for item in values if _track_key(item)}),
                    0,
                )
                for seed, values in catalog_seed_items.items()
                if len({_track_key(item) for item in values if _track_key(item)})
                < POPULAR_RADIO_CARD_MIN_TRACKS
            },
        },
    )


def radio_card_candidates(inventory: ArtistRadioInventory | None) -> List[DiscoveryCandidate]:
    if inventory is None or not inventory.is_ready:
        return []
    # The durable inventory may contain several rotations; the home row keeps
    # the product contract of a bounded 8-12 card slice.
    visible_cards = list(inventory.cards or [])[:20]
    return [
        DiscoveryCandidate(
            item=dict(card),
            source="artist_radio_inventory",
            score=10.0,
            reasons=["independent_artist_radio_inventory"],
            item_type="radio",
        )
        for card in visible_cards
    ]


def select_radio_rotation(
    inventory: ArtistRadioInventory | None,
    *,
    excluded_card_ids: set[str] | None = None,
    excluded_track_ids: set[str] | None = None,
    epoch: int = 0,
) -> ArtistRadioInventory | None:
    """Derive a deterministic visible slice from the persisted reservoir.

    Novelty is preferred, but it is not a publication gate.  A reservoir can
    legitimately have fewer than eight wholly-new cards after the active feed
    and its successor have claimed their identities.  In that case we use the
    remaining valid cards with the smallest overlap and rotate their tracks so
    the visible queue still changes.  Quality admission remains strict via
    :meth:`ArtistRadioInventory.is_ready`.
    """
    if inventory is None:
        return None
    reservoir = list(inventory.reservoir_cards or inventory.cards or [])
    excluded_card_ids = excluded_card_ids or set()
    excluded_track_ids = excluded_track_ids or set()
    novel = []
    fallback = []
    for card in reservoir:
        cid = str(card.get("id") or "")
        tracks = [
            dict(track)
            for track in (card.get("tracks") or card.get("items") or [])
            if isinstance(track, dict) and _track_key(track)
        ]
        if not tracks:
            continue
        # Do not weaken the card contract while selecting.  Invalid cards are
        # never admitted as a fallback or counted as novelty.
        if (
            not str(card.get("thumbnail") or "").startswith("/artist_artwork/")
            or not str(card.get("seed_artist_identity") or "").strip()
            or not str(card.get("artwork_owner_token") or "").strip()
            or str(card.get("artwork_owner_token") or "").strip()
            != str(card.get("seed_artist_identity_token") or "").strip()
            or str(card.get("seed_artist_identity_token") or "").strip()
            != artist_artwork_token(card.get("seed_artist_identity"))
            or str(card.get("artwork_owner_identity") or "").strip()
            != str(card.get("seed_artist_identity") or "").strip()
            or not any(
                str(value or "").startswith("/artist_artwork/")
                for value in card.get("collage_images") or []
            )
            or len(tracks) < POPULAR_RADIO_CARD_MIN_TRACKS
        ):
            continue
        track_ids = {_track_key(track) for track in tracks}
        card_overlap = cid in excluded_card_ids
        track_overlap = track_ids & excluded_track_ids
        overlap_score = int(card_overlap) + len(track_overlap)
        candidate = (dict(card), tracks, track_overlap, card_overlap)
        if not card_overlap and not track_overlap:
            novel.append(candidate)
        else:
            fallback.append((overlap_score, candidate))

    # Stable deterministic order with epoch rotation.  Novel cards are always
    # consumed before controlled fallbacks; fallback score minimizes repeated
    # card/track identities instead of failing the entire feed.
    epoch_value = max(int(epoch or 0), 0)
    novel.sort(key=lambda value: str(value[0].get("id") or ""))
    if novel:
        offset = epoch_value % len(novel)
        novel = novel[offset:] + novel[:offset]
    fallback.sort(key=lambda value: (value[0], str(value[1][0].get("id") or "")))
    fallback_candidates = [candidate for _score, candidate in fallback]

    target_limit = min(20, 16 + (max(int(epoch or 0), 0) % 5))
    discovery_novel = [
        candidate for candidate in novel
        if str(candidate[0].get("seed_affinity") or "") in {"related", "exploratory"}
    ]
    familiar_novel = [candidate for candidate in novel if candidate not in discovery_novel]
    discovery_fallback = [
        candidate for candidate in fallback_candidates
        if str(candidate[0].get("seed_affinity") or "") in {"related", "exploratory"}
    ]
    selected_candidates = [
        *familiar_novel[: max(target_limit - RADIO_VISIBLE_DISCOVERY_MINIMUM, 0)],
        *[ *discovery_novel, *discovery_fallback ][:RADIO_VISIBLE_DISCOVERY_MINIMUM],
    ]
    selected_candidates.extend(
        candidate for candidate in [*familiar_novel, *discovery_novel, *fallback_candidates]
        if candidate not in selected_candidates
    )
    selected_candidates = selected_candidates[:target_limit]
    selected = []
    selected_novel_count = 0
    selected_fallback_count = 0
    selected_track_overlap_count = 0
    selected_card_overlap_count = 0
    for card, tracks, track_overlap, card_overlap in selected_candidates:
        # Rotate toward tracks not used by the active/queued feeds.  If the
        # card has fewer than the full target after filtering, retain its
        # verified tracks as a controlled, quality-preserving fallback.
        preferred = [track for track in tracks if _track_key(track) not in excluded_track_ids]
        if preferred and len(preferred) >= POPULAR_RADIO_CARD_MIN_TRACKS:
            ordered = preferred
        else:
            ordered = tracks
        if ordered:
            track_offset = epoch_value % len(ordered)
            ordered = ordered[track_offset:] + ordered[:track_offset]
        card_copy = dict(card)
        card_copy["tracks"] = ordered
        card_copy["items"] = ordered
        selected.append(card_copy)
        if card_overlap or track_overlap:
            selected_fallback_count += 1
            selected_track_overlap_count += len(track_overlap)
            selected_card_overlap_count += int(card_overlap)
        else:
            selected_novel_count += 1

    overlap_count = len(fallback)
    valid_count = len(novel) + len(fallback_candidates)
    diagnostics = dict(inventory.diagnostics or {})
    reservoir_discovery_count = sum(
        str(card.get("seed_affinity") or "").strip()
        in {"related", "exploratory"}
        for card in reservoir
    )
    selected_discovery_count = sum(
        str(card.get("seed_affinity") or "").strip()
        in {"related", "exploratory"}
        for card in selected
    )
    discovery_deficit = max(
        RADIO_DISCOVERY_RESERVE_TARGET_CARDS - reservoir_discovery_count,
        0,
    )
    diagnostics.update({
        "reservoir_size": len(reservoir),
        "reservoir_target_size": RADIO_RESERVOIR_TARGET_CARDS,
        "reservoir_replenishment_needed": (
            len(reservoir) < RADIO_RESERVOIR_TARGET_CARDS
            or discovery_deficit > 0
        ),
        "available_valid_card_count": valid_count,
        "available_novel_card_count": len(novel),
        "available_fallback_card_count": len(fallback_candidates),
        "selected_card_count": len(selected),
        "novel_card_count": selected_novel_count,
        "fallback_card_count": selected_fallback_count,
        "novel_card_ratio": (selected_novel_count / max(len(selected), 1)),
        "fallback_card_ratio": (selected_fallback_count / max(len(selected), 1)),
        "fallback_track_overlap_count": selected_track_overlap_count,
        "fallback_card_overlap_count": selected_card_overlap_count,
        "rotation_epoch": epoch_value,
        # Repairs are inputs to background enrichment, never a property of
        # the selected valid slice. Do not carry stale universe-wide counts
        # into queue publication diagnostics.
        "artwork_repair_scheduled_count": 0,
        "artwork_repair_pending_count": 0,
        "reservoir_overlap_count": overlap_count,
        "reservoir_overlap_ratio": (
            overlap_count / max(len(reservoir), 1)
        ),
        "visible_target_count": 16,
        "visible_selected_limit": target_limit,
        "visible_minimum_count": 12,
        "visible_maximum_count": 20,
        "reservoir_discovery_card_count": reservoir_discovery_count,
        "selected_discovery_card_count": selected_discovery_count,
        "discovery_target_count": RADIO_DISCOVERY_RESERVE_TARGET_CARDS,
        "discovery_deficit": discovery_deficit,
        "reservoir_shortage": len(selected) < 12,
    })
    return ArtistRadioInventory(
        user_scope_id=inventory.user_scope_id,
        profile_fingerprint=inventory.profile_fingerprint,
        generated_at=inventory.generated_at,
        expires_at=inventory.expires_at,
        generation_id=inventory.generation_id,
        cards=selected,
        reservoir_cards=reservoir,
        artwork_repair_records=list(inventory.artwork_repair_records or []),
        diagnostics=diagnostics,
    )


def merge_radio_reservoirs(
    existing: ArtistRadioInventory | None,
    incoming: ArtistRadioInventory,
) -> ArtistRadioInventory:
    """Merge qualified local supply by stable card identity, bounded to 36."""
    if (
        existing is None
        or existing.profile_fingerprint != incoming.profile_fingerprint
        or existing.expires_at <= time.time()
    ):
        return incoming
    ordered_ids: List[str] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    # New qualified supply comes first so a previously full 36-card reservoir
    # can actually admit bounded enrichment instead of permanently retaining
    # the oldest identities. Existing-only cards fill the remaining reserve.
    for card in [
        *(incoming.reservoir_cards or incoming.cards or []),
        *(existing.reservoir_cards or existing.cards or []),
    ]:
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            continue
        if card_id not in by_id:
            ordered_ids.append(card_id)
        if card_id not in by_id:
            by_id[card_id] = dict(card)
    reservoir = [
        by_id[card_id]
        for card_id in ordered_ids[:RADIO_RESERVOIR_MAX_CARDS]
    ]
    diagnostics = {
        **dict(existing.diagnostics or {}),
        **dict(incoming.diagnostics or {}),
        "reservoir_previous_size": len(
            existing.reservoir_cards or existing.cards or []
        ),
        "reservoir_merged_size": len(reservoir),
    }
    return ArtistRadioInventory(
        user_scope_id=incoming.user_scope_id,
        profile_fingerprint=incoming.profile_fingerprint,
        generated_at=max(existing.generated_at, incoming.generated_at),
        expires_at=max(existing.expires_at, incoming.expires_at),
        generation_id=incoming.generation_id,
        cards=list(incoming.cards or []),
        reservoir_cards=reservoir,
        artwork_repair_records=list(incoming.artwork_repair_records or []),
        diagnostics=diagnostics,
    )
