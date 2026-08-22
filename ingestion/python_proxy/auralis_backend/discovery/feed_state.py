from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, Dict
import json
import threading
import time

from ..storage.session_store import get_session_store
from .artifact import (
    _artifact_from_dict,
    artifact_to_dict,
)
from .schema import DiscoveryArtifact
from .config import (
    FEED_PROMOTION_CONTRACT_VERSION,
    POPULAR_RADIO_CARD_MIN_TRACKS,
    ROW_RECIPES,
)

FEED_STATE_NAMESPACE = "discovery_feed_state_v2"
FEED_STATE_MODEL_VERSION = "feed-state-v2"
FEED_STATE_TTL_SECONDS = 60 * 60 * 24 * 14
FEED_ARTIFACT_NAMESPACE = "discovery_feed_artifact"
FEED_ARTIFACT_MODEL = "discovery-feed-artifact"

# Stable machine-readable outcomes for queue persistence.  Callers can make
# bounded recovery decisions without inferring intent from ``None``.
STORED = "stored"
QUEUE_FULL = "queue_full"
DUPLICATE = "duplicate"
CONTRACT_SHORTAGE = "contract_shortage"
QUALITY_REJECTION = "quality_rejection"
VERSION_RACE = "version_race"
INVENTORY_RACE = "inventory_race"
PERSISTENCE_FAILURE = "persistence_failure"


@dataclass
class PreparedFeedStoreResult:
    outcome: str
    state: "FeedState | None" = None
    reason: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.outcome == STORED

    def __getattr__(self, name: str) -> Any:
        # Compatibility for existing callers that read ``ready_feeds`` from
        # the old FeedState return value.
        state = object.__getattribute__(self, "state")
        if state is not None:
            return getattr(state, name)
        raise AttributeError(name)


def artifact_promotion_contract(artifact: DiscoveryArtifact | None) -> tuple[bool, str]:
    """Validate the current persisted successor contract.

    This is deliberately stricter than ``accepted``: accepted artifacts from
    older workers may deserialize, but must not be promoted after the radio
    contract changed. The active artifact is never rejected by this helper;
    callers apply it only to successors.
    """
    if artifact is None or not artifact.accepted:
        return False, "artifact_not_accepted"
    if float(artifact.expires_at or 0.0) <= time.time():
        return False, "artifact_expired"
    diagnostics = dict(artifact.diagnostics or {})
    if str(diagnostics.get("feed_promotion_contract") or "") != FEED_PROMOTION_CONTRACT_VERSION:
        return False, "promotion_contract_outdated"
    radio = next((row for row in artifact.rows or [] if row.kind == "popular_radio"), None)
    items = list(radio.items or []) if radio is not None else []
    minimum = max(int(ROW_RECIPES["popular_radio"].min_items), 8)
    if len(items) < minimum:
        return False, "popular_radio_card_shortage"
    for card in items:
        if not isinstance(card, dict):
            return False, "popular_radio_card_malformed"
        if not str(card.get("thumbnail") or "").strip().startswith("/artist_artwork/"):
            return False, "popular_radio_artwork_unverified"
        if not any(
            str(value or "").strip().startswith("/artist_artwork/")
            for value in card.get("collage_images") or []
        ):
            return False, "popular_radio_collage_unverified"
        if len(card.get("tracks") or card.get("items") or []) < POPULAR_RADIO_CARD_MIN_TRACKS:
            return False, "popular_radio_track_depth"
    return True, "ready"


@dataclass
class ReadyFeedEntry:
    artifact: DiscoveryArtifact
    parent_session_id: str = ""
    inventory_generation: str = ""
    rotation_epoch: int = 0
    intent_version: int = 0
    profile_fingerprint: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def session_id(self) -> str:
        return str(self.artifact.session_id or "")


@dataclass
class FeedState:
    user_scope_id: str
    active_feed: DiscoveryArtifact | None = None
    prepared_feed: DiscoveryArtifact | None = None
    active_version: int = 0
    prepared_base_version: int = 0
    profile_fingerprint: str = ""
    dirty_reasons: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    generation_status: str = "idle"
    preparation_lease_started_at: float = 0.0
    preparation_lease_deadline: float = 0.0
    preparation_lease_reason: str = ""
    # Durable fencing token for the preparation lease. A worker that outlives
    # its lease must not commit candidate/feed output after a successor has
    # taken over the queue.
    preparation_lease_token: str = ""
    retry_at: float = 0.0
    # Exact internal reason used to resume a delayed worker after restart.
    # This avoids guessing from customer-facing dirty-reason strings.
    retry_reason: str = ""
    rotation_epoch: int = 0
    prepared_rotation_epoch: int = 0
    active_inventory_generation: str = ""
    prepared_inventory_generation: str = ""
    prepared_intent_version: int = 0
    # Ordered persisted successors. ``prepared_feed`` is retained as a
    # compatibility alias for the queue head during migration.
    ready_feeds: list[ReadyFeedEntry] = field(default_factory=list)
    recovery_attempt_keys: list[str] = field(default_factory=list)
    # Monotonic durable topology generation. Auxiliary status/lease writes
    # preserve it; append/promote/active replacement advance it atomically.
    queue_revision: int = 0

    def __post_init__(self) -> None:
        if not self.ready_feeds and self.prepared_feed is not None:
            if (
                self.prepared_base_version
                and self.prepared_base_version != self.active_version
            ):
                # One-time migration safety for the former single-slot model.
                # New queue entries carry their own parent metadata and do not
                # use this legacy base-version rule.
                self.prepared_feed = None
                self.prepared_base_version = 0
                self.generation_status = "stale_prepared_discarded"
            else:
                self.ready_feeds = [
                    ReadyFeedEntry(
                        artifact=self.prepared_feed,
                        inventory_generation=self.prepared_inventory_generation,
                        rotation_epoch=self.prepared_rotation_epoch,
                        intent_version=self.prepared_intent_version,
                        profile_fingerprint=str(self.prepared_feed.profile_key or ""),
                    )
                ]
        elif self.ready_feeds:
            self.ready_feeds = [
                (
                    entry
                    if isinstance(entry, ReadyFeedEntry)
                    else ReadyFeedEntry(artifact=entry)
                )
                for entry in list(self.ready_feeds)[:2]
            ]
            self.prepared_feed = self.ready_feeds[0].artifact


_STATE_LOCKS: Dict[str, threading.RLock] = {}
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_CACHE: Dict[str, FeedState] = {}
_QUEUE_CONDITIONS: Dict[str, threading.Condition] = {}


def feed_queue_condition(user_scope_id: str) -> threading.Condition:
    scope = _scope(user_scope_id)
    with _STATE_LOCKS_GUARD:
        return _QUEUE_CONDITIONS.setdefault(scope, threading.Condition())


