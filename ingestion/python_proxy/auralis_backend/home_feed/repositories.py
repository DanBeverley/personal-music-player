"""Public Home repository facade and transactional feed promotion."""
from __future__ import annotations

from collections.abc import (Mapping)
from collections import OrderedDict
from typing import (Any)
import hashlib
import json
import sqlite3
import time
import os
from threading import Lock

from .contracts import (
    QUEUE_TARGET_DEPTH,
    READY_ARTIFACT_TTL_SECONDS,
    STALE_ACTIVE_FALLBACK_TTL_SECONDS,
    CandidateSnapshotRecord,
    FeedArtifactRecord,
    HomeJobRecord,
    HomeJobStatus,
    HomeJobType,
    HomeStateRecord,
    RadioSnapshotRecord,
    evaluate_feed_contract,
    retry_delay_seconds,
)
from .bundles import is_complete_feed_bundle_payload



from .repository_records import (
    HomeRepositoryConflict,
    ImmutableRecordConflict,
    InvalidArtifactReference,
    StaleRevision,
    QueueFull,
    LeaseLost,
    recommendation_store_connection_factory,
    _canonical_json,
    _content_hash,
    _scope,
    _feed_payload_accepted,
    ensure_home_feed_schema,
    _Repository,
    SnapshotReference,
    _SnapshotRepository,
    CandidateSnapshotRepository,
    HomeExposureRepository,
    RadioSnapshotRepository,
    FeedArtifactRepository,
    ConnectionFactory,
    HOME_FEED_SCHEMA_VERSION,
)
from .repository_jobs import HomeJobRepository, _job_from_row
_repository_set_lock = Lock()

