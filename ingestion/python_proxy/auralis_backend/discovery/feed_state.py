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


FEED_STATE_NAMESPACE = "discovery_feed_state_v2"
FEED_STATE_MODEL_VERSION = "feed-state-v2"
FEED_STATE_TTL_SECONDS = 60 * 60 * 24 * 14
FEED_ARTIFACT_NAMESPACE = "discovery_feed_artifact"
FEED_ARTIFACT_MODEL = "discovery-feed-artifact"


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
    rotation_epoch: int = 0
    prepared_rotation_epoch: int = 0
    active_inventory_generation: str = ""
    prepared_inventory_generation: str = ""
    prepared_intent_version: int = 0


_STATE_LOCKS: Dict[str, threading.RLock] = {}
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_CACHE: Dict[str, FeedState] = {}


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
    return {
        kind
        for kind in optional
        if int(counts.get(kind) or 0) >= ROW_RECIPES[kind].min_items
    }


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


def _optional_row_signature(
    artifact: DiscoveryArtifact | None,
    kind: str,
) -> str:
    def item_signature(item: Dict[str, Any]) -> Any:
        nested = item.get("tracks") or item.get("items")
        return {
            "id": str(
                item.get("canonical_entity_id")
                or item.get("canonical_track_identity")
                or item.get("id")
                or item.get("videoId")
                or item.get("title")
                or ""
            ),
            "thumbnail": str(item.get("thumbnail") or ""),
            "nested": [
                item_signature(value)
                for value in nested
                if isinstance(value, dict)
            ]
            if isinstance(nested, list)
            else [],
        }

    return json.dumps(
        [item_signature(item) for item in _optional_row_payload(artifact, kind)],
        sort_keys=True,
        ensure_ascii=True,
    )