def notify_feed_queue(user_scope_id: str) -> None:
    condition = feed_queue_condition(user_scope_id)
    with condition:
        condition.notify_all()


def feed_queue_summary(server: Any, user_scope_id: str) -> Dict[str, Any]:
    """Read queue metadata without hydrating active/ready artifact bodies."""
    payload = _persistent_get(server, _state_key(user_scope_id)) or {}
    if not payload:
        with _STATE_LOCKS_GUARD:
            cached = _STATE_CACHE.get(_scope(user_scope_id))
        if cached is not None:
            payload = _state_to_payload(cached)
    refs = payload.get("ready_feed_session_ids")
    if not isinstance(refs, list):
        refs = [payload.get("prepared_feed_session_id")] if payload.get("prepared_feed_session_id") else []
    deadline = float(payload.get("preparation_lease_deadline") or 0.0)
    lease_active = bool(payload.get("preparation_lease_token") and deadline > time.time())
    phase = str(payload.get("generation_status") or "idle")
    inflight = lease_active or phase in {"preparing", "inventory_building"}
    return {
        "user_scope_id": _scope(user_scope_id),
        "preparation_state": str(payload.get("generation_status") or "idle"),
        "queue_phase": "building" if inflight else phase,
        "queue_build_inflight": inflight,
        "preparation_lease_deadline": deadline,
        "ready_feed_count": min(len([value for value in refs if str(value or "")]), 2),
        "ready_feed_depth": min(len([value for value in refs if str(value or "")]), 2),
        "ready_feed_target_depth": 2,
        "ready_feed_session_ids": [str(value or "") for value in refs[:2] if str(value or "")],
        "queue_revision": max(int(payload.get("queue_revision") or 0), 0),
        "retry_at": float(payload.get("retry_at") or 0.0),
        "retry_reason": str(payload.get("retry_reason") or ""),
        "dirty_reasons": list(payload.get("dirty_reasons") or []),
    }


def _completed_optional_rows(artifact: DiscoveryArtifact | None) -> set[str]:
    if artifact is None:
        return set()
    from .config import ROW_RECIPES

    optional = {"featured_new_albums", "popular_radio", "recommended_albums"}
    counts = {
        row.kind: len(row.items or [])
        for row in artifact.rows or []
        if row.kind in optional
    }
    completed = {
        kind
        for kind in optional
        if int(counts.get(kind) or 0) >= ROW_RECIPES[kind].min_items
    }
    radio_row = next(
        (row for row in artifact.rows or [] if row.kind == "popular_radio"),
        None,
    )
    radio_items = list(radio_row.items or []) if radio_row is not None else []
    valid_radio_items = [item for item in radio_items if isinstance(item, dict)]
    if radio_row is not None and (
        len(valid_radio_items) != len(radio_items)
        or not all(
            str(item.get("thumbnail") or "").strip().startswith(
                "/artist_artwork/"
            )
            and any(
                str(value or "").strip().startswith("/artist_artwork/")
                for value in item.get("collage_images") or []
            )
            for item in valid_radio_items
        )
    ):
        completed.discard("popular_radio")
    return completed


def _optional_row_payload(
    artifact: DiscoveryArtifact | None,
    kind: str,
) -> list[Dict[str, Any]]:
    if artifact is None:
        return []
    row = next(
        (candidate for candidate in artifact.rows or [] if candidate.kind == kind),
        None,
    )
    return [dict(item) for item in (row.items if row is not None else []) or []]


def _optional_row_quality(
    artifact: DiscoveryArtifact | None,
    kind: str,
) -> tuple[int, int, int]:
    items = _optional_row_payload(artifact, kind)
    artwork_count = sum(
        1
        for item in items
        if (
            str(item.get("thumbnail") or "").strip().startswith(
                "/artist_artwork/"
            )
            if kind == "popular_radio"
            else str(item.get("thumbnail") or "").strip()
            or any(
                str(value or "").strip()
                for value in item.get("collage_images") or []
            )
        )
    )
    nested_track_count = sum(
        len(
            [
                value
                for value in (item.get("tracks") or item.get("items") or [])
                if isinstance(value, dict)
            ]
        )
        for item in items
    )
    # Once a row meets its product minimum, extra nested tracks are reserve
    # detail and must not make a successor appear weaker (e.g. 99 -> 97).
    from .config import ROW_RECIPES
    recipe = ROW_RECIPES.get(kind)
    count = min(len(items), int(recipe.target_items if recipe else len(items)))
    nested_target = int(recipe.min_items if recipe else nested_track_count)
    nested = min(nested_track_count, nested_target)
    artwork_target = int(recipe.target_items if recipe else artwork_count)
    return count, min(artwork_count, artwork_target), nested


def _row_content_signatures(
    artifact: DiscoveryArtifact | None,
) -> Dict[str, str]:
    if artifact is None:
        return {}
    output: Dict[str, str] = {}
    for row in artifact.rows or []:
        items = []
        for item in row.items or []:
            if not isinstance(item, dict):
                continue
            identity = str(
                item.get("canonical_entity_id")
                or item.get("canonical_track_identity")
                or item.get("canonical_source_identity")
                or item.get("canonical_key")
                or item.get("recording_id")
                or item.get("id")
                or item.get("key")
                or item.get("videoId")
                or item.get("video_id")
                or ""
            )
            nested = []
            for value in item.get("tracks") or item.get("items") or []:
                if not isinstance(value, dict):
                    continue
                nested.append({
                    "identity": str(
                        value.get("canonical_entity_id")
                        or value.get("canonical_track_identity")
                        or value.get("canonical_source_identity")
                        or value.get("canonical_key")
                        or value.get("recording_id")
                        or value.get("id")
                        or value.get("videoId")
                        or value.get("video_id")
                        or ""
                    ),
                    "artwork": value.get("thumbnail") or value.get("artwork_url") or "",
                    "playable": bool(
                        value.get("playable")
                        or value.get("source_id")
                        or value.get("videoId")
                        or value.get("video_id")
                    ),
                })
            items.append({
                "identity": identity,
                "artwork": item.get("thumbnail") or item.get("artwork_url") or "",
                "playable": bool(
                    item.get("playable")
                    or item.get("source_id")
                    or item.get("videoId")
                    or item.get("video_id")
                ),
                "detail": bool(item.get("detail_url") or item.get("album_id") or item.get("artist_id") or item.get("canonical_key")),
                "nested": nested,
            })
        kind = str(row.kind or "")
        if kind:
            output[kind] = json.dumps(items, sort_keys=True, ensure_ascii=True)
    return output


def _feed_content_signature(artifact: DiscoveryArtifact) -> str:
    return json.dumps(
        {
            "rows": _row_content_signatures(artifact),
            "home_tab_lanes": artifact.home_tab_lanes or {},
        },
        sort_keys=True,
        ensure_ascii=True,
    )