def _decode_ready_ids(raw: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        decoded = []
    if not isinstance(decoded, list):
        return ()
    return tuple(str(value).strip() for value in decoded if str(value).strip())


def _state_from_row(row: sqlite3.Row) -> HomeStateRecord:
    return HomeStateRecord(
        user_scope_id=str(row["user_scope_id"]),
        active_artifact_id=str(row["active_artifact_id"] or ""),
        ready_artifact_ids=_decode_ready_ids(row["ready_artifact_ids_json"]),
        active_version=max(int(row["active_version"] or 0), 0),
        revision=max(int(row["revision"] or 0), 0),
        last_launch_token=str(row["last_launch_token"] or ""),
        active_since=float(row["active_since"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
    )


class HomeStateRepository(_Repository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bundle_checks = OrderedDict()
        self._bundle_check_lock = Lock()

    def _bundle_is_accepted(self, raw: str) -> bool:
        # Retain only a digest/result, never another resident artifact tree.
        # A changed payload must be fully validated even under the same ID.
        if not isinstance(raw, str):
            return False
        digest = hashlib.sha256(raw.encode('utf-8')).digest()
        with self._bundle_check_lock:
            if digest in self._bundle_checks:
                self._bundle_checks.move_to_end(digest)
                return self._bundle_checks[digest]
        try:
            payload = json.loads(raw)
            accepted = (isinstance(payload, Mapping) and is_complete_feed_bundle_payload(payload)
                        and _feed_payload_accepted(payload))
        except (TypeError, json.JSONDecodeError):
            accepted = False
        with self._bundle_check_lock:
            self._bundle_checks[digest] = accepted
            while len(self._bundle_checks) > 64:
                self._bundle_checks.popitem(last=False)
        return accepted

    @staticmethod
    def _require_artifact(
        connection: sqlite3.Connection,
        *,
        user_scope_id: str,
        artifact_id: str,
        now: float,
        promotable: bool,
        require_complete_bundle: bool = False,
    ) -> None:
        row = connection.execute(
            """
            SELECT user_scope_id, created_at, expires_at, payload_json
            FROM home_feed_artifacts WHERE artifact_id = ?
            """,
            [artifact_id],
        ).fetchone()
        if row is None or str(row["user_scope_id"] or "") != user_scope_id:
            raise InvalidArtifactReference("artifact is missing or belongs to another scope")
        created_at = float(row["created_at"] or 0.0)
        expires_at = float(row["expires_at"] or 0.0)
        if created_at > now or expires_at <= now:
            raise InvalidArtifactReference("artifact is not currently servable")
        if promotable and now - created_at > READY_ARTIFACT_TTL_SECONDS:
            raise InvalidArtifactReference("artifact exceeded the ready promotion TTL")
        if require_complete_bundle:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise InvalidArtifactReference("artifact bundle payload is invalid") from exc
            if (
                not isinstance(payload, Mapping)
                or not is_complete_feed_bundle_payload(payload)
                or not _feed_payload_accepted(payload)
            ):
                raise InvalidArtifactReference("artifact bundle is not complete")

    def get(self, user_scope_id: str) -> HomeStateRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [_scope(user_scope_id)],
            ).fetchone()
            return _state_from_row(row) if row is not None else None
        finally:
            connection.close()

    def get_or_create(self, user_scope_id: str, *, now: float | None = None) -> HomeStateRecord:
        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO home_feed_state(user_scope_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(user_scope_id) DO NOTHING
                """,
                [scope, timestamp],
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [scope],
            ).fetchone()
            if row is None:
                raise HomeRepositoryConflict("home state insert did not persist")
            return _state_from_row(row)
        finally:
            connection.close()

    def reconcile(
        self,
        user_scope_id: str,
        *,
        now: float | None = None,
        _attempt: int = 0,
    ) -> HomeStateRecord:
        """Remove missing/expired references without deleting immutable history."""

        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [scope],
            ).fetchone()
            if row is None:
                return self.get_or_create(scope, now=timestamp)
            state = _state_from_row(row)

            def valid(artifact_id: str, *, promotable: bool) -> bool:
                artifact = connection.execute(
                    "SELECT created_at, expires_at, payload_json FROM home_feed_artifacts "
                    "WHERE artifact_id = ? AND user_scope_id = ?",
                    [artifact_id, scope],
                ).fetchone()
                if artifact is None:
                    return False
                created_at = float(artifact["created_at"] or 0.0)
                expires_at = float(artifact["expires_at"] or 0.0)
                if created_at > timestamp or expires_at <= timestamp:
                    return False
                if promotable and timestamp - created_at > READY_ARTIFACT_TTL_SECONDS:
                    return False
                if promotable or artifact_id == state.active_artifact_id:
                    if not self._bundle_is_accepted(artifact["payload_json"] or "{}"):
                        return False
                return True

            active = (
                state.active_artifact_id
                if state.active_artifact_id
                and valid(state.active_artifact_id, promotable=False)
                else ""
            )
            ready = tuple(
                artifact_id
                for artifact_id in state.ready_artifact_ids
                if artifact_id != active and valid(artifact_id, promotable=True)
            )[:QUEUE_TARGET_DEPTH]
            if active == state.active_artifact_id and ready == state.ready_artifact_ids:
                connection.rollback()
                return state
            changed = connection.execute(
                """
                UPDATE home_feed_state
                SET active_artifact_id = ?, ready_artifact_ids_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [
                    active,
                    _canonical_json(list(ready)),
                    timestamp,
                    scope,
                    state.revision,
                ],
            )
            if changed.rowcount != 1:
                connection.rollback()
                if _attempt >= 2:
                    raise StaleRevision("home state changed during reconciliation")
                return self.reconcile(scope, now=timestamp, _attempt=_attempt + 1)
            connection.commit()
            return self.get(scope)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_active(
        self,
        user_scope_id: str,
        artifact_id: str,
        *,
        expected_revision: int,
        now: float | None = None,
    ) -> HomeStateRecord:
        scope = _scope(user_scope_id)
        artifact = str(artifact_id or "").strip()
        if not artifact:
            raise ValueError("artifact_id is required")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_artifact(
                connection,
                user_scope_id=scope,
                artifact_id=artifact,
                now=timestamp,
                promotable=False,
                require_complete_bundle=True,
            )
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [scope],
            ).fetchone()
            if row is None:
                raise HomeRepositoryConflict("home state does not exist")
            state = _state_from_row(row)
            if state.revision != int(expected_revision):
                raise StaleRevision("home state revision changed")
            if state.active_artifact_id and state.active_artifact_id != artifact:
                raise HomeRepositoryConflict("active artifact is already initialized")
            if artifact in state.ready_artifact_ids:
                raise HomeRepositoryConflict("active artifact is already queued")
            if state.active_artifact_id == artifact:
                connection.rollback()
                return state
            connection.execute(
                """
                UPDATE home_feed_state
                SET active_artifact_id = ?, active_since = ?,
                    active_version = CASE
                        WHEN active_version > 0 THEN active_version + 1
                        ELSE 1
                    END,
                    revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [artifact, timestamp, timestamp, scope, expected_revision],
            )
            connection.commit()
            return self.get(scope)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_ready(
        self,
        user_scope_id: str,
        artifact_id: str,
        *,
        expected_revision: int,
        now: float | None = None,
    ) -> HomeStateRecord:
        scope = _scope(user_scope_id)
        artifact = str(artifact_id or "").strip()
        if not artifact:
            raise ValueError("artifact_id is required")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_artifact(
                connection,
                user_scope_id=scope,
                artifact_id=artifact,
                now=timestamp,
                promotable=True,
                require_complete_bundle=True,
            )
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [scope],
            ).fetchone()
            if row is None:
                raise HomeRepositoryConflict("home state does not exist")
            state = _state_from_row(row)
            if state.revision != int(expected_revision):
                raise StaleRevision("home state revision changed")
            if artifact == state.active_artifact_id or artifact in state.ready_artifact_ids:
                raise HomeRepositoryConflict("artifact is already active or queued")
            if len(state.ready_artifact_ids) >= QUEUE_TARGET_DEPTH:
                raise QueueFull("ready queue is full")
            ready = (*state.ready_artifact_ids, artifact)
            connection.execute(
                """
                UPDATE home_feed_state
                SET ready_artifact_ids_json = ?, revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [_canonical_json(list(ready)), timestamp, scope, expected_revision],
            )
            connection.commit()
            return self.get(scope)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def promote_ready(
        self,
        user_scope_id: str,
        *,
        expected_revision: int,
        launch_token: str = "",
        minimum_active_age: float = 0.0,
        expected_active_version: int | None = None,
        now: float | None = None,
    ) -> tuple[HomeStateRecord, str]:
        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM home_feed_state WHERE user_scope_id = ?",
                [scope],
            ).fetchone()
            if row is None:
                raise HomeRepositoryConflict("home state does not exist")
            state = _state_from_row(row)
            if state.revision != int(expected_revision):
                raise StaleRevision("home state revision changed")
            if (not state.ready_artifact_ids
                or (expected_active_version is not None and state.active_version != expected_active_version)
                or (launch_token and launch_token == state.last_launch_token)
                or (state.active_artifact_id and state.active_since > 0
                    and timestamp - state.active_since < minimum_active_age)):
                connection.rollback()
                return state, ""
            promoted = state.ready_artifact_ids[0]
            self._require_artifact(
                connection,
                user_scope_id=scope,
                artifact_id=promoted,
                now=timestamp,
                promotable=True,
                require_complete_bundle=True,
            )
            remaining = state.ready_artifact_ids[1:]
            connection.execute(
                """
                UPDATE home_feed_state
                SET active_artifact_id = ?, ready_artifact_ids_json = ?, active_since = ?,
                    active_version = active_version + 1,
                    last_launch_token = CASE WHEN ? != '' THEN ?
                        ELSE last_launch_token END,
                    revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [
                    promoted,
                    _canonical_json(list(remaining)),
                    timestamp,
                    str(launch_token or ""),
                    str(launch_token or ""),
                    timestamp,
                    scope,
                    expected_revision,
                ],
            )
            connection.commit()
            return self.get(scope), promoted  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class HomeRepositorySet:
    """One construction point for all durable Home repositories."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        ensure_home_feed_schema(connection_factory)
        self._connection_factory = connection_factory
        self.state = HomeStateRepository(connection_factory, ensure_schema=False)
        self.jobs = HomeJobRepository(connection_factory, ensure_schema=False)
        self.candidates = CandidateSnapshotRepository(
            connection_factory, ensure_schema=False
        )
        self.radio = RadioSnapshotRepository(connection_factory, ensure_schema=False)
        self.artifacts = FeedArtifactRepository(connection_factory, ensure_schema=False)
        self.exposures = HomeExposureRepository(connection_factory, ensure_schema=False)
        # Startup/schema reconciliation is intentionally non-destructive: it
        # preserves succeeded history while making the actionable queue sane.
        self.jobs.reconcile_queue()

    @classmethod
    def from_server(cls, server: Any) -> "HomeRepositorySet":
        path = getattr(server, "RECOMMENDATION_STORE_DB_PATH", None)
        key = (os.getpid(), os.path.realpath(str(path)) if path else id(server))
        with _repository_set_lock:
            cached = getattr(server, "_home_repository_set", None)
            if cached is None or cached[0] != key:
                cached = (key, cls(recommendation_store_connection_factory(server)))
                server._home_repository_set = cached
            return cached[1]

    def delete_scope(self, user_scope_id: str) -> dict[str, int]:
        """Delete all durable Home state for an explicit account reset."""

        scope = _scope(user_scope_id)
        tables = (
            "home_feed_state",
            "home_feed_jobs",
            "home_feed_artifacts",
            "home_feed_exposures",
            "home_radio_snapshots",
            "home_candidate_snapshots",
        )
        deleted: dict[str, int] = {}
        connection = self._connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in tables:
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE user_scope_id = ?",
                    [scope],
                )
                deleted[table] = max(int(cursor.rowcount or 0), 0)
            connection.commit()
            return deleted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
