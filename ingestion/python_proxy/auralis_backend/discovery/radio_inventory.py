from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping
import json
import time
import uuid

from ..recommend.store_runtime import open_recommendation_store_connection
from ..search.intelligence import catalog_entity_key, load_catalog_artist_records
from ..storage.artist_artwork import attach_persisted_artist_artwork, artist_artwork_token
from .candidates import artist_name
from .config import POPULAR_RADIO_CARD_MIN_TRACKS, POPULAR_RADIO_CARD_TARGET_TRACKS
from .inventory import canonical_item_identity, playable_source_rank
from .schema import DiscoveryCandidate, TasteProfile


RADIO_INVENTORY_NAMESPACE = "discovery_artist_radio_inventory"
RADIO_INVENTORY_VERSION = "artist-radio-inventory-v5"
RADIO_INVENTORY_TTL_SECONDS = 60 * 60 * 24
RADIO_CATALOG_INDEX_NAMESPACE = "discovery_radio_catalog_index"
RADIO_CATALOG_INDEX_VERSION = "radio-catalog-index-v1"
RADIO_CATALOG_INDEX_MAX_TRACKS = 96
# Storage ceiling only. Readiness and replenishment are driven by the visible
# row plus missing unexposed/discovery supply, not by filling all 36 slots.
RADIO_RESERVOIR_TARGET_CARDS = 36
RADIO_RESERVOIR_MAX_CARDS = 36
RADIO_WORKING_RESERVOIR_TARGET_CARDS = 15
RADIO_VISIBLE_DISCOVERY_MINIMUM = 4
# A feed slice aims for six genuinely related/exploratory seeds. Four truly
# unexposed identities remain the minimum freshness admission threshold.
RADIO_VISIBLE_DISCOVERY_TARGET = 6
# One active rotation and its two queued successors must have independent
# discovery supply. A two-row reserve exhausted itself on the third rotation.
RADIO_DISCOVERY_RESERVE_TARGET_CARDS = RADIO_VISIBLE_DISCOVERY_TARGET * 3
RADIO_UNEXPOSED_TARGET_CARDS = RADIO_VISIBLE_DISCOVERY_TARGET * 2
RADIO_EXPOSURE_LEDGER_MAX_ENTRIES = 128  # five 20-card feeds, plus bounded margin
ACQUISITION_JOB_NAMESPACE = "discovery_acquisition_job"

_RADIO_PERSONAL_FIELDS = {
    "user_scope_id", "user_id", "account_id", "profile_key", "profile_fingerprint",
    "history", "history_ids", "play_count", "play_counts", "completed",
    "completion_count", "completed_at", "is_favorite", "favorite", "liked",
    "likes", "saved", "in_library", "last_played_at", "played_at", "seen_at",
    "negative_feedback_state", "negative_feedback", "feedback", "avoid_ids",
    "request_metadata", "canonical_query", "query", "session_id", "score",
    "personal_score", "taste_score", "interaction_score", "updated_at",
    "user", "profile", "session", "taste", "ranking", "rank", "timestamp",
    "timestamps", "completion", "completions",
}


def _sanitize_global_catalog_item(value: Dict[str, Any]) -> Dict[str, Any]:
    """Remove account/profile/ranking state before writing shared catalog rows."""
    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            output: Dict[str, Any] = {}
            for key, item in node.items():
                normalized = str(key or "").strip().casefold()
                if (
                    normalized in _RADIO_PERSONAL_FIELDS
                    or normalized.startswith(
                        (
                            "user_", "profile_", "history_", "personal_", "feedback_",
                            "score_", "interaction_", "engagement_", "taste_",
                        )
                    )
                    or normalized.endswith(("_score", "_at", "_timestamp"))
                ):
                    continue
                output[key] = clean(item)
            return output
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    return clean(dict(value or {}))


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
        if len(self.cards) < 10:
            return False
        # Visible radio cards must have a usable seed thumbnail and at least
        # one verified collage image; a track catalog alone is not publishable.
        qualified = all(
            str(card.get("thumbnail") or "").strip().startswith("/artist_artwork/")
            and str(card.get("seed_artist_identity") or "").strip()
            and (
                str(card.get("radio_mode") or "").strip() == "multi_artist_mix"
                or (
                    str(card.get("artwork_owner_token") or "").strip()
                    and str(card.get("artwork_owner_token") or "").strip()
                    == str(card.get("seed_artist_identity_token") or "").strip()
                    and str(card.get("seed_artist_identity_token") or "").strip()
                    == artist_artwork_token(card.get("seed_artist_identity"))
                    and str(card.get("artwork_owner_identity") or "").strip()
                    == str(card.get("seed_artist_identity") or "").strip()
                    and str(card.get("thumbnail") or "").strip()
                    == f"/artist_artwork/{artist_artwork_token(card.get('seed_artist_identity'))}"
                )
            )
            and bool(
                [
                    str(value or "").strip()
                    for value in (card.get("collage_images") or [])
                    if str(value or "").strip().startswith("/artist_artwork/")
                ]
            )
            and (str(card.get("radio_mode") or "").casefold() != "multi_artist_mix"
                 or _radio_mix_artwork_complete(card))
            and len(card.get("tracks") or card.get("items") or [])
            >= POPULAR_RADIO_CARD_MIN_TRACKS
            and all(_radio_metadata_complete(track) and _is_playable_radio_record(track)
                    for track in card.get("tracks") or card.get("items") or [])
            and len({_radio_source_key(track) for track in card.get("tracks") or card.get("items") or []})
            == len(card.get("tracks") or card.get("items") or [])
            for card in self.cards
        )
        if not qualified:
            return False
        from ..home_feed.contracts import evaluate_popular_radio_contract
        return evaluate_popular_radio_contract(self.cards).accepted


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
    playback = item.get("playback") if isinstance(item.get("playback"), dict) else {}
    return bool(str(item.get("playable_source_id") or item.get("videoId") or item.get("source_id")
                    or (playback.get("source_id") if playback.get("provider") else "") or "").strip())


def _radio_metadata_complete(item: Dict[str, Any]) -> bool:
    title = _artist_key(item.get("title") or item.get("name"))
    artist = _artist_key(artist_name(item))
    return bool(title and artist and title not in {"unknown", "unknown track", "untitled"}
                and artist not in {"unknown", "unknown artist"})


def _radio_source_key(item: Dict[str, Any]) -> str:
    playback = item.get("playback") if isinstance(item.get("playback"), dict) else {}
    source = str(playback.get("source_id") or item.get("playable_source_id") or item.get("playback_source_id")
                 or item.get("source_id") or item.get("videoId") or item.get("id") or _track_key(item)).strip()
    return source.removeprefix("youtube:").removeprefix("ytmusic:track:")