def _scope(value: Any) -> str:
    return str(value or "guest").strip() or "guest"


def _state_key(user_scope_id: str) -> str:
    return f"feed-state:{_scope(user_scope_id)}"


def feed_state_lock(user_scope_id: str) -> threading.RLock:
    scope = _scope(user_scope_id)
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(scope, threading.RLock())


def _state_to_payload(state: FeedState) -> Dict[str, Any]:
    queue = list(state.ready_feeds or [])[:2]
    if not queue and state.prepared_feed is not None:
        queue = [ReadyFeedEntry(artifact=state.prepared_feed)]
    return {
        "state_version": FEED_STATE_MODEL_VERSION,
        "user_scope_id": _scope(state.user_scope_id),
        "active_feed_session_id": str(
            state.active_feed.session_id if state.active_feed is not None else ""
        ),
        "prepared_feed_session_id": str(queue[0].session_id if queue else ""),
        "ready_feed_session_ids": [str(item.session_id or "") for item in queue],
        "ready_feeds": [
            {
                "session_id": entry.session_id,
                "parent_session_id": entry.parent_session_id,
                "inventory_generation": entry.inventory_generation,
                "rotation_epoch": entry.rotation_epoch,
                "intent_version": entry.intent_version,
                "profile_fingerprint": entry.profile_fingerprint,
                "created_at": entry.created_at,
            }
            for entry in queue
        ],
        "active_version": int(state.active_version or 0),
        "prepared_base_version": int(state.prepared_base_version or 0),
        "profile_fingerprint": str(state.profile_fingerprint or ""),
        "dirty_reasons": list(
            dict.fromkeys(str(reason) for reason in state.dirty_reasons if str(reason))
        ),
        "updated_at": float(state.updated_at or time.time()),
        "generation_status": str(state.generation_status or "idle"),
        "preparation_lease_started_at": float(state.preparation_lease_started_at or 0.0),
        "preparation_lease_deadline": float(state.preparation_lease_deadline or 0.0),
        "preparation_lease_reason": str(state.preparation_lease_reason or ""),
        "preparation_lease_token": str(state.preparation_lease_token or ""),
        "retry_at": float(state.retry_at or 0.0),
        "retry_reason": str(state.retry_reason or ""),
        "rotation_epoch": max(int(state.rotation_epoch or 0), 0),
        "prepared_rotation_epoch": max(int(state.prepared_rotation_epoch or 0), 0),
        "active_inventory_generation": str(state.active_inventory_generation or ""),
        "prepared_inventory_generation": str(state.prepared_inventory_generation or ""),
        "prepared_intent_version": max(int(state.prepared_intent_version or 0), 0),
        "ready_feed_count": len(queue),
        "recovery_attempt_keys": list(dict.fromkeys(state.recovery_attempt_keys or []))[-16:],
        "queue_revision": max(int(state.queue_revision or 0), 0),
    }


def _artifact_entity_id(user_scope_id: str, session_id: str) -> str:
    return f"{_scope(user_scope_id)}:{str(session_id or '').strip()}"


def _persistent_get_artifact(
    server: Any,
    user_scope_id: str,
    session_id: str,
) -> DiscoveryArtifact | None:
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        return None
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
            [
                FEED_ARTIFACT_NAMESPACE,
                _artifact_entity_id(user_scope_id, normalized_session),
            ],
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["payload_json"] or "{}")
        return _artifact_from_dict(decoded if isinstance(decoded, dict) else None)
    except Exception:
        return None
    finally:
        connection.close()


def _state_from_payload(
    payload: Dict[str, Any] | None,
    *,
    server: Any = None,
) -> FeedState | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("state_version") or "") != FEED_STATE_MODEL_VERSION:
        return None
    user_scope_id = _scope(payload.get("user_scope_id"))
    active = _artifact_from_dict(payload.get("active_feed"))
    prepared = _artifact_from_dict(payload.get("prepared_feed"))
    active_ref = str(payload.get("active_feed_session_id") or "").strip()
    prepared_ref = str(payload.get("prepared_feed_session_id") or "").strip()
    if active is None and active_ref:
        active = _persistent_get_artifact(server, user_scope_id, active_ref)
    if prepared is None and prepared_ref:
        prepared = _persistent_get_artifact(server, user_scope_id, prepared_ref)
    queue: list[ReadyFeedEntry] = []
    queue_refs = payload.get("ready_feed_session_ids") or []
    metadata = (
        payload.get("ready_feeds")
        if isinstance(payload.get("ready_feeds"), list)
        else []
    )
    if isinstance(queue_refs, list):
        for idx, ref in enumerate(queue_refs[:2]):
            artifact = _persistent_get_artifact(server, user_scope_id, str(ref))
            if artifact is not None:
                meta = (
                    metadata[idx]
                    if idx < len(metadata) and isinstance(metadata[idx], dict)
                    else {}
                )
                queue.append(
                    ReadyFeedEntry(
                        artifact=artifact,
                        parent_session_id=str(meta.get("parent_session_id") or ""),
                        inventory_generation=str(
                            meta.get("inventory_generation") or ""
                        ),
                        rotation_epoch=int(meta.get("rotation_epoch") or 0),
                        intent_version=int(meta.get("intent_version") or 0),
                        profile_fingerprint=str(
                            meta.get("profile_fingerprint")
                            or artifact.profile_key
                            or ""
                        ),
                        created_at=float(meta.get("created_at") or time.time()),
                    )
                )
    legacy_prepared_stale = False
    if not queue and prepared is not None:
        legacy_base_version = max(
            int(payload.get("prepared_base_version") or 0),
            0,
        )
        active_version = max(int(payload.get("active_version") or 0), 0)
        legacy_prepared_stale = bool(
            legacy_base_version and legacy_base_version != active_version
        )
        if legacy_prepared_stale:
            prepared = None
        else:
            queue = [
                ReadyFeedEntry(
                    artifact=prepared,
                    inventory_generation=str(
                        payload.get("prepared_inventory_generation") or ""
                    ),
                    rotation_epoch=max(
                        int(payload.get("prepared_rotation_epoch") or 0),
                        0,
                    ),
                    intent_version=max(
                        int(payload.get("prepared_intent_version") or 0),
                        0,
                    ),
                    profile_fingerprint=str(prepared.profile_key or ""),
                )
            ]
    elif queue:
        prepared = queue[0].artifact
    artifact_cutover = isinstance(payload.get("active_feed"), dict) and active is None
    return FeedState(
        user_scope_id=user_scope_id,
        active_feed=active,
        prepared_feed=prepared,
        active_version=max(int(payload.get("active_version") or 0), 0),
        prepared_base_version=(
            0
            if legacy_prepared_stale
            else max(int(payload.get("prepared_base_version") or 0), 0)
        ),
        profile_fingerprint=(
            "" if artifact_cutover else str(payload.get("profile_fingerprint") or "")
        ),
        dirty_reasons=(
            ["artifact_version_cutover"]
            if artifact_cutover
            else [
                str(reason)
                for reason in payload.get("dirty_reasons") or []
                if str(reason)
            ]
        ),
        updated_at=float(payload.get("updated_at") or 0.0),
        generation_status=(
            "idle"
            if artifact_cutover
            else (
                "stale_prepared_discarded"
                if legacy_prepared_stale
                else str(payload.get("generation_status") or "idle")
            )
        ),
        preparation_lease_started_at=float(payload.get("preparation_lease_started_at") or 0.0),
        preparation_lease_deadline=float(payload.get("preparation_lease_deadline") or 0.0),
        preparation_lease_reason=str(payload.get("preparation_lease_reason") or ""),
        preparation_lease_token=str(payload.get("preparation_lease_token") or ""),
        retry_at=float(payload.get("retry_at") or 0.0),
        retry_reason=str(payload.get("retry_reason") or ""),
        rotation_epoch=max(int(payload.get("rotation_epoch") or 0), 0),
        prepared_rotation_epoch=(
            0
            if legacy_prepared_stale
            else max(int(payload.get("prepared_rotation_epoch") or 0), 0)
        ),
        active_inventory_generation=str(
            payload.get("active_inventory_generation") or ""
        ),
        prepared_inventory_generation=(
            ""
            if legacy_prepared_stale
            else str(payload.get("prepared_inventory_generation") or "")
        ),
        prepared_intent_version=(
            0
            if legacy_prepared_stale
            else max(int(payload.get("prepared_intent_version") or 0), 0)
        ),
        ready_feeds=queue,
        recovery_attempt_keys=[str(value) for value in (payload.get("recovery_attempt_keys") or []) if str(value)],
        queue_revision=max(int(payload.get("queue_revision") or 0), 0),
    )


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
            [FEED_STATE_NAMESPACE, key],
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["payload_json"] or "{}")
        return dict(decoded) if isinstance(decoded, dict) else None
    except Exception:
        return None
    finally:
        connection.close()