def _optional_row_quality(
    artifact: DiscoveryArtifact | None,
    kind: str,
) -> tuple[int, int, int]:
    items = _optional_row_payload(artifact, kind)
    artwork_count = sum(
        1
        for item in items
        if str(item.get("thumbnail") or "").strip()
        or any(
            str(value or "").strip()
            for value in item.get("collage_images") or []
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
    return len(items), artwork_count, nested_track_count


def _scope(value: Any) -> str:
    return str(value or "guest").strip() or "guest"


def _state_key(user_scope_id: str) -> str:
    return f"feed-state:{_scope(user_scope_id)}"


def feed_state_lock(user_scope_id: str) -> threading.RLock:
    scope = _scope(user_scope_id)
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(scope, threading.RLock())


def _state_to_payload(state: FeedState) -> Dict[str, Any]:
    return {
        "state_version": FEED_STATE_MODEL_VERSION,
        "user_scope_id": _scope(state.user_scope_id),
        "active_feed_session_id": str(
            state.active_feed.session_id if state.active_feed is not None else ""
        ),
        "prepared_feed_session_id": str(
            state.prepared_feed.session_id if state.prepared_feed is not None else ""
        ),
        "active_version": int(state.active_version or 0),
        "prepared_base_version": int(state.prepared_base_version or 0),
        "profile_fingerprint": str(state.profile_fingerprint or ""),
        "dirty_reasons": list(dict.fromkeys(str(reason) for reason in state.dirty_reasons if str(reason))),
        "updated_at": float(state.updated_at or time.time()),
        "generation_status": str(state.generation_status or "idle"),
        "rotation_epoch": max(int(state.rotation_epoch or 0), 0),
        "prepared_rotation_epoch": max(int(state.prepared_rotation_epoch or 0), 0),
        "active_inventory_generation": str(state.active_inventory_generation or ""),
        "prepared_inventory_generation": str(state.prepared_inventory_generation or ""),
        "prepared_intent_version": max(int(state.prepared_intent_version or 0), 0),
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
    artifact_cutover = isinstance(payload.get("active_feed"), dict) and active is None
    return FeedState(
        user_scope_id=user_scope_id,
        active_feed=active,
        prepared_feed=prepared,
        active_version=max(int(payload.get("active_version") or 0), 0),
        prepared_base_version=max(int(payload.get("prepared_base_version") or 0), 0),
        profile_fingerprint=(
            "" if artifact_cutover else str(payload.get("profile_fingerprint") or "")
        ),
        dirty_reasons=(
            ["artifact_version_cutover"]
            if artifact_cutover
            else [str(reason) for reason in payload.get("dirty_reasons") or [] if str(reason)]
        ),
        updated_at=float(payload.get("updated_at") or 0.0),
        generation_status=(
            "idle" if artifact_cutover else str(payload.get("generation_status") or "idle")
        ),
        rotation_epoch=max(int(payload.get("rotation_epoch") or 0), 0),
        prepared_rotation_epoch=max(int(payload.get("prepared_rotation_epoch") or 0), 0),
        active_inventory_generation=str(payload.get("active_inventory_generation") or ""),
        prepared_inventory_generation=str(payload.get("prepared_inventory_generation") or ""),
        prepared_intent_version=max(int(payload.get("prepared_intent_version") or 0), 0),
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
    expected_prepared_session: str | None = None,
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
            expected_active_version is not None
            and max(int(current_payload.get("active_version") or 0), 0)
            != max(int(expected_active_version or 0), 0)
        ):
            print(
                "[EBB:feed-state][cas-miss] "
                f"scope={payload.get('user_scope_id') or ''} field=active_version "
                f"expected={expected_active_version} "
                f"actual={current_payload.get('active_version') or 0}",
                flush=True,
            )
            connection.rollback()
            return False
        current_prepared = current_payload.get("prepared_feed")
        current_prepared_session = str(
            current_payload.get("prepared_feed_session_id") or ""
        )
        if not current_prepared_session and isinstance(current_prepared, dict):
            current_prepared_session = str(current_prepared.get("session_id") or "")
        if (
            expected_prepared_session is not None
            and current_prepared_session != str(expected_prepared_session or "")
        ):
            print(
                "[EBB:feed-state][cas-miss] "
                f"scope={payload.get('user_scope_id') or ''} field=prepared_session "
                f"expected={expected_prepared_session or '-'} "
                f"actual={current_prepared_session or '-'}",
                flush=True,
            )
            connection.rollback()
            return False
        artifact_values = {
            _artifact_entity_id(payload.get("user_scope_id") or "guest", session_id): artifact
            for session_id, artifact in dict(artifacts or {}).items()
            if str(session_id or "").strip() and artifact is not None
        }
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
        missing_artifact_rows = [
            [
                FEED_ARTIFACT_NAMESPACE,
                entity_id,
                FEED_ARTIFACT_MODEL,
                json.dumps(artifact_to_dict(artifact), ensure_ascii=False),
                time.time(),
            ]
            for entity_id, artifact in artifact_values.items()
            if entity_id not in existing_artifacts
        ]
        if missing_artifact_rows:
            connection.executemany(
                """
                INSERT OR IGNORE INTO recommendation_feature_store(
                    namespace, entity_id, model_id, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                missing_artifact_rows,
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
    expected_prepared_session: str | None = None,
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
            str(artifact.session_id): artifact
            for artifact in (state.active_feed, state.prepared_feed)
            if artifact is not None and str(artifact.session_id or "").strip()
        },
        expected_active_version=expected_active_version,
        expected_prepared_session=expected_prepared_session,
    )
    if persistent_result is False and (
        expected_active_version is not None or expected_prepared_session is not None
    ):
        return False
    cache_saved = persistent_result is True or persistent_result is None or (
        expected_active_version is None
        and expected_prepared_session is None
    )
    if cache_saved:
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
    with _STATE_LOCKS_GUARD:
        cached = _STATE_CACHE.get(scope)
    if cached is not None:
        return deepcopy(cached)
    key = _state_key(user_scope_id)
    payload = None
    try:
        payload = get_session_store().get(key)
    except Exception:
        payload = None
    state = _state_from_payload(
        payload if isinstance(payload, dict) else None,
        server=server,
    )
    state = state or _state_from_payload(_persistent_get(server, key), server=server)
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
    target.rotation_epoch = source.rotation_epoch
    target.prepared_rotation_epoch = source.prepared_rotation_epoch
    target.active_inventory_generation = source.active_inventory_generation
    target.prepared_inventory_generation = source.prepared_inventory_generation
    target.prepared_intent_version = source.prepared_intent_version
    return target


def load_feed_state(
    server: Any,
    user_scope_id: str,
) -> FeedState | None:
    return _load_state(server, _scope(user_scope_id))


def promote_prepared_feed(
    server: Any,
    state: FeedState,
) -> DiscoveryArtifact | None:
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        if current.prepared_feed is None:
            _sync_state(state, current)
            return None
        if current.active_feed is not None and current.prepared_base_version != current.active_version:
            current.prepared_feed = None
            current.prepared_base_version = 0
            current.generation_status = "stale_prepared_discarded"
            save_feed_state(server, current)
            _sync_state(state, current)
            return None
        current.active_feed = current.prepared_feed
        expected_version = current.active_version
        expected_prepared_session = str(current.prepared_feed.session_id or "")
        current.prepared_feed = None
        current.active_version = max(current.active_version + 1, 1)
        current.rotation_epoch = max(current.prepared_rotation_epoch, current.rotation_epoch + 1)
        current.active_inventory_generation = current.prepared_inventory_generation
        current.prepared_base_version = 0
        current.prepared_rotation_epoch = 0
        current.prepared_inventory_generation = ""
        current.prepared_intent_version = 0
        current.profile_fingerprint = str(current.active_feed.profile_key or "")
        current.dirty_reasons = []
        current.generation_status = "ready"
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_prepared_session=expected_prepared_session,
        ):
            latest = _load_state(server, state.user_scope_id) or state
            _sync_state(state, latest)
            return None
        _sync_state(state, current)
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
) -> FeedState | None:
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        expected_version = state.active_version if expected_active_version is None else expected_active_version
        if current.active_version != expected_version:
            _sync_state(state, current)
            return None
        expected_prepared_session = str(
            current.prepared_feed.session_id if current.prepared_feed is not None else ""
        )
        current.active_feed = artifact
        current.active_version = max(current.active_version + 1, 1)
        current.rotation_epoch = max(int(rotation_epoch or 0), current.rotation_epoch)
        current.active_inventory_generation = str(inventory_generation or "")
        current.prepared_feed = None
        current.prepared_base_version = 0
        current.prepared_rotation_epoch = 0
        current.prepared_inventory_generation = ""
        current.prepared_intent_version = 0
        current.profile_fingerprint = profile_fingerprint
        current.dirty_reasons = []
        current.generation_status = "ready"
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_prepared_session=expected_prepared_session,
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
) -> FeedState | None:
    with feed_state_lock(state.user_scope_id):
        current = _load_state(server, state.user_scope_id) or state
        expected_version = state.active_version if expected_active_version is None else expected_active_version
        if current.active_version != expected_version:
            _sync_state(state, current)
            return None
        if (
            expected_inventory_generation is not None
            and current.active_inventory_generation
            and current.active_inventory_generation != expected_inventory_generation
        ):
            _sync_state(state, current)
            return None
        if (
            current.prepared_feed is not None
            and current.prepared_base_version == current.active_version
        ):
            existing_optional = _completed_optional_rows(current.prepared_feed)
            replacement_optional = _completed_optional_rows(artifact)
            preserves_existing = existing_optional.issubset(replacement_optional)
            added_optional = replacement_optional - existing_optional
            quality_improved = any(
                _optional_row_quality(artifact, kind)
                > _optional_row_quality(current.prepared_feed, kind)
                for kind in existing_optional
            )
            optional_content_changed = any(
                _optional_row_signature(artifact, kind)
                != _optional_row_signature(current.prepared_feed, kind)
                for kind in existing_optional
            )
            newer_inventory = bool(
                inventory_generation
                and inventory_generation != current.prepared_inventory_generation
            )
            newer_intent = int(intent_version or 0) > int(
                current.prepared_intent_version or 0
            )
            if not preserves_existing or not (
                added_optional
                or quality_improved
                or newer_intent
                or (newer_inventory and optional_content_changed)
            ):
                return _sync_state(state, current)
            artifact.diagnostics = dict(artifact.diagnostics or {})
            artifact.diagnostics["prepared_replaced_for_optional_rows"] = sorted(
                added_optional
            )
            artifact.diagnostics["prepared_replacement_reasons"] = [
                *(
                    ["added_optional:" + ",".join(sorted(added_optional))]
                    if added_optional
                    else []
                ),
                *(["optional_quality_improved"] if quality_improved else []),
                *(["newer_intent"] if newer_intent else []),
                *(
                    ["new_inventory_optional_content_changed"]
                    if newer_inventory and optional_content_changed
                    else []
                ),
            ]
        expected_prepared_session = str(
            current.prepared_feed.session_id if current.prepared_feed is not None else ""
        )
        current.prepared_feed = artifact
        current.prepared_base_version = current.active_version
        current.prepared_rotation_epoch = max(int(rotation_epoch or 0), current.rotation_epoch + 1)
        current.prepared_inventory_generation = str(inventory_generation or "")
        current.prepared_intent_version = max(int(intent_version or 0), 0)
        current.dirty_reasons = []
        current.generation_status = "prepared"
        if not save_feed_state(
            server,
            current,
            expected_active_version=expected_version,
            expected_prepared_session=expected_prepared_session,
        ):
            latest = _load_state(server, state.user_scope_id) or state
            _sync_state(state, latest)
            return None
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