def _radio_mix_artwork_complete(card: Mapping[str, Any]) -> bool:
    """Mix images must belong to at least two distinct credited identities."""
    identities = {str(value or "").strip().casefold()
                  for value in card.get("seed_artist_identities") or []
                  if str(value or "").strip()}
    images = {str(value or "").strip() for value in card.get("collage_images") or []
              if str(value or "").startswith("/artist_artwork/")}
    expected = {f"/artist_artwork/{artist_artwork_token(identity)}" for identity in identities}
    return bool(len(identities) >= 2 and len(expected & images) >= 2
                and images.issubset(expected) and card.get("thumbnail") in expected)


def _merge_radio_tracks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reconcile the same physical source before checking publication facts."""
    merged: Dict[str, Dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        key = _radio_source_key(raw)
        if not key:
            continue
        previous = merged.get(key, {})
        # Sparse normalizations must never erase a known title/credit. New
        # complete metadata can repair retained placeholders for the same ID.
        richer = _radio_metadata_complete(raw) or not _radio_metadata_complete(previous)
        base, extra = (previous, raw) if richer else (raw, previous)
        merged[key] = {**base, **{k: v for k, v in extra.items() if v not in (None, "", [], {})}}
        if _radio_metadata_complete(extra):
            merged[key]["title"] = extra.get("title") or extra.get("name")
            merged[key]["artist"] = artist_name(extra)
    canonical: Dict[str, Dict[str, Any]] = {}
    for item in merged.values():
        if not _radio_metadata_complete(item) or not _is_playable_radio_record(item):
            continue
        identity = _track_key(item)
        previous = canonical.get(identity)
        if previous is None or playable_source_rank(item) > playable_source_rank(previous):
            canonical[identity] = item
    return list(canonical.values())


def _revalidate_radio_sources(
    connection: Any,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Reproject retained tracks from current persisted source proofs only."""
    from .source_registry import _apply_source, _recording_key, _verified_source

    keys = list(dict.fromkeys(
        key for item in items if isinstance(item, dict)
        for key in (_recording_key(item), _track_key(item), catalog_entity_key("track", item))
        if key
    ))
    source_ids = list(dict.fromkeys(
        _radio_source_key(item) for item in items if isinstance(item, dict)
        and _is_playable_radio_record(item)
    ))
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for column, values in (("entity_key", keys), ("source_key", source_ids)):
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT entity_key,source_provider,source_key,payload_json "
                "FROM catalog_entity_sources WHERE entity_type='track' AND source_provider='youtube' "
                f"AND {column} IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                signature = (row["entity_key"], row["source_provider"], row["source_key"])
                if signature in seen:
                    continue
                seen.add(signature)
                source = {
                    **json.loads(row["payload_json"] or "{}"),
                    "provider": row["source_provider"],
                    "source_id": row["source_key"],
                }
                by_key.setdefault(row["entity_key"], []).append(source)
                by_source.setdefault(row["source_key"], []).append(source)
    verified = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sources = [
            *by_key.get(_recording_key(item), []),
            *by_key.get(_track_key(item), []),
            *by_key.get(catalog_entity_key("track", item), []),
            *by_source.get(_radio_source_key(item), []),
        ]
        matched = _verified_source(sources, item)
        if matched is not None:
            verified.append(_apply_source(item, matched))
    return _merge_radio_tracks(verified)


def _radio_seed_keys(item: Dict[str, Any], fallback: str = "") -> List[str]:
    values = list(item.get("radio_seed_artists") or [])
    if item.get("radio_seed_artist"):
        values.append(item.get("radio_seed_artist"))
    if not values and fallback:
        values.append(fallback)
    return list(dict.fromkeys(_artist_key(value) for value in values if _artist_key(value)))


def _radio_card_identity(card: Dict[str, Any]) -> str:
    return _artist_key(
        card.get("seed_artist_identity")
        or card.get("seed_artist_key")
        or card.get("artist_name")
        or card.get("id")
    )