def _persistent_set(
    server: Any,
    key: str,
    payload: Dict[str, Any],
    *,
    artifacts: Dict[str, DiscoveryArtifact] | None = None,
    expected_active_version: int | None = None,
    expected_ready_session_ids: list[str] | None = None,
    expected_preparation_lease_token: str | None = None,
) -> bool | None:
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload_json FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [FEED_STATE_NAMESPACE, key],
        ).fetchone()
        current_payload: Dict[str, Any] = {}
        if row is not None:
            decoded = json.loads(row["payload_json"] or "{}")
            if isinstance(decoded, dict):
                current_payload = dict(decoded)
        if (
            expected_preparation_lease_token is not None
            and str(current_payload.get("preparation_lease_token") or "")
            != str(expected_preparation_lease_token or "")
        ):
            print(
                "[EBB:feed-state][cas-miss] "
                f"scope={payload.get('user_scope_id') or ''} "
                "field=preparation_lease_token",
                flush=True,
            )
            connection.rollback()
            return False
        # Status/lease/retry writers often carry a stale FeedState snapshot.
        # They are non-queue mutations and must never erase a newer durable
        # successor queue written by another process.  Merge queue-bearing
        # fields from the transaction's current row unless this call supplied
        # an explicit queue CAS (append/promote/invalidate).
        if expected_ready_session_ids is None and current_payload:
            for field in (
                "active_feed_session_id", "prepared_feed_session_id",
                "ready_feed_session_ids", "ready_feeds", "ready_feed_count",
                "active_version", "rotation_epoch", "active_inventory_generation",
                "profile_fingerprint", "queue_revision",
                "prepared_base_version", "prepared_rotation_epoch",
                "prepared_inventory_generation", "prepared_intent_version",
            ):
                if field in current_payload:
                    payload[field] = current_payload[field]
        if expected_active_version is not None and max(
            int(current_payload.get("active_version") or 0), 0
        ) != max(int(expected_active_version or 0), 0):
            print(
                "[EBB:feed-state][cas-miss] "
                f"scope={payload.get('user_scope_id') or ''} field=active_version "
                f"expected={expected_active_version} "
                f"actual={current_payload.get('active_version') or 0}",
                flush=True,
            )
            connection.rollback()
            return False
        current_ready_session_ids = current_payload.get("ready_feed_session_ids")
        if not isinstance(current_ready_session_ids, list):
            legacy_prepared_session = str(
                current_payload.get("prepared_feed_session_id") or ""
            )
            current_ready_session_ids = (
                [legacy_prepared_session] if legacy_prepared_session else []
            )
        current_ready_session_ids = [
            str(value or "") for value in current_ready_session_ids
        ]
        if expected_ready_session_ids is not None and current_ready_session_ids != [
            str(value or "") for value in expected_ready_session_ids
        ]:
            print(
                "[EBB:feed-state][cas-miss] "
                f"scope={payload.get('user_scope_id') or ''} field=ready_queue "
                f"expected={expected_ready_session_ids or []} "
                f"actual={current_ready_session_ids}",
                flush=True,
            )
            connection.rollback()
            return False
        next_ready_session_ids = [
            str(value or "")
            for value in (payload.get("ready_feed_session_ids") or [])
        ]
        current_active_ref = str(
            current_payload.get("active_feed_session_id") or ""
        )
        next_active_ref = str(payload.get("active_feed_session_id") or "")
        topology_changed = bool(
            current_payload
            and (
                current_ready_session_ids != next_ready_session_ids
                or current_active_ref != next_active_ref
                or int(current_payload.get("active_version") or 0)
                != int(payload.get("active_version") or 0)
            )
        )
        current_queue_revision = max(
            int(current_payload.get("queue_revision") or 0), 0
        )
        payload["queue_revision"] = (
            current_queue_revision + 1
            if topology_changed
            else current_queue_revision
        )
        artifact_values = {
            _artifact_entity_id(
                payload.get("user_scope_id") or "guest", session_id
            ): artifact
            for session_id, artifact in dict(artifacts or {}).items()
            if str(session_id or "").strip() and artifact is not None
        }
        # Session artifacts are immutable once published. For topology CAS
        # writes, only newly referenced sessions can need a body write;
        # promotion therefore updates refs/state without serializing bodies.
        if expected_ready_session_ids is not None and current_payload:
            current_refs = {
                str(current_payload.get("active_feed_session_id") or "").strip(),
                *[str(value or "").strip() for value in current_ready_session_ids],
            }
            artifact_values = {
                entity_id: artifact
                for entity_id, artifact in artifact_values.items()
                if entity_id.rsplit(":", 1)[-1] not in current_refs
            }
        # Auxiliary state writers patch status/lease/retry fields only. They
        # must not replace newer artifact bodies written by another process.
        if expected_ready_session_ids is None and current_payload:
            artifact_values = {}
        existing_artifacts: set[str] = set()
        artifact_keys = list(artifact_values)
        if artifact_keys:
            placeholders = ",".join("?" for _ in artifact_keys)
            existing_artifacts = {
                str(row["entity_id"])
                for row in connection.execute(
                    f"""
                    SELECT entity_id
                    FROM recommendation_feature_store
                    WHERE namespace = ? AND entity_id IN ({placeholders})
                    """,
                    [FEED_ARTIFACT_NAMESPACE, *artifact_keys],
                ).fetchall()
            }
        artifact_rows = []
        for entity_id, artifact in artifact_values.items():
            if entity_id in existing_artifacts:
                continue
            encoded = json.dumps(artifact_to_dict(artifact), ensure_ascii=False)
            artifact_rows.append([
                FEED_ARTIFACT_NAMESPACE, entity_id, FEED_ARTIFACT_MODEL, encoded, time.time()
            ])
        if artifact_rows:
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
                artifact_rows,
            )
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
                FEED_STATE_NAMESPACE,
                key,
                FEED_STATE_MODEL_VERSION,
                json.dumps(payload, ensure_ascii=False),
                time.time(),
            ],
        )
        keep_artifact_keys = list(artifact_values)
        if expected_ready_session_ids is not None:
            scope_value = payload.get("user_scope_id") or "guest"
            keep_artifact_keys.extend(
                _artifact_entity_id(scope_value, value)
                for value in [
                    payload.get("active_feed_session_id"),
                    *(payload.get("ready_feed_session_ids") or []),
                ]
                if str(value or "").strip()
            )
        # Preserve artifacts referenced by the durable queue when merging a
        # stale auxiliary write; otherwise cleanup below could delete them.
        if expected_ready_session_ids is None:
            for session_id in current_ready_session_ids:
                normalized = str(session_id or "").strip()
                if normalized:
                    keep_artifact_keys.append(
                        _artifact_entity_id(payload.get("user_scope_id") or "guest", normalized)
                    )
            active_ref = str(current_payload.get("active_feed_session_id") or "").strip()
            if active_ref:
                keep_artifact_keys.append(
                    _artifact_entity_id(payload.get("user_scope_id") or "guest", active_ref)
                )
        keep_artifact_keys = list(dict.fromkeys(keep_artifact_keys))
        scope_prefix = _scope(payload.get("user_scope_id")) + ":%"
        if keep_artifact_keys:
            placeholders = ",".join("?" for _ in keep_artifact_keys)
            connection.execute(
                f"""
                DELETE FROM recommendation_feature_store
                WHERE namespace = ? AND entity_id LIKE ?
                  AND entity_id NOT IN ({placeholders})
                """,
                [FEED_ARTIFACT_NAMESPACE, scope_prefix, *keep_artifact_keys],
            )
        connection.commit()
        if topology_changed:
            if len(next_ready_session_ids) > len(current_ready_session_ids):
                mutation = "append"
            elif len(next_ready_session_ids) < len(current_ready_session_ids):
                mutation = (
                    "promote"
                    if current_active_ref != next_active_ref
                    else "queue_trim"
                )
            elif current_active_ref != next_active_ref:
                mutation = "active_replace"
            else:
                mutation = "queue_rewrite"
            print(
                "[EBB:feed-queue] mutate "
                f"scope={payload.get('user_scope_id') or ''} "
                f"action={mutation} "
                f"revision={payload['queue_revision']} "
                f"before={current_ready_session_ids} "
                f"after={next_ready_session_ids} "
                f"active_before={current_active_ref} "
                f"active_after={next_active_ref}",
                flush=True,
            )
        return True
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        print(
            "[EBB:feed-state][persist-error] "
            f"scope={payload.get('user_scope_id') or ''} "
            f"error={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
        return False
    finally:
        connection.close()


def save_feed_state(
    server: Any,
    state: FeedState,
    *,
    expected_active_version: int | None = None,
    expected_ready_session_ids: list[str] | None = None,
    expected_preparation_lease_token: str | None = None,
) -> bool:
    state.user_scope_id = _scope(state.user_scope_id)
    state.updated_at = time.time()
    payload = _state_to_payload(state)
    key = _state_key(state.user_scope_id)
    persistent_result = _persistent_set(
        server,
        key,
        payload,
        artifacts={
            str(artifact.session_id): (
                artifact.artifact if isinstance(artifact, ReadyFeedEntry) else artifact
            )
            for artifact in (
                [state.active_feed]
                + list(state.ready_feeds or [])
                + ([state.prepared_feed] if state.prepared_feed is not None else [])
            )
            if artifact is not None and str(artifact.session_id or "").strip()
        },
        expected_active_version=expected_active_version,
        expected_ready_session_ids=expected_ready_session_ids,
        expected_preparation_lease_token=expected_preparation_lease_token,
    )
    if persistent_result is False and (
        expected_active_version is not None
        or expected_ready_session_ids is not None
        or expected_preparation_lease_token is not None
    ):
        return False
    cache_saved = persistent_result is True or persistent_result is None
    if cache_saved:
        # A successful auxiliary merge may have incorporated a newer queue
        # (and active topology) than the caller's stale object. Cache the
        # durable merged snapshot, never the stale writer snapshot.
        # Explicit queue/active CAS callers already hold the committed state;
        # avoid rereading and deserializing every artifact after a successful
        # atomic write. Auxiliary writers still reload to merge durable state.
        # Queue topology CAS is authoritative only when the caller fenced the
        # expected ready-session list. Lease-token/status writers may carry a
        # stale state snapshot and must reload the durable merge.
        explicit_cas = expected_ready_session_ids is not None
        if persistent_result is True and not explicit_cas:
            merged_payload = _persistent_get(server, key)
            merged_state = _state_from_payload(merged_payload, server=server)
            if merged_state is not None:
                _sync_state(state, merged_state)
        elif persistent_result is True and explicit_cas:
            # Refresh only lightweight CAS metadata; artifact bodies remain
            # authoritative in the caller and are not deserialized again.
            committed_payload = _persistent_get(server, key)
            if isinstance(committed_payload, dict):
                state.queue_revision = max(
                    int(committed_payload.get("queue_revision") or state.queue_revision or 0), 0
                )
                state.updated_at = float(committed_payload.get("updated_at") or state.updated_at or time.time())
        with _STATE_LOCKS_GUARD:
            _STATE_CACHE[state.user_scope_id] = deepcopy(state)
    session_saved = False
    try:
        session_store = get_session_store()
        if session_store.__class__.__name__ != "MemorySessionStore":
            session_store.set(key, payload, FEED_STATE_TTL_SECONDS)
            session_saved = True
    except Exception:
        pass
    return persistent_result is True or cache_saved or session_saved


def _load_state(server: Any, user_scope_id: str) -> FeedState | None:
    scope = _scope(user_scope_id)
    key = _state_key(user_scope_id)
    # SQLite is the cross-process authority.  The proxy and recommendation
    # worker each have their own memory cache; preferring either cache here
    # allowed an older one-entry queue to overwrite a durable two-entry queue
    # after Flutter had already observed 2/2.
    persistent_payload = _persistent_get(server, key)
    with _STATE_LOCKS_GUARD:
        cached = _STATE_CACHE.get(scope)
    if (
        cached is not None
        and isinstance(persistent_payload, dict)
        and float(persistent_payload.get("updated_at") or 0.0)
        == float(cached.updated_at or 0.0)
    ):
        # The lightweight state row is unchanged, so avoid deserializing the
        # active artifact and both queued artifacts on every status/worker
        # read. Cross-process updates still invalidate this cache through the
        # durable updated_at generation above.
        return deepcopy(cached)
    persistent_state = _state_from_payload(persistent_payload, server=server)
    if persistent_state is not None:
        with _STATE_LOCKS_GUARD:
            _STATE_CACHE[scope] = deepcopy(persistent_state)
        return deepcopy(persistent_state)

    payload = None
    try:
        payload = get_session_store().get(key)
    except Exception:
        payload = None
    state = _state_from_payload(
        payload if isinstance(payload, dict) else None,
        server=server,
    )
    if state is None:
        state = deepcopy(cached) if cached is not None else None
    if state is not None:
        with _STATE_LOCKS_GUARD:
            _STATE_CACHE[scope] = deepcopy(state)
        return deepcopy(state)
    return None


def _sync_state(target: FeedState, source: FeedState) -> FeedState:
    target.active_feed = source.active_feed
    target.prepared_feed = source.prepared_feed
    target.active_version = source.active_version
    target.prepared_base_version = source.prepared_base_version
    target.profile_fingerprint = source.profile_fingerprint
    target.dirty_reasons = list(source.dirty_reasons)
    target.updated_at = source.updated_at
    target.generation_status = source.generation_status
    target.preparation_lease_started_at = source.preparation_lease_started_at
    target.preparation_lease_deadline = source.preparation_lease_deadline
    target.preparation_lease_reason = source.preparation_lease_reason
    target.preparation_lease_token = source.preparation_lease_token
    target.retry_at = source.retry_at
    target.retry_reason = source.retry_reason
    target.rotation_epoch = source.rotation_epoch
    target.prepared_rotation_epoch = source.prepared_rotation_epoch
    target.active_inventory_generation = source.active_inventory_generation
    target.prepared_inventory_generation = source.prepared_inventory_generation
    target.prepared_intent_version = source.prepared_intent_version
    target.ready_feeds = list(source.ready_feeds or [])[:2]
    target.recovery_attempt_keys = list(source.recovery_attempt_keys or [])[-16:]
    target.queue_revision = max(int(source.queue_revision or 0), 0)
    target.prepared_feed = (
        target.ready_feeds[0].artifact if target.ready_feeds else None
    )
    return target


def _retain_valid_queue_chain(
    active_feed: DiscoveryArtifact | None,
    queue: list[ReadyFeedEntry],
) -> tuple[list[ReadyFeedEntry], list[str]]:
    """Keep only the contiguous valid successor chain.

    A queued tail depends on its predecessor.  If a head is invalid (or a
    parent reference is broken), retaining later entries would create an
    orphan that can be promoted out of order.
    """
    retained: list[ReadyFeedEntry] = []
    rejected: list[str] = []
    expected_parent = str(active_feed.session_id or "") if active_feed else ""
    for entry in list(queue)[:2]:
        valid, reason = artifact_promotion_contract(entry.artifact)
        parent = str(entry.parent_session_id or "")
        if not valid:
            rejected.append(reason)
            break
        if parent and expected_parent and parent != expected_parent:
            rejected.append("queue_parent_mismatch")
            break
        retained.append(entry)
        expected_parent = entry.session_id
    return retained, rejected


def load_feed_state(
    server: Any,
    user_scope_id: str,
) -> FeedState | None:
    state = _load_state(server, _scope(user_scope_id))
    if state is None or not state.ready_feeds:
        return state
    original = list(state.ready_feeds)
    retained, rejected = _retain_valid_queue_chain(state.active_feed, original)
    if not rejected:
        return state
    state.ready_feeds = retained[:2]
    state.prepared_feed = retained[0].artifact if retained else None
    state.prepared_base_version = state.active_version if retained else 0
    state.prepared_rotation_epoch = retained[0].rotation_epoch if retained else 0
    state.prepared_inventory_generation = retained[0].inventory_generation if retained else ""
    state.prepared_intent_version = retained[0].intent_version if retained else 0
    state.generation_status = "successor_rejected"
    state.dirty_reasons = list(dict.fromkeys([
        *state.dirty_reasons,
        *[f"queued_artifact_rejected:{reason}" for reason in rejected],
    ]))
    save_feed_state(
        server,
        state,
        expected_active_version=state.active_version,
        expected_ready_session_ids=[entry.session_id for entry in original],
    )
    print(
        "[EBB:feed-queue] revalidate "
        f"scope={state.user_scope_id} retained={len(retained)} "
        f"rejected={','.join(rejected)}",
        flush=True,
    )
    return state


def promote_prepared_feed(
    server: Any,
    state: FeedState,
) -> DiscoveryArtifact | None:
    started = time.perf_counter()
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        queue = list(current.ready_feeds or [])
        if not queue and current.prepared_feed is not None:
            queue = [ReadyFeedEntry(artifact=current.prepared_feed)]
        if not queue:
            print("[EBB:feed-queue] promote depth=0 reason=empty elapsed_ms=0", flush=True)
            _sync_state(state, current)
            return None
        original_queue = list(queue)
        valid_queue, rejected_reasons = _retain_valid_queue_chain(current.active_feed, queue)
        if rejected_reasons:
            queue = valid_queue
            current.ready_feeds = queue[:2]
            current.prepared_feed = queue[0].artifact if queue else None
            current.generation_status = "successor_rejected"
            current.dirty_reasons = list(dict.fromkeys([
                *current.dirty_reasons,
                *[f"queued_artifact_rejected:{reason}" for reason in rejected_reasons],
            ]))
            if not save_feed_state(
                server,
                current,
                expected_active_version=current.active_version,
                expected_ready_session_ids=[entry.session_id for entry in original_queue],
            ):
                _sync_state(state, _load_state(server, state.user_scope_id) or state)
                return None
            print(
                "[EBB:feed-queue] reject "
                f"scope={current.user_scope_id} count={len(rejected_reasons)} "
                f"reason={','.join(rejected_reasons)}",
                flush=True,
            )
        if not queue:
            _sync_state(state, current)
            return None
        starting_depth = len(queue)
        expected_ready_session_ids = [entry.session_id for entry in queue]
        promoted_entry = queue.pop(0)
        current.active_feed = promoted_entry.artifact
        expected_version = current.active_version
        current.ready_feeds = queue[:2]
        current.prepared_feed = (
            current.ready_feeds[0].artifact if current.ready_feeds else None
        )
        current.active_version = max(current.active_version + 1, 1)
        current.rotation_epoch = max(
            promoted_entry.rotation_epoch, current.rotation_epoch + 1
        )
        current.active_inventory_generation = promoted_entry.inventory_generation
        current.prepared_base_version = (
            current.active_version if current.ready_feeds else 0
        )
        current.prepared_rotation_epoch = (
            current.ready_feeds[0].rotation_epoch if current.ready_feeds else 0
        )
        current.prepared_inventory_generation = (
            current.ready_feeds[0].inventory_generation if current.ready_feeds else ""
        )
        current.prepared_intent_version = (
            current.ready_feeds[0].intent_version if current.ready_feeds else 0
        )
        current.profile_fingerprint = str(current.active_feed.profile_key or "")
        current.recovery_attempt_keys = []
        current.dirty_reasons = []
        current.generation_status = "ready"
        current.preparation_lease_token = ""
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_ready_session_ids=expected_ready_session_ids,
        ):
            latest = _load_state(server, state.user_scope_id) or state
            _sync_state(state, latest)
            return None
        _sync_state(state, current)
        print(
            "[EBB:feed-queue] promote "
            f"before={starting_depth} depth={len(current.ready_feeds)} "
            f"reason=fifo elapsed_ms={int((time.perf_counter()-started)*1000)}",
            flush=True,
        )
        notify_feed_queue(current.user_scope_id)
        return current.active_feed


