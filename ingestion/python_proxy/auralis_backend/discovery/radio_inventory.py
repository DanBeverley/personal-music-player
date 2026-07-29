from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
import json
import time
import uuid

from ..recommend.store_runtime import open_recommendation_store_connection
from ..search.intelligence import load_catalog_artist_records
from ..storage.postgres import load_catalog_artist_payloads
from .candidates import artist_name
from .config import POPULAR_RADIO_CARD_MIN_TRACKS, POPULAR_RADIO_CARD_TARGET_TRACKS
from .inventory import canonical_item_identity
from .schema import DiscoveryCandidate, TasteProfile


RADIO_INVENTORY_NAMESPACE = "discovery_artist_radio_inventory"
RADIO_INVENTORY_VERSION = "artist-radio-inventory-v1"
RADIO_INVENTORY_TTL_SECONDS = 60 * 60 * 24


@dataclass
class ArtistRadioInventory:
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float
    generation_id: str
    cards: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return len(self.cards) >= 8


def _key(user_scope_id: str) -> str:
    return f"artist-radio-inventory:{str(user_scope_id or 'guest').strip() or 'guest'}"


def _artist_key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _track_key(item: Dict[str, Any]) -> str:
    return canonical_item_identity(item, item_type="track")


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


def build_artist_radio_inventory(
    taste: TasteProfile,
    pools: Dict[str, List[DiscoveryCandidate]],
    *,
    server: Any | None = None,
) -> ArtistRadioInventory:
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
    radio_candidates = [
        candidate
        for candidate in pools.get("radio_artist_catalog", []) or []
        if candidate.item_type == "track"
    ]
    persisted_artists = load_catalog_artist_payloads(
        str(
            (candidate.item or {}).get("artist_id")
            or (candidate.item or {}).get("artistId")
            or (candidate.item or {}).get("artist_browse_id")
            or ""
        ).strip()
        for candidate in radio_candidates
    )
    persisted_by_name = (
        load_catalog_artist_records(
            server,
            artist_names=(
                artist_name(candidate.item or {})
                for candidate in radio_candidates
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
    for persisted in persisted_artists.values():
        persisted_key = _artist_key(persisted.get("name"))
        persisted_thumbnail = str(persisted.get("thumbnail") or "").strip()
        if persisted_key and persisted_thumbnail:
            artist_artwork.setdefault(persisted_key, persisted_thumbnail)

    for candidate in radio_candidates:
        item = dict(candidate.item or {})
        identity = _track_key(item)
        item_artist_key = _artist_key(artist_name(item))
        item_artist_id = str(
            item.get("artist_id")
            or item.get("artistId")
            or item.get("artist_browse_id")
            or ""
        ).strip()
        persisted_artist = (
            persisted_artists.get(item_artist_id)
            or persisted_by_name.get(item_artist_key)
            or {}
        )
        item_artist_thumbnail = str(
            item.get("artist_thumbnail")
            or persisted_artist.get("thumbnail")
            or ""
        ).strip()
        if item_artist_key and item_artist_thumbnail:
            artist_artwork.setdefault(item_artist_key, item_artist_thumbnail)
        seed_names = list(item.get("radio_seed_artists") or [])
        if item.get("radio_seed_artist"):
            seed_names.append(item.get("radio_seed_artist"))
        for seed_name in dict.fromkeys(str(value or "").strip() for value in seed_names):
            seed_key = _artist_key(seed_name)
            if not seed_key or not identity or identity in seen_seed_tracks.setdefault(seed_key, set()):
                continue
            seen_seed_tracks[seed_key].add(identity)
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
        (key, "catalog")
        for key in _rotated(catalog_seeds, rotation_epoch * 2)
    )
    cards: List[Dict[str, Any]] = []
    visible_used: set[str] = set()
    prior_full_sets: List[set[str]] = []
    direct_cards = 0
    backbone_cards = 0
    similar_cards = 0
    rejected: Dict[str, int] = {}

    for seed_key, affinity in seeds:
        if len(cards) >= 12:
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
            thumbnail = str(
                track.get("artist_thumbnail")
                or artist_artwork.get(track_artist_key)
                or ""
            ).strip()
            if not thumbnail:
                continue
            if track_artist_key == seed_key:
                seed_artist_thumbnail = thumbnail
            artwork_artist_keys.add(track_artist_key)
            collage_artists.append(track_artist or labels.get(track_artist_key) or track_artist_key.title())
            collage_images.append(thumbnail)
            if len(collage_images) >= 5:
                break
        visible_ids = [_track_key(item) for item in selected[:8] if _track_key(item)]
        visible_used.update(visible_ids)
        prior_full_sets.append(full_set)
        cards.append(
            {
                "id": f"radio:artist_inventory:{seed_key}",
                "title": f"{label} Radio",
                "radio_title": f"{label} Radio",
                "artist_name": label,
                "seed_artist_key": seed_key,
                "seed_affinity": affinity,
                "radio_mode": "artist_inventory",
                "tracks": selected,
                "items": selected,
                "track_count": len(selected),
                "thumbnail": seed_artist_thumbnail
                or (collage_images[0] if collage_images else ""),
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
        elif affinity == "catalog":
            backbone_cards += 1
        else:
            similar_cards += 1

    if enforce_direct_ratio and cards and direct_cards / len(cards) < 0.60:
        direct_only = [card for card in cards if card.get("seed_affinity") == "direct"]
        non_direct = [card for card in cards if card.get("seed_affinity") != "direct"]
        max_total = min(int(len(direct_only) / 0.60), 12)
        cards = [*direct_only, *non_direct[: max(max_total - len(direct_only), 0)]]
        direct_cards = len(direct_only)
        backbone_cards = sum(card.get("seed_affinity") == "backbone" for card in cards)
        similar_cards = sum(card.get("seed_affinity") == "similar" for card in cards)
    now = time.time()
    return ArtistRadioInventory(
        user_scope_id=taste.user_scope_id,
        profile_fingerprint=taste.profile_key,
        generated_at=now,
        expires_at=now + RADIO_INVENTORY_TTL_SECONDS,
        generation_id=str(uuid.uuid4()),
        cards=cards,
        diagnostics={
            "card_count": len(cards),
            "direct_card_count": direct_cards,
            "backbone_card_count": backbone_cards,
            "similar_card_count": similar_cards,
            "rotation_epoch": rotation_epoch,
            "visible_track_count": len(visible_used),
            "minimum_tracks_per_card": POPULAR_RADIO_CARD_MIN_TRACKS,
            "target_tracks_per_card": POPULAR_RADIO_CARD_TARGET_TRACKS,
            "rejected": rejected,
        },
    )


def radio_card_candidates(inventory: ArtistRadioInventory | None) -> List[DiscoveryCandidate]:
    if inventory is None:
        return []
    return [
        DiscoveryCandidate(
            item=dict(card),
            source="artist_radio_inventory",
            score=10.0,
            reasons=["independent_artist_radio_inventory"],
            item_type="radio",
        )
        for card in inventory.cards
    ]