def _radio_card_artist_aliases(card: Dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for value in (
        card.get("seed_artist_identity"),
        card.get("seed_artist_key"),
        card.get("artist_name"),
        *(card.get("seed_artist_identities") or []),
    ):
        normalized = _artist_key(value)
        if not normalized:
            continue
        aliases.add(normalized)
        if normalized.startswith("artist:"):
            aliases.add(normalized.removeprefix("artist:"))
        elif not normalized.startswith(
            ("provider:artist:", "musicbrainz:artist:", "mix:")
        ):
            aliases.add(f"artist:{normalized}")
    return aliases


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


def _canonical_artist_cache_identity(
    seed_key: str,
    items: List[Dict[str, Any]] | None = None,
) -> str:
    normalized_seed = _artist_key(seed_key)
    if normalized_seed.startswith(("provider:artist:", "musicbrainz:artist:")):
        return normalized_seed
    for item in items or []:
        if _artist_key(artist_name(item)) != normalized_seed:
            continue
        mbid = str(item.get("musicbrainz_artist_id") or "").strip()
        if mbid:
            return f"musicbrainz:artist:{mbid.casefold()}"
        canonical = str(item.get("canonical_artist_id") or "").strip()
        if canonical.startswith(("provider:artist:", "musicbrainz:artist:")):
            return canonical.casefold()
        provider = str(
            item.get("provider_artist_id")
            or item.get("artist_id")
            or item.get("artistId")
            or ""
        ).strip()
        if provider:
            return f"provider:artist:{provider.casefold()}"
    return ""


def _radio_catalog_index_key(
    user_scope_id: str,
    seed_key: str,
    *,
    items: List[Dict[str, Any]] | None = None,
) -> str:
    identity = _canonical_artist_cache_identity(seed_key, items)
    if identity:
        return f"global:{identity}"
    # Name-only rows remain account-scoped until canonical artist evidence is
    # available; they must not contaminate the global cache.
    scope = str(user_scope_id or "guest").strip() or "guest"
    return f"{scope}:{_artist_key(seed_key)}"


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
        scope = str(user_scope_id or "guest").strip() or "guest"
        lookup_ids: set[str] = set()
        for seed_key, values in merged.items():
            lookup_ids.add(_radio_catalog_index_key(scope, seed_key, items=values))
            # Read the scoped name fallback during canonical migration.
            lookup_ids.add(f"{scope}:{_artist_key(seed_key)}")
        if lookup_ids:
            rows = []
            ordered_lookup_ids = sorted(lookup_ids)
            for offset in range(0, len(ordered_lookup_ids), 400):
                batch = ordered_lookup_ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        "SELECT entity_id, payload_json FROM recommendation_feature_store "
                        f"WHERE namespace = ? AND entity_id IN ({placeholders}) "
                        "ORDER BY updated_at DESC LIMIT 512",
                        [RADIO_CATALOG_INDEX_NAMESPACE, *batch],
                    ).fetchall()
                )
        else:
            rows = []
        indexed_by_seed: Dict[str, List[Dict[str, Any]]] = {}
        indexed_keys: Dict[str, str] = {}
        global_rows_needing_rewrite: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                continue
            if payload.get("version") not in (None, RADIO_CATALOG_INDEX_VERSION):
                continue
            cache_identity = _artist_key(payload.get("cache_identity"))
            if (
                str(row["entity_id"] or "").startswith("global:")
                and (
                    not cache_identity
                    or str(row["entity_id"]) != f"global:{cache_identity}"
                )
            ):
                continue
            seed_key = _artist_key(payload.get("seed_key"))
            if not seed_key:
                continue
            is_global = str(row["entity_id"] or "").startswith("global:")
            indexed_items: List[Dict[str, Any]] = []
            for item in payload.get("tracks") or []:
                if not isinstance(item, dict):
                    continue
                cleaned = _sanitize_global_catalog_item(item) if is_global else dict(item)
                if is_global and cleaned != item:
                    global_rows_needing_rewrite.add(seed_key)
                indexed_items.append(cleaned)
            indexed_by_seed.setdefault(seed_key, []).extend(indexed_items)
            if is_global or seed_key not in indexed_keys:
                indexed_keys[seed_key] = str(row["entity_id"])

        changed: Dict[str, List[Dict[str, Any]]] = {}
        for seed_key in set(indexed_by_seed) | set(merged):
            combined = _merge_radio_tracks([
                *indexed_by_seed.get(seed_key, []),
                *merged.get(seed_key, []),
            ])[:RADIO_CATALOG_INDEX_MAX_TRACKS]
            merged[seed_key] = combined
            if combined != indexed_by_seed.get(seed_key, []) or seed_key in global_rows_needing_rewrite:
                changed[seed_key] = combined

        now = time.time()
        for seed_key, tracks in changed.items():
            cache_key = indexed_keys.get(seed_key) or _radio_catalog_index_key(
                user_scope_id, seed_key, items=tracks
            )
            persisted_tracks = (
                [_sanitize_global_catalog_item(item) for item in tracks]
                if cache_key.startswith("global:")
                else tracks
            )
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
                    cache_key,
                    RADIO_CATALOG_INDEX_VERSION,
                    json.dumps(
                        {
                            "version": RADIO_CATALOG_INDEX_VERSION,
                            "seed_key": seed_key,
                            "cache_identity": _canonical_artist_cache_identity(
                                seed_key, tracks
                            ),
                            "tracks": persisted_tracks,
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
    allowed_seed_keys: set[str] | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Merge current candidate radio supply with persisted acquisition results.

    This is intentionally lazy/idempotent: completed acquisition rows are the
    durable source of truth and are compacted into one bounded row per seed.
    """
    from .source_registry import _apply_source, _verified_source

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
        source_cache: Dict[str, List[Dict[str, Any]]] = {}
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
                        source_cache.setdefault(str(source_row["entity_key"]), []).append({
                            **payload,
                            "source_id": source_row["source_key"],
                            "playable_source_id": source_row["source_key"],
                            "playable": True,
                            "source_verified": True,
                            "provider": source_row["source_provider"],
                            "source_authority": source_row["source_authority"],
                        })
        touched: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict) or payload.get("pool") != "radio_artist_catalog":
                continue
            if payload.get("kind") != "artist_catalog":
                continue
            fallback = str(payload.get("key") or "").strip()
            results = payload.get("results")
            if not isinstance(results, list):
                continue
            for raw in results:
                if not isinstance(raw, dict) or not _track_key(raw):
                    continue
                if not _is_playable_radio_record(raw):
                    sources = source_cache.get(_track_key(raw)) or source_cache.get(catalog_entity_key("track", raw)) or []
                    # Matching a canonical key alone does not prove this edition
                    # is playable. Reuse the exact existing source matcher before
                    # projecting physical playback onto a retained catalog row.
                    matched = _verified_source(sources, raw)
                    raw = _apply_source(raw, matched) if matched else None
                if raw is None:
                    continue
                for seed_key in _radio_seed_keys(raw, fallback):
                    if allowed_seed_keys is not None and seed_key not in allowed_seed_keys:
                        continue
                    touched.setdefault(seed_key, []).append(dict(raw))
        # Include already-indexed rows even if their source acquisition rows age out.
        seed_keys = set(merged) | set(touched)
        for seed_key in seed_keys:
            seed_items = [
                *merged.get(seed_key, []),
                *touched.get(seed_key, []),
            ]
            cache_key = _radio_catalog_index_key(
                user_scope_id, seed_key, items=seed_items
            )
            existing_row = connection.execute(
                "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
                [RADIO_CATALOG_INDEX_NAMESPACE, cache_key],
            ).fetchone()
            indexed: List[Dict[str, Any]] = []
            if existing_row:
                try:
                    value = json.loads(existing_row["payload_json"] or "{}")
                    expected_identity = _canonical_artist_cache_identity(
                        seed_key, seed_items
                    )
                    payload_identity = _artist_key(value.get("cache_identity"))
                    if (
                        value.get("version") not in (None, RADIO_CATALOG_INDEX_VERSION)
                        or cache_key.startswith("global:")
                        and payload_identity != expected_identity
                    ):
                        value = {}
                    indexed = [
                        (
                            _sanitize_global_catalog_item(item)
                            if cache_key.startswith("global:")
                            else dict(item)
                        )
                        for item in value.get("tracks") or []
                        if isinstance(item, dict)
                    ]
                except Exception:
                    indexed = []
            # Cached "playable" flags are not source proofs. This includes
            # old index rows and already-playable acquisition results, which
            # previously bypassed the edition matcher on every later build.
            combined = _revalidate_radio_sources(
                connection,
                [*indexed, *merged.get(seed_key, []), *touched.get(seed_key, [])],
            )[:RADIO_CATALOG_INDEX_MAX_TRACKS]
            if cache_key.startswith("global:"):
                combined = [
                    _sanitize_global_catalog_item(item) for item in combined
                ]
            merged[seed_key] = combined
            if combined or indexed:
                connection.execute(
                    """INSERT INTO recommendation_feature_store(namespace, entity_id, model_id, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, entity_id) DO UPDATE SET
                    model_id=excluded.model_id, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                    [RADIO_CATALOG_INDEX_NAMESPACE, cache_key,
                     RADIO_CATALOG_INDEX_VERSION, json.dumps({"version": RADIO_CATALOG_INDEX_VERSION, "seed_key": seed_key, "cache_identity": _canonical_artist_cache_identity(seed_key, combined), "tracks": combined, "updated_at": time.time()}, ensure_ascii=False), time.time()],
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


def artist_radio_inventory_to_payload(
    inventory: ArtistRadioInventory,
) -> Dict[str, Any]:
    """Serialize the independent radio reservoir for durable Home storage."""

    return _payload(inventory)


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


def artist_radio_inventory_from_payload(
    payload: Dict[str, Any] | None,
) -> ArtistRadioInventory | None:
    """Deserialize one radio reservoir from durable Home storage."""

    return _from_payload(payload)


def load_artist_radio_inventory(
    server: Any,
    user_scope_id: str,
    *,
    profile_fingerprint: str = "",
    allow_profile_mismatch: bool = False,
    allow_expired: bool = False,
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
    if inventory is None or (
        profile_fingerprint
        and inventory.profile_fingerprint != profile_fingerprint
        and not allow_profile_mismatch
    ):
        return None
    if inventory.expires_at <= time.time() and not allow_expired:
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


def merge_radio_exposure(
    server: Any,
    user_scope_id: str,
    identities: List[str],
    *,
    max_entries: int = RADIO_EXPOSURE_LEDGER_MAX_ENTRIES,
) -> bool:
    """Merge promotion exposure without clobbering concurrent expansion state."""
    values = [str(value or "").strip().casefold() for value in identities]
    values = list(dict.fromkeys(value for value in values if value))
    if not values:
        return False
    connection = None
    try:
        connection = open_recommendation_store_connection(server)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store "
            "WHERE namespace = ? AND entity_id = ?",
            [RADIO_INVENTORY_NAMESPACE, _key(user_scope_id)],
        ).fetchone()
        if row is None:
            connection.rollback()
            return False
        inventory = _from_payload(json.loads(row["payload_json"] or "{}"))
        if inventory is None:
            connection.rollback()
            return False
        diagnostics = dict(inventory.diagnostics or {})
        prior = [
            str(value or "").strip().casefold()
            for value in diagnostics.get("popular_radio_exposure_ledger") or []
            if str(value or "").strip()
        ]
        merged = list(dict.fromkeys([*values, *prior]))[:max(int(max_entries or 0), 1)]
        diagnostics["popular_radio_exposure_ledger"] = merged
        diagnostics["popular_radio_last_exposure_at"] = time.time()
        updated = replace(inventory, diagnostics=diagnostics)
        connection.execute(
            """UPDATE recommendation_feature_store
               SET model_id = ?, payload_json = ?, updated_at = ?
               WHERE namespace = ? AND entity_id = ?""",
            [
                RADIO_INVENTORY_VERSION,
                json.dumps(_payload(updated), ensure_ascii=False),
                time.time(),
                RADIO_INVENTORY_NAMESPACE,
                _key(user_scope_id),
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


def merge_store_artist_radio_inventory(
    server: Any,
    incoming: ArtistRadioInventory,
) -> bool:
    """Atomically merge qualified reservoir supply without clobbering state.

    Expansion workers may finish after another reservoir update. The
    transaction merges qualified cards under ``BEGIN IMMEDIATE`` and derives a
    complete visible rotation from that immutable reservoir before commit.
    Profile and expiry mismatches are treated as a no-op.
    """
    connection = None
    try:
        merge_now = time.time()

        def stamped_card(card: Dict[str, Any], fallback: float) -> Dict[str, Any]:
            value = dict(card)
            first_verified_at = float(value.get("first_verified_at") or fallback or merge_now)
            last_verified_at = float(
                value.get("last_verified_at") or first_verified_at
            )
            value["first_verified_at"] = first_verified_at
            value["last_verified_at"] = max(last_verified_at, first_verified_at)
            return value

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
        if current is not None and current.profile_fingerprint != incoming.profile_fingerprint:
            # Profile fingerprints describe ranking context, not ownership of
            # the durable radio supply. Keep the reservoir/cursors across
            # profile snapshots; the next selection applies the current taste
            # filter. Only reject a genuinely older completion.
            if float(incoming.generated_at or 0.0) < float(current.generated_at or 0.0):
                connection.rollback()
                return False
        if current is None:
            incoming_fallback = float(incoming.generated_at or merge_now)
            merged = replace(
                incoming,
                generated_at=max(float(incoming.generated_at or 0), merge_now),
                expires_at=max(
                    float(incoming.expires_at or 0),
                    merge_now + RADIO_INVENTORY_TTL_SECONDS,
                ),
                cards=[
                    stamped_card(card, incoming_fallback)
                    for card in incoming.cards or []
                ],
                reservoir_cards=[
                    stamped_card(card, incoming_fallback)
                    for card in incoming.reservoir_cards or incoming.cards or []
                ],
            )
            for card in merged.reservoir_cards:
                verified = _revalidate_radio_sources(
                    connection, card.get("tracks") or card.get("items") or [],
                )
                card["tracks"] = verified
                card["items"] = verified
        else:
            by_id: Dict[str, Dict[str, Any]] = {}
            order: List[str] = []
            current_reservoir = [
                stamped_card(card, float(current.generated_at or 0.0))
                for card in current.reservoir_cards or current.cards or []
            ]
            incoming_reservoir = [
                stamped_card(card, float(incoming.generated_at or merge_now))
                for card in incoming.reservoir_cards or incoming.cards or []
            ]
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

            def is_mix(card: Dict[str, Any]) -> bool:
                return str(card.get("radio_mode") or "").strip() == "multi_artist_mix"

            # Fresh discovery supply gets first claim on bounded reserve slots;
            # the currently selected slice is then pinned before older excess
            # familiar cards. This cannot mutate active feed artifacts, but it
            # prevents a full direct-only reservoir from freezing forever.
            ordered_candidates = [
                *[card for card in incoming_reservoir if is_discovery(card)],
                *[card for card in current_reservoir if is_discovery(card)],
                *[card for card in incoming_reservoir if is_mix(card)],
                *[card for card in current_reservoir if is_mix(card)],
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
                tracks = _revalidate_radio_sources(
                    connection, candidate.get("tracks") or candidate.get("items") or [],
                )
                if len(tracks) < POPULAR_RADIO_CARD_MIN_TRACKS:
                    continue
                candidate["tracks"] = tracks
                candidate["items"] = tracks
                if card_id not in by_id:
                    order.append(card_id)
                    by_id[card_id] = candidate
                else:
                    previous = by_id[card_id]
                    first_verified_at = min(
                        float(previous.get("first_verified_at") or merge_now),
                        float(candidate.get("first_verified_at") or merge_now),
                    )
                    last_verified_at = max(
                        float(previous.get("last_verified_at") or 0.0),
                        float(candidate.get("last_verified_at") or 0.0),
                    )
                    if (
                        float(candidate.get("last_verified_at") or 0.0)
                        >= float(previous.get("last_verified_at") or 0.0)
                    ):
                        by_id[card_id] = candidate
                    by_id[card_id]["first_verified_at"] = first_verified_at
                    by_id[card_id]["last_verified_at"] = max(
                        last_verified_at,
                        first_verified_at,
                    )
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
            current_own_counts = {
                str(key).strip().casefold(): int(value or 0)
                for key, value in dict(
                    (current.diagnostics or {}).get("own_track_count_by_seed")
                    or {}
                ).items()
                if str(key).strip()
            }
            incoming_own_counts = {
                str(key).strip().casefold(): int(value or 0)
                for key, value in dict(
                    (incoming.diagnostics or {}).get("own_track_count_by_seed")
                    or {}
                ).items()
                if str(key).strip()
            }
            merged_own_counts = {
                key: max(
                    int(current_own_counts.get(key) or 0),
                    int(incoming_own_counts.get(key) or 0),
                )
                for key in set(current_own_counts) | set(incoming_own_counts)
            }
            merged = ArtistRadioInventory(
                user_scope_id=current.user_scope_id,
                profile_fingerprint=incoming.profile_fingerprint or current.profile_fingerprint,
                generated_at=max(current.generated_at, incoming.generated_at),
                expires_at=max(time.time() + RADIO_INVENTORY_TTL_SECONDS, incoming.expires_at),
                generation_id=current.generation_id or incoming.generation_id,
                # ``cards`` is the publishable visible rotation, while
                # ``reservoir_cards`` is only its larger source pool. Keeping
                # an old empty selection here permanently trapped an otherwise
                # qualified reservoir in ``is_ready == False``.
                cards=[
                    stamped_card(
                        card,
                        float(incoming.generated_at or current.generated_at or merge_now),
                    )
                    for card in incoming.cards or current.cards or []
                ],
                reservoir_cards=reservoir,
                diagnostics={
                    **dict(current.diagnostics or {}),
                    "radio_supply_funnel": dict(
                        (incoming.diagnostics or {}).get("radio_supply_funnel")
                        or (current.diagnostics or {}).get("radio_supply_funnel")
                        or {}
                    ),
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
                    "exposure_deficit": int(
                        (incoming.diagnostics or {}).get("exposure_deficit") or 0
                    ),
                    "own_track_count_by_seed": merged_own_counts,
                    "radio_expansion_progress": prior_progress,
                },
            )
        # Always materialize the visible 10-15 card rotation from the merged
        # reservoir before publication. This is local/deterministic and never
        # opens provider or artwork work. It also repairs older persisted
        # inventories whose reservoir was populated while ``cards`` remained
        # empty.
        selected = select_radio_rotation(
            merged,
            epoch=max(
                int((merged.diagnostics or {}).get("rotation_epoch") or 0),
                0,
            ),
        )
        if selected is not None and selected.is_ready:
            merged = selected
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
        # A single historical play is not strong evidence of familiarity.
        # Keep it available as discovery supply, while repeated/completed or
        # explicitly saved/favourite tracks remain familiar anchors.
        play_count = float(track.get("play_count") or 0)
        engagement = (
            (2.5 if track.get("is_favorite") is True else 0.0)
            + (1.5 if track.get("in_library") is True or track.get("saved") is True else 0.0)
            + (1.0 if track.get("completed") is True else 0.0)
        )
        if engagement > 0.0 or play_count >= 2.0:
            add_seed(name, 2.0 + engagement + min(play_count, 20.0) * 0.35)
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
        for provenance_key in (
            "radio_discovery_provenance",
            "relationship_provenance",
            "radio_catalog_role",
        ):
            if item.get(provenance_key):
                artwork_record[provenance_key] = item.get(provenance_key)
        provenance_value = str(
            item.get("radio_discovery_provenance")
            or item.get("relationship_provenance")
            or item.get("radio_catalog_role")
            or ""
        ).casefold()
        if provenance_value in {"artist_graph", "related", "relationship", "neighbor", "neighbor_artist"}:
            artwork_record["seed_affinity"] = "related"
        elif provenance_value in {"popularity", "discovery_universe", "exploratory"}:
            artwork_record["seed_affinity"] = "exploratory"
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
    seeds = [(key, "direct") for key in direct_seeds if key in catalog_by_seed]
    catalog_seeds = [key for key in catalog_by_seed if key not in direct_scores]

    def catalog_seed_affinity(seed_key: str) -> str:
        values = catalog_by_seed.get(seed_key, [])
        provenance = {
            str(
                item.get("radio_discovery_provenance")
                or item.get("discovery_origin")
                or ""
            ).strip().casefold()
            for item in values
        }
        if provenance & {
            "popularity",
            "collaborative",
            "genre_mood",
            "ytmusic_home",
            "exploratory",
            "broad_global",
        } or any(value.startswith("lane_") for value in provenance):
            return "exploratory"
        if provenance & {
            "artist_graph",
            "related",
            "relationship",
            "neighbor",
            "neighbor_artist",
            "similarity",
        }:
            return "related"
        if any(
            _artist_key(item.get("related_to_artist"))
            or item.get("artist_graph")
            or item.get("source") == "artist_graph"
            for item in values
        ):
            return "related"
        return "exploratory"

    seeds.extend(
        (key, catalog_seed_affinity(key))
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
    now = time.time()
    cards: List[Dict[str, Any]] = []
    visible_used: set[str] = set()
    prior_full_sets: List[set[str]] = []
    direct_cards = 0
    backbone_cards = 0
    similar_cards = 0
    rejected: Dict[str, int] = {}

    seen_seed_identities: set[str] = set()
    for seed_key, affinity in seeds:
        # Keep a bounded multi-rotation reservoir.  A later successor selects
        # from this durable supply while excluding active/queued identities.
        if len(cards) >= RADIO_RESERVOIR_MAX_CARDS - 2:
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
        seed_identity = artist_identity_by_key.get(seed_key, "")
        if not seed_identity:
            for candidate_item in selected:
                if _artist_key(artist_name(candidate_item)) == seed_key:
                    seed_identity = _artist_identity(candidate_item)
                    if seed_identity:
                        break
        canonical_seed_key = seed_identity or seed_key
        if canonical_seed_key in seen_seed_identities:
            rejected["canonical_artist_alias"] = rejected.get("canonical_artist_alias", 0) + 1
            continue
        card_id = f"radio:artist_inventory:{canonical_seed_key}"
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
                "first_verified_at": now,
                "last_verified_at": now,
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
        seen_seed_identities.add(canonical_seed_key)
        if affinity == "direct":
            direct_cards += 1
        elif affinity == "related":
            backbone_cards += 1
        else:
            similar_cards += 1

    # Familiar artists naturally lead the row through scoring/order, but do
    # not discard qualified related/exploratory cards to satisfy an arbitrary
    # ratio. Discovery supply is the contract that prevents stale reshuffles.
    discovery_cards = sum(
        card.get("seed_affinity") in {"related", "exploratory"}
        for card in cards
    )
    # Add a small, deterministic pair of multi-artist mixes from already
    # qualified cards.  These are synthetic queue identities (never artist
    # identities) and retain the source cards' verified artwork/playability.
    mix_sources = [
        card for card in cards
        if str(card.get("seed_artist_identity") or "").strip()
        and str(card.get("thumbnail") or "").startswith("/artist_artwork/")
        and len(card.get("tracks") or []) >= POPULAR_RADIO_CARD_MIN_TRACKS
    ]
    for offset in (0, 2):
        if len(mix_sources) < offset + 2 or len(cards) >= RADIO_RESERVOIR_MAX_CARDS:
            break
        left, right = mix_sources[offset], mix_sources[offset + 1]
        source_ids = [
            str(left["seed_artist_identity"]), str(right["seed_artist_identity"])
        ]
        mix_id = "mix:" + "+".join(sorted(source_ids)).casefold()
        if any(str(card.get("seed_artist_identity") or "").casefold() == mix_id for card in cards):
            continue
        tracks = list((left.get("tracks") or [])[:6]) + list((right.get("tracks") or [])[:6])
        mix_discovery = left.get("seed_affinity") in {"related", "exploratory"} or right.get("seed_affinity") in {"related", "exploratory"}
        cards.append({
            "id": "radio:multi_artist_mix:" + mix_id,
            "title": f"{left.get('artist_name') or 'Artist'} + {right.get('artist_name') or 'Artist'} Mix",
            "radio_title": f"{left.get('artist_name') or 'Artist'} + {right.get('artist_name') or 'Artist'} Mix",
            "artist_name": " + ".join((left.get("artist_name") or "Artist", right.get("artist_name") or "Artist")),
            "seed_artist_identity": mix_id,
            "seed_artist_identities": source_ids,
            "seed_affinity": "exploratory" if mix_discovery else "direct",
            "radio_mode": "multi_artist_mix",
            "first_verified_at": now,
            "last_verified_at": now,
            "tracks": tracks,
            "items": tracks,
            "track_count": len(tracks),
            "thumbnail": left.get("thumbnail"),
            "collage_images": [left.get("thumbnail"), right.get("thumbnail")],
            "collage_artists": [left.get("artist_name"), right.get("artist_name")],
            "artwork_owner_identity": source_ids[0],
            "artwork_owner_token": left.get("seed_artist_identity_token", ""),
            "seed_artist_identity_token": "",
        })
        discovery_cards += int(mix_discovery)
    discovery_deficit = max(RADIO_VISIBLE_DISCOVERY_TARGET - discovery_cards, 0)
    return ArtistRadioInventory(
        user_scope_id=taste.user_scope_id,
        profile_fingerprint=taste.profile_key,
        generated_at=now,
        expires_at=now + RADIO_INVENTORY_TTL_SECONDS,
        generation_id=str(uuid.uuid4()),
        cards=cards,
        reservoir_cards=list(cards),
        artwork_repair_records=list(artwork_repair_records.values())[:36],
        diagnostics={
            "card_count": len(cards),
            "reservoir_size": len(cards),
            "reservoir_target_size": RADIO_WORKING_RESERVOIR_TARGET_CARDS,
            "reservoir_replenishment_needed": (
                len(cards) < RADIO_WORKING_RESERVOIR_TARGET_CARDS
                or discovery_deficit > 0
            ),
            "direct_card_count": direct_cards,
            "related_card_count": sum(card.get("seed_affinity") == "related" for card in cards),
            "exploratory_card_count": sum(card.get("seed_affinity") == "exploratory" for card in cards),
            "discovery_card_count": discovery_cards,
            "discovery_target_count": RADIO_VISIBLE_DISCOVERY_TARGET,
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
            "artwork_contract_ready": len(cards) >= 10
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
            "own_track_count_by_seed": {
                seed: len({
                    _track_key(item) for item in values
                    if _track_key(item) and _artist_key(artist_name(item)) == seed
                })
                for seed, values in catalog_seed_items.items()
            },
        },
    )


def radio_card_candidates(inventory: ArtistRadioInventory | None) -> List[DiscoveryCandidate]:
    if inventory is None or not inventory.is_ready:
        return []
    # The durable inventory may contain several rotations; the home row keeps
    # the product contract of a bounded 8-12 card slice.
    visible_cards = list(inventory.cards or [])[:15]
    diag = dict(inventory.diagnostics or {})
    return [
        DiscoveryCandidate(
            item={
                **dict(card),
                "radio_supply_generation_id": inventory.generation_id,
                "radio_discovery_cursor_next": int(
                    (diag.get("radio_expansion_progress") or {}).get(
                        "radio_discovery_cursor_next", 0
                    )
                ),
                "fresh_rotation": bool(diag.get("fresh_rotation")),
                "radio_novel_identity_count": int(diag.get("novel_identity_count") or 0),
                "radio_selected_discovery_count": int(
                    diag.get("selected_discovery_card_count")
                    or diag.get("selected_discovery_count")
                    or 0
                ),
                "rotation_pending": bool(
                    diag.get("rotation_pending") or diag.get("radio_rotation_pending")
                ),
            },
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
    comparison_card_ids: set[str] | None = None,
    known_artist_identities: set[str] | None = None,
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
    exposure_ledger = {
        _artist_key(value)
        for value in (inventory.diagnostics or {}).get(
            "popular_radio_exposure_ledger"
        ) or []
        if _artist_key(value)
    }
    known_artist_keys = set(exposure_ledger)
    for value in known_artist_identities or set():
        normalized = _artist_key(value)
        if not normalized:
            continue
        known_artist_keys.add(normalized)
        if normalized.startswith("artist:"):
            known_artist_keys.add(normalized.removeprefix("artist:"))
        elif not normalized.startswith(
            ("provider:artist:", "musicbrainz:artist:", "mix:")
        ):
            known_artist_keys.add(f"artist:{normalized}")

    def is_genuine_discovery(candidate: tuple[Any, ...]) -> bool:
        card = candidate[0]
        mode = str(card.get("radio_mode") or "").strip().casefold()
        if mode in {"multi_artist_mix", "mix", "artist_mix"}:
            return False
        affinity = str(card.get("seed_affinity") or "").strip().casefold()
        return bool(
            affinity in {"related", "exploratory", "similar"}
            and not (_radio_card_artist_aliases(card) & known_artist_keys)
        )

    def first_verified_at(candidate: tuple[Any, ...]) -> float:
        card = candidate[0]
        return float(
            card.get("first_verified_at")
            or card.get("last_verified_at")
            or 0.0
        )
    novel = []
    fallback = []
    for card in reservoir:
        cid = str(card.get("id") or "")
        tracks = _merge_radio_tracks(card.get("tracks") or card.get("items") or [])
        if not tracks:
            continue
        if _artist_key(card.get("artist_name")) in {"unknown", "unknown artist"}:
            continue
        if any(_artist_key(name) in {"unknown", "unknown artist"} for name in card.get("collage_artists") or []):
            continue
        # Do not weaken the card contract while selecting.  Invalid cards are
        # never admitted as a fallback or counted as novelty.
        is_mix = str(card.get("radio_mode") or "").strip().casefold() in {"multi_artist_mix", "mix", "artist_mix"}
        valid_mix = is_mix and _radio_mix_artwork_complete(card)
        if (
            not str(card.get("thumbnail") or "").startswith("/artist_artwork/")
            or (is_mix and not valid_mix)
            or not str(card.get("seed_artist_identity") or "").strip()
            or (not valid_mix and (not str(card.get("artwork_owner_token") or "").strip()
            or str(card.get("artwork_owner_token") or "").strip() != str(card.get("seed_artist_identity_token") or "").strip()
            or str(card.get("seed_artist_identity_token") or "").strip() != artist_artwork_token(card.get("seed_artist_identity"))
            or str(card.get("thumbnail") or "").strip() != f"/artist_artwork/{artist_artwork_token(card.get('seed_artist_identity'))}"
            or str(card.get("artwork_owner_identity") or "").strip() != str(card.get("seed_artist_identity") or "").strip()))
            or not any(
                str(value or "").startswith("/artist_artwork/")
                for value in card.get("collage_images") or []
            )
            or len(tracks) < POPULAR_RADIO_CARD_MIN_TRACKS
        ):
            continue
        track_ids = {_track_key(track) for track in tracks}
        card_identity = _radio_card_identity(card)
        active_overlap = cid in excluded_card_ids
        historical_overlap = card_identity in exposure_ledger
        track_overlap = track_ids & excluded_track_ids
        overlap_score = (1000 * int(active_overlap)) + len(track_overlap)
        candidate = (
            dict(card),
            tracks,
            track_overlap,
            active_overlap,
            historical_overlap,
        )
        if not active_overlap and not track_overlap:
            novel.append(candidate)
        else:
            fallback.append((overlap_score, candidate))

    # Stable deterministic order with epoch rotation.  Novel cards are always
    # consumed before controlled fallbacks; fallback score minimizes repeated
    # card/track identities instead of failing the entire feed.
    epoch_value = max(int(epoch or 0), 0)
    exposure_rank = {
        _artist_key(value): index
        for index, value in enumerate(
            (inventory.diagnostics or {}).get("popular_radio_exposure_ledger") or []
        )
        if _artist_key(value)
    }
    novel.sort(
        key=lambda value: (
            int(value[4]),
            -first_verified_at(value),
            -int(exposure_rank.get(_radio_card_identity(value[0]), -1)),
            str(value[0].get("id") or ""),
        )
    )
    fallback.sort(
        key=lambda value: (
            value[0],
            int(value[1][4]),
            -first_verified_at(value[1]),
            -int(exposure_rank.get(_radio_card_identity(value[1][0]), -1)),
            str(value[1][0].get("id") or ""),
        )
    )
    fallback_candidates = [candidate for _score, candidate in fallback]

    target_limit = min(15, 12 + (max(int(epoch or 0), 0) % 4))
    discovery_novel = [
        candidate for candidate in novel
        if is_genuine_discovery(candidate)
    ]
    familiar_novel = [candidate for candidate in novel if candidate not in discovery_novel]
    discovery_fallback = [
        candidate for candidate in fallback_candidates
        if is_genuine_discovery(candidate)
    ]
    selected_candidates = [
        *familiar_novel[: max(target_limit - RADIO_VISIBLE_DISCOVERY_TARGET, 0)],
        *[ *discovery_novel, *discovery_fallback ][:RADIO_VISIBLE_DISCOVERY_TARGET],
    ]
    selected_candidates.extend(
        candidate for candidate in [*familiar_novel, *discovery_novel, *fallback_candidates]
        if candidate not in selected_candidates
    )
    selected_candidates = selected_candidates[:target_limit]
    mixes = [candidate for candidate in [*novel, *fallback_candidates] if str(candidate[0].get("radio_mode") or "").strip().casefold() == "multi_artist_mix"]
    # Reservoir generations retain old mixes. Selecting all of them can repeat
    # source artists across mixes and fail the publication identity contract.
    # Reserve two disjoint pairs, then fill with the already ranked artist cards.
    mix_pair = next((
        [left, right]
        for index, left in enumerate(mixes)
        for right in mixes[index + 1:]
        if set(left[0].get("seed_artist_identities") or []).isdisjoint(
            right[0].get("seed_artist_identities") or []
        )
    ), [])
    artist_candidates = [
        candidate for candidate in [*selected_candidates, *novel, *fallback_candidates]
        if candidate not in mixes
    ]
    selected_candidates = list(mix_pair)
    for candidate in artist_candidates:
        if candidate not in selected_candidates:
            selected_candidates.append(candidate)
        if len(selected_candidates) >= target_limit:
            break
    selected_candidates.sort(
        key=lambda value: (
            -first_verified_at(value),
            int(value[4]),
            str(value[0].get("id") or ""),
        )
    )
    selected = []
    selected_novel_count = 0
    selected_fallback_count = 0
    selected_track_overlap_count = 0
    selected_card_overlap_count = 0
    selected_historical_exposure_count = 0
    for card, tracks, track_overlap, active_overlap, historical_overlap in selected_candidates:
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
        selected_historical_exposure_count += int(historical_overlap)
        if active_overlap or track_overlap:
            selected_fallback_count += 1
            selected_track_overlap_count += len(track_overlap)
            selected_card_overlap_count += int(active_overlap)
        else:
            selected_novel_count += 1

    overlap_count = len(fallback)
    valid_count = len(novel) + len(fallback_candidates)
    diagnostics = dict(inventory.diagnostics or {})
    valid_reservoir = [candidate[0] for candidate in [*novel, *fallback_candidates]]
    reservoir_discovery_count = sum(
        is_genuine_discovery((card,)) for card in valid_reservoir
    )
    selected_discovery_count = sum(
        is_genuine_discovery((card,)) for card in selected
    )
    selected_affinity_discovery_count = sum(
        str(card.get("seed_affinity") or "").strip().casefold()
        in {"related", "exploratory", "similar"}
        and str(card.get("radio_mode") or "").strip().casefold()
        not in {"multi_artist_mix", "mix", "artist_mix"}
        for card in selected
    )
    discovery_novel_ids = {
        _radio_card_identity(candidate[0]) for candidate in discovery_novel
    }
    selected_discovery_novel_count = sum(
        _radio_card_identity(card) in discovery_novel_ids
        for card in selected
    )
    discovery_deficit = max(
        RADIO_VISIBLE_DISCOVERY_TARGET - reservoir_discovery_count,
        0,
    )
    reservoir_unexposed_count = sum(
        _radio_card_identity(card) not in exposure_ledger
        for card in valid_reservoir
        if isinstance(card, dict) and _radio_card_identity(card)
    )
    unexposed_deficit = max(
        RADIO_UNEXPOSED_TARGET_CARDS - reservoir_unexposed_count,
        0,
    )
    selected_ids = {
        str(card.get("id") or "").strip() for card in selected if str(card.get("id") or "").strip()
    }
    def actual_artist_identities(card: Dict[str, Any]) -> set[str]:
        if str(card.get("radio_mode") or "").casefold() == "multi_artist_mix":
            return {_artist_key(value) for value in card.get("seed_artist_identities") or [] if _artist_key(value)}
        return {_radio_card_identity(card)} - {""}

    selected_identity_ids = {identity for card in selected for identity in actual_artist_identities(card)}
    comparison_ids = {
        str(value).strip() for value in (comparison_card_ids if comparison_card_ids is not None else excluded_card_ids)
        if str(value).strip()
    }
    comparison_identity_ids: set[str] = set()
    for card in reservoir:
        identity = _radio_card_identity(card)
        if (
            str(card.get("id") or "").strip() in comparison_ids
            or identity in comparison_ids
        ):
            if identity:
                comparison_identity_ids.update(actual_artist_identities(card))
    novel_identity_count = len(selected_identity_ids - comparison_identity_ids)
    diagnostics.update({
        "reservoir_size": len(reservoir),
        "reservoir_target_size": RADIO_WORKING_RESERVOIR_TARGET_CARDS,
        "reservoir_replenishment_needed": (
            len(reservoir) < RADIO_WORKING_RESERVOIR_TARGET_CARDS
            or discovery_deficit > 0
            or unexposed_deficit > 0
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
        "historical_exposure_selected_count": selected_historical_exposure_count,
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
        "visible_target_count": 12,
        "visible_selected_limit": target_limit,
        "visible_minimum_count": 10,
        "visible_maximum_count": 15,
        "reservoir_discovery_card_count": reservoir_discovery_count,
        "reservoir_unexposed_card_count": reservoir_unexposed_count,
        "unexposed_target_count": RADIO_UNEXPOSED_TARGET_CARDS,
        "unexposed_deficit": unexposed_deficit,
        "exposure_target_count": RADIO_UNEXPOSED_TARGET_CARDS,
        "exposure_deficit": unexposed_deficit,
        "selected_discovery_card_count": selected_discovery_count,
        "selected_affinity_discovery_card_count": selected_affinity_discovery_count,
        "known_artist_identity_count": len(known_artist_keys),
        "selected_discovery_novel_count": selected_discovery_novel_count,
        "visible_discovery_target_count": RADIO_VISIBLE_DISCOVERY_TARGET,
        "visible_discovery_target_deficit": max(
            RADIO_VISIBLE_DISCOVERY_TARGET - selected_discovery_count, 0
        ),
        "visible_discovery_fresh_deficit": max(
            RADIO_VISIBLE_DISCOVERY_TARGET - selected_discovery_novel_count, 0
        ),
        "novel_identity_count": novel_identity_count,
        "fresh_rotation": novel_identity_count >= 3,
        "exact_identity_set_reshuffle": bool(
            selected_ids and comparison_ids and selected_ids == comparison_ids
        ),
        "discovery_target_count": RADIO_VISIBLE_DISCOVERY_TARGET,
        "discovery_deficit": discovery_deficit,
        "reservoir_shortage": len(selected) < 10,
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
    if existing is None:
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
            by_id[card_id] = dict(card)
            continue
        retained = by_id[card_id]
        retained_first = float(
            retained.get("first_verified_at")
            or incoming.generated_at
            or time.time()
        )
        candidate_first = float(
            card.get("first_verified_at")
            or existing.generated_at
            or retained_first
        )
        retained["first_verified_at"] = min(retained_first, candidate_first)
        retained["last_verified_at"] = max(
            float(retained.get("last_verified_at") or retained_first),
            float(card.get("last_verified_at") or candidate_first),
        )
        tracks = _merge_radio_tracks([
            *(card.get("tracks") or card.get("items") or []),
            *(retained.get("tracks") or retained.get("items") or []),
        ])[:RADIO_CATALOG_INDEX_MAX_TRACKS]
        retained["tracks"] = tracks
        retained["items"] = tracks
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
        expires_at=max(time.time() + RADIO_INVENTORY_TTL_SECONDS, incoming.expires_at),
        generation_id=incoming.generation_id,
        cards=list(incoming.cards or []),
        reservoir_cards=reservoir,
        artwork_repair_records=list(incoming.artwork_repair_records or []),
        diagnostics=diagnostics,
    )