def store_active_feed(
    server: Any,
    state: FeedState,
    artifact: DiscoveryArtifact,
    *,
    profile_fingerprint: str,
    inventory_generation: str = "",
    rotation_epoch: int = 0,
    expected_active_version: int | None = None,
    expected_preparation_lease_token: str | None = None,
    clear_ready_queue: bool = False,
) -> FeedState | None:
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        expected_version = (
            state.active_version
            if expected_active_version is None
            else expected_active_version
        )
        if current.active_version != expected_version:
            _sync_state(state, current)
            return None
        if (
            expected_preparation_lease_token is not None
            and str(current.preparation_lease_token or "")
            != str(expected_preparation_lease_token or "")
        ):
            _sync_state(state, current)
            return None
        if (
            current.active_feed is not None
            and current.ready_feeds
            and not clear_ready_queue
        ):
            # A direct active replacement would orphan a successor chain
            # whose head was composed against the previous active artifact.
            # Let the caller reload/promote instead of erasing or reparenting
            # valid prepared work.
            _sync_state(state, current)
            return None
        expected_ready_session_ids = [
            entry.session_id for entry in current.ready_feeds or []
        ]
        current.active_feed = artifact
        current.active_version = max(current.active_version + 1, 1)
        current.rotation_epoch = max(int(rotation_epoch or 0), current.rotation_epoch)
        current.active_inventory_generation = str(inventory_generation or "")
        # Replacing the active artifact is ordinarily a status/initialization
        # update and must retain already prepared successors.  Queue removal
        # is reserved for explicit invalidation/contract rejection callers.
        if clear_ready_queue:
            current.prepared_feed = None
            current.ready_feeds = []
            current.prepared_base_version = 0
            current.prepared_rotation_epoch = 0
            current.prepared_inventory_generation = ""
            current.prepared_intent_version = 0
        current.profile_fingerprint = profile_fingerprint
        current.dirty_reasons = []
        current.generation_status = "ready"
        current.preparation_lease_token = ""
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_ready_session_ids=expected_ready_session_ids,
            expected_preparation_lease_token=expected_preparation_lease_token,
        ):
            latest = _load_state(server, state.user_scope_id) or state
            _sync_state(state, latest)
            return None
        return _sync_state(state, current)


def store_prepared_feed(
    server: Any,
    state: FeedState,
    artifact: DiscoveryArtifact,
    *,
    expected_active_version: int | None = None,
    expected_inventory_generation: str | None = None,
    inventory_generation: str = "",
    rotation_epoch: int = 0,
    intent_version: int = 0,
    expected_preparation_lease_token: str | None = None,
) -> PreparedFeedStoreResult:
    started = time.perf_counter()
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        expected_version = (
            state.active_version
            if expected_active_version is None
            else expected_active_version
        )
        if current.active_version != expected_version:
            _sync_state(state, current)
            return PreparedFeedStoreResult(VERSION_RACE, current, "active_version_changed")
        if (
            expected_inventory_generation is not None
            and current.active_inventory_generation
            and current.active_inventory_generation != expected_inventory_generation
        ):
            _sync_state(state, current)
            return PreparedFeedStoreResult(INVENTORY_RACE, current, "inventory_generation_changed")
        if (
            expected_preparation_lease_token is not None
            and str(current.preparation_lease_token or "")
            != str(expected_preparation_lease_token or "")
        ):
            _sync_state(state, current)
            return PreparedFeedStoreResult(
                VERSION_RACE,
                current,
                "preparation_lease_superseded",
            )
        queue = list(current.ready_feeds or [])
        if not queue and current.prepared_feed is not None:
            queue = [ReadyFeedEntry(artifact=current.prepared_feed)]
        if len(queue) >= 2:
            print("[EBB:feed-queue] append depth=2 reason=target elapsed_ms=0", flush=True)
            return PreparedFeedStoreResult(QUEUE_FULL, _sync_state(state, current), "ready_queue_full", {"depth": len(queue)})

        valid_artifact, contract_reason = artifact_promotion_contract(artifact)
        if not valid_artifact or not str(artifact.session_id or "").strip():
            print(f"[EBB:feed-queue] reject depth={len(queue)} reason=artifact_invalid elapsed_ms={int((time.perf_counter()-started)*1000)}", flush=True)
            artifact.diagnostics = dict(artifact.diagnostics or {})
            artifact.diagnostics["queue_rejection_reason"] = contract_reason
            _sync_state(state, current)
            return PreparedFeedStoreResult(CONTRACT_SHORTAGE, current, contract_reason)
        existing_artifacts = [
            value
            for value in [
                current.active_feed,
                *[entry.artifact for entry in queue],
            ]
            if value is not None
        ]
        artifact_signature = _feed_content_signature(artifact)
        for existing in existing_artifacts:
            if str(existing.session_id or "") == str(artifact.session_id or ""):
                _sync_state(state, current)
                return PreparedFeedStoreResult(DUPLICATE, current, "session_id_duplicate")
            if _feed_content_signature(existing) == artifact_signature:
                print(f"[EBB:feed-queue] reject depth={len(queue)} reason=duplicate elapsed_ms={int((time.perf_counter()-started)*1000)}", flush=True)
                _sync_state(state, current)
                return PreparedFeedStoreResult(DUPLICATE, current, "content_duplicate")

        parent = queue[-1].artifact if queue else current.active_feed
        if parent is not None:
            parent_optional = _completed_optional_rows(parent)
            candidate_optional = _completed_optional_rows(artifact)
            if not parent_optional.issubset(candidate_optional):
                print(f"[EBB:feed-queue] reject depth={len(queue)} reason=optional_rows elapsed_ms={int((time.perf_counter()-started)*1000)}", flush=True)
                _sync_state(state, current)
                return PreparedFeedStoreResult(CONTRACT_SHORTAGE, current, "optional_row_contract")
            if any(
                _optional_row_quality(artifact, kind)
                < _optional_row_quality(parent, kind)
                for kind in parent_optional
            ):
                print(f"[EBB:feed-queue] reject depth={len(queue)} reason=optional_quality elapsed_ms={int((time.perf_counter()-started)*1000)}", flush=True)
                _sync_state(state, current)
                return PreparedFeedStoreResult(QUALITY_REJECTION, current, "optional_quality")

        expected_ready_session_ids = [entry.session_id for entry in queue]
        artifact.diagnostics = dict(artifact.diagnostics or {})
        parent_session_id = str(parent.session_id or "") if parent is not None else ""
        artifact.diagnostics["queue_parent_session_id"] = parent_session_id
        artifact.diagnostics["queue_position"] = len(queue) + 1
        queue.append(
            ReadyFeedEntry(
                artifact=artifact,
                parent_session_id=parent_session_id,
                inventory_generation=str(inventory_generation or ""),
                rotation_epoch=max(
                    int(rotation_epoch or 0),
                    current.rotation_epoch + len(queue) + 1,
                ),
                intent_version=max(int(intent_version or 0), 0),
                profile_fingerprint=str(artifact.profile_key or ""),
            )
        )
        current.ready_feeds = queue
        current.prepared_feed = queue[0].artifact
        current.prepared_base_version = current.active_version
        current.prepared_rotation_epoch = queue[0].rotation_epoch
        current.prepared_inventory_generation = queue[0].inventory_generation
        current.prepared_intent_version = queue[0].intent_version
        current.dirty_reasons = []
        current.generation_status = "prepared"
        current.preparation_lease_token = ""
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_ready_session_ids=expected_ready_session_ids,
            expected_preparation_lease_token=expected_preparation_lease_token,
        ):
            latest = _load_state(server, state.user_scope_id) or state
            _sync_state(state, latest)
            return PreparedFeedStoreResult(PERSISTENCE_FAILURE, latest, "save_feed_state_failed")
        print(f"[EBB:feed-queue] append depth={len(queue)} reason=prepared elapsed_ms={int((time.perf_counter()-started)*1000)}", flush=True)
        notify_feed_queue(current.user_scope_id)
        return PreparedFeedStoreResult(STORED, _sync_state(state, current), "stored", {"depth": len(queue)})


def retain_compatible_ready_feeds(
    server: Any,
    state: FeedState,
    profile_fingerprint: str,
) -> FeedState:
    """Drop only successors built for a different strong profile epoch."""
    normalized_profile = str(profile_fingerprint or "")
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        original = list(current.ready_feeds or [])
        compatible = [
            entry
            for entry in original
            if not entry.profile_fingerprint
            or entry.profile_fingerprint == normalized_profile
        ]
        retained, _ = _retain_valid_queue_chain(current.active_feed, compatible)
        if len(retained) == len(original):
            return _sync_state(state, current)
        expected_ready_session_ids = [entry.session_id for entry in original]
        current.ready_feeds = retained
        current.prepared_feed = retained[0].artifact if retained else None
        current.prepared_base_version = current.active_version if retained else 0
        current.prepared_rotation_epoch = retained[0].rotation_epoch if retained else 0
        current.prepared_inventory_generation = (
            retained[0].inventory_generation if retained else ""
        )
        current.prepared_intent_version = retained[0].intent_version if retained else 0
        current.generation_status = "dirty"
        save_feed_state(
            server,
            current,
            expected_active_version=current.active_version,
            expected_ready_session_ids=expected_ready_session_ids,
        )
        return _sync_state(state, current)


def mark_feed_dirty(server: Any, state: FeedState, reason: str) -> None:
    normalized = str(reason or "profile_changed").strip() or "profile_changed"
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        if normalized not in current.dirty_reasons:
            current.dirty_reasons.append(normalized)
        current.generation_status = "dirty"
        save_feed_state(server, current)
        _sync_state(state, current)


def mark_feed_build_failed(server: Any, state: FeedState, reason: str) -> None:
    normalized = str(reason or "feed_build_failed").strip() or "feed_build_failed"
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        if normalized not in current.dirty_reasons:
            current.dirty_reasons.append(normalized)
        current.generation_status = "build_failed"
        save_feed_state(server, current)
        _sync_state(state, current)


def mark_feed_replenishing(
    server: Any,
    state: FeedState,
    shortages: list[str],
) -> None:
    normalized = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in shortages or []
            if str(value or "").strip()
        )
    )
    reason = "row_shortage:" + ",".join(normalized)
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        current.dirty_reasons = [reason]
        current.generation_status = "inventory_building"
        save_feed_state(server, current)
        _sync_state(state, current)


def invalidate_feed_state(server: Any, user_scope_id: str) -> None:
    scope = _scope(user_scope_id)
    key = _state_key(scope)
    with _STATE_LOCKS_GUARD:
        _STATE_CACHE.pop(scope, None)
    try:
        get_session_store().delete(key)
    except Exception:
        pass
    from ..recommend.store_runtime import open_recommendation_store_connection

    try:
        connection = open_recommendation_store_connection(server)
    except Exception:
        return
    try:
        connection.execute(
            "DELETE FROM recommendation_feature_store WHERE namespace = ? AND entity_id = ?",
            [FEED_STATE_NAMESPACE, key],
        )
        connection.execute(
            "DELETE FROM recommendation_feature_store WHERE namespace = ? AND entity_id LIKE ?",
            [FEED_ARTIFACT_NAMESPACE, scope + ":%"],
        )
        connection.commit()
    except Exception:
        pass
    finally:
        connection.close()
