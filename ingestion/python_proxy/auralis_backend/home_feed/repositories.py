from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Iterable
import hashlib
import json
import sqlite3
import time
import uuid

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


ConnectionFactory = Callable[[], sqlite3.Connection]
HOME_FEED_SCHEMA_VERSION = 7


class HomeRepositoryConflict(RuntimeError):
    pass


class ImmutableRecordConflict(HomeRepositoryConflict):
    pass


class InvalidArtifactReference(HomeRepositoryConflict):
    pass


class StaleRevision(HomeRepositoryConflict):
    pass


class QueueFull(HomeRepositoryConflict):
    pass


class LeaseLost(HomeRepositoryConflict):
    pass


def recommendation_store_connection_factory(server: Any) -> ConnectionFactory:
    """Adapt the existing recommendation SQLite store to the new repositories."""

    def connect() -> sqlite3.Connection:
        from ..recommend.store_runtime import open_recommendation_store_connection

        return open_recommendation_store_connection(server)

    return connect


def _canonical_json(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _scope(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("user_scope_id is required")
    return normalized


def _feed_payload_accepted(payload: Mapping[str, Any]) -> bool:
    if payload.get("accepted") is not True:
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    return evaluate_feed_contract(rows).accepted


def ensure_home_feed_schema(connection_factory: ConnectionFactory) -> None:
    """Create the first-class durable Home tables idempotently."""

    connection = connection_factory()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS home_feed_schema_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS home_feed_state (
                user_scope_id TEXT PRIMARY KEY,
                active_artifact_id TEXT NOT NULL DEFAULT '',
                ready_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                active_version INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                last_launch_token TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """
        )
        state_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(home_feed_state)").fetchall()
        }
        if "last_launch_token" not in state_columns:
            connection.execute(
                "ALTER TABLE home_feed_state "
                "ADD COLUMN last_launch_token TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS home_feed_jobs (
                job_id TEXT PRIMARY KEY,
                user_scope_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                generation_key TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                run_after REAL NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_scope_id, job_type, generation_key)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_feed_jobs_due
            ON home_feed_jobs(status, run_after, lease_expires_at, priority, created_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_feed_jobs_scope
            ON home_feed_jobs(user_scope_id, updated_at DESC)
            """
        )
        for table in ("home_candidate_snapshots", "home_radio_snapshots"):
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    snapshot_id TEXT PRIMARY KEY,
                    user_scope_id TEXT NOT NULL,
                    profile_fingerprint TEXT NOT NULL DEFAULT '',
                    generated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_scope_fresh
                ON {table}(user_scope_id, expires_at DESC, generated_at DESC)
                """
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS home_feed_artifacts (
                artifact_id TEXT PRIMARY KEY,
                user_scope_id TEXT NOT NULL,
                profile_fingerprint TEXT NOT NULL DEFAULT '',
                candidate_snapshot_id TEXT NOT NULL,
                radio_snapshot_id TEXT NOT NULL DEFAULT '',
                allocation_epoch INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        artifact_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(home_feed_artifacts)"
            ).fetchall()
        }
        if "allocation_epoch" not in artifact_columns:
            connection.execute(
                "ALTER TABLE home_feed_artifacts "
                "ADD COLUMN allocation_epoch INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_feed_artifacts_scope_time
            ON home_feed_artifacts(user_scope_id, created_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_feed_artifacts_supply_allocation
            ON home_feed_artifacts(
                user_scope_id, candidate_snapshot_id, radio_snapshot_id,
                allocation_epoch
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS home_feed_exposures (
                user_scope_id TEXT NOT NULL,
                row_kind TEXT NOT NULL,
                item_kind TEXT NOT NULL,
                item_identity TEXT NOT NULL,
                exposure_count INTEGER NOT NULL DEFAULT 1,
                last_exposed_at REAL NOT NULL,
                PRIMARY KEY(user_scope_id, row_kind, item_kind, item_identity)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_feed_exposures_scope_time
            ON home_feed_exposures(user_scope_id, last_exposed_at DESC)
            """
        )
        connection.execute(
            """
            INSERT INTO home_feed_schema_meta(singleton, version, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                version = CASE
                    WHEN excluded.version > home_feed_schema_meta.version
                    THEN excluded.version ELSE home_feed_schema_meta.version END,
                updated_at = excluded.updated_at
            """,
            [HOME_FEED_SCHEMA_VERSION, time.time()],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class _Repository:
    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        ensure_schema: bool = True,
    ) -> None:
        self._connection_factory = connection_factory
        if ensure_schema:
            ensure_home_feed_schema(connection_factory)

    def _connect(self) -> sqlite3.Connection:
        return self._connection_factory()


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
        updated_at=float(row["updated_at"] or 0.0),
    )


class HomeStateRepository(_Repository):
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
    ) -> HomeStateRecord:
        """Remove missing/expired references without deleting immutable history."""

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
                connection.execute(
                    "INSERT INTO home_feed_state(user_scope_id, updated_at) VALUES (?, ?)",
                    [scope, timestamp],
                )
                connection.commit()
                return self.get(scope)  # type: ignore[return-value]
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
                    try:
                        payload = json.loads(artifact["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        return False
                    if (
                        not isinstance(payload, Mapping)
                        or not is_complete_feed_bundle_payload(payload)
                        or not _feed_payload_accepted(payload)
                    ):
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
            connection.execute(
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
                SET active_artifact_id = ?,
                    active_version = CASE
                        WHEN active_version > 0 THEN active_version + 1
                        ELSE 1
                    END,
                    revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [artifact, timestamp, scope, expected_revision],
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
            if not state.ready_artifact_ids:
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
                SET active_artifact_id = ?, ready_artifact_ids_json = ?,
                    active_version = active_version + 1,
                    last_launch_token = CASE WHEN ? != '' THEN ?
                        ELSE last_launch_token END,
                    revision = revision + 1, updated_at = ?
                WHERE user_scope_id = ? AND revision = ?
                """,
                [
                    promoted,
                    _canonical_json(list(remaining)),
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


def _job_from_row(row: sqlite3.Row) -> HomeJobRecord:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return HomeJobRecord(
        job_id=str(row["job_id"]),
        user_scope_id=str(row["user_scope_id"]),
        job_type=HomeJobType(str(row["job_type"])),
        generation_key=str(row["generation_key"]),
        status=HomeJobStatus(str(row["status"])),
        priority=int(row["priority"] or 0),
        attempts=max(int(row["attempts"] or 0), 0),
        max_attempts=max(int(row["max_attempts"] or 0), 1),
        run_after=float(row["run_after"] or 0.0),
        lease_token=str(row["lease_token"] or ""),
        lease_owner=str(row["lease_owner"] or ""),
        lease_expires_at=float(row["lease_expires_at"] or 0.0),
        payload=payload if isinstance(payload, dict) else {},
        last_error=str(row["last_error"] or ""),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
    )


class HomeJobRepository(_Repository):
    def enqueue_singleton(
        self,
        user_scope_id: str,
        job_type: HomeJobType,
        generation_key: str,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        run_after: float | None = None,
        now: float | None = None,
    ) -> HomeJobRecord:
        """Atomically keep at most one actionable job of this type per scope."""

        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        due_at = timestamp if run_after is None else float(run_after)
        self.reconcile_expired(now=timestamp)
        encoded_payload = _canonical_json(dict(payload or {}))
        generation = str(generation_key or "").strip()
        if not generation:
            raise ValueError("generation_key is required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM home_feed_jobs
                WHERE user_scope_id = ? AND job_type = ?
                  AND (
                    status IN (?, ?)
                    OR (status = ? AND lease_expires_at > ?)
                  )
                ORDER BY CASE WHEN status = ? THEN 0 ELSE 1 END, created_at ASC
                LIMIT 1
                """,
                [
                    scope,
                    job_type.value,
                    HomeJobStatus.PENDING.value,
                    HomeJobStatus.RETRY.value,
                    HomeJobStatus.RUNNING.value,
                    timestamp,
                    HomeJobStatus.RUNNING.value,
                ],
            ).fetchone()
            if existing is not None:
                existing_job = _job_from_row(existing)
                if (
                    existing_job.status is HomeJobStatus.RUNNING
                    or existing_job.generation_key == generation
                ):
                    connection.execute(
                        """
                        UPDATE home_feed_jobs SET status = ?, updated_at = ?,
                            last_error = 'superseded_duplicate_job'
                        WHERE user_scope_id = ? AND job_type = ? AND job_id != ?
                          AND status IN (?, ?)
                        """,
                        [
                            HomeJobStatus.CANCELLED.value,
                            timestamp,
                            scope,
                            job_type.value,
                            existing_job.job_id,
                            HomeJobStatus.PENDING.value,
                            HomeJobStatus.RETRY.value,
                        ],
                    )
                    connection.commit()
                    return existing_job
                connection.execute(
                    """
                    UPDATE home_feed_jobs SET status = ?, updated_at = ?,
                        last_error = 'superseded_by_new_generation'
                    WHERE user_scope_id = ? AND job_type = ?
                      AND status IN (?, ?)
                    """,
                    [
                        HomeJobStatus.CANCELLED.value,
                        timestamp,
                        scope,
                        job_type.value,
                        HomeJobStatus.PENDING.value,
                        HomeJobStatus.RETRY.value,
                    ],
                )
            collision = connection.execute(
                """
                SELECT 1 FROM home_feed_jobs
                WHERE user_scope_id = ? AND job_type = ? AND generation_key = ?
                """,
                [scope, job_type.value, generation],
            ).fetchone()
            if collision is not None:
                generation = f"{generation}:{int(timestamp * 1000)}:{uuid.uuid4().hex[:8]}"
            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO home_feed_jobs(
                    job_id, user_scope_id, job_type, generation_key, status,
                    priority, attempts, max_attempts, run_after, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                [
                    job_id,
                    scope,
                    job_type.value,
                    generation,
                    HomeJobStatus.PENDING.value,
                    int(priority),
                    max(int(max_attempts), 1),
                    due_at,
                    encoded_payload,
                    timestamp,
                    timestamp,
                ],
            )
            connection.commit()
            return self.get(job_id)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue_coalesced(
        self, user_scope_id: str, job_type: HomeJobType, generation_key: str,
        *, payload: Mapping[str, Any] | None = None, priority: int = 0,
        max_attempts: int = 5, run_after: float | None = None,
    ) -> HomeJobRecord:
        """Return the existing generation even if mutable enrichment drifts."""
        scope = _scope(user_scope_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_jobs WHERE user_scope_id=? AND job_type=? AND generation_key=?",
                [scope, job_type.value, str(generation_key)],
            ).fetchone()
            if row is not None:
                return _job_from_row(row)
        finally:
            connection.close()
        try:
            return self.enqueue(scope, job_type, generation_key, payload=payload,
                                priority=priority, max_attempts=max_attempts,
                                run_after=run_after)
        except ImmutableRecordConflict:
            # Another scheduler won the generation between the read and
            # insert. Identity coalescing intentionally returns that record,
            # preserving its immutable payload/history.
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM home_feed_jobs WHERE user_scope_id=? AND job_type=? AND generation_key=?",
                    [scope, job_type.value, str(generation_key)],
                ).fetchone()
                if row is not None:
                    return _job_from_row(row)
            finally:
                connection.close()
            raise

    def reconcile_queue(self, *, now: float | None = None) -> int:
        """Repair queues created by older schedulers without deleting history."""
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        changed = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Pre-slot schedulers keyed compose work by every supply pair.
            # Those durable retries can race the current queue-slot owner
            # after an upgrade and create state-commit conflicts. Preserve
            # their history but drain every non-running legacy generation.
            legacy = connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0, last_error = ?, updated_at = ?
                WHERE job_type = ? AND generation_key NOT LIKE 'compose:slot:%'
                  AND status IN (?, ?)
                """,
                [
                    HomeJobStatus.CANCELLED.value,
                    "legacy_compose_generation_drained",
                    timestamp,
                    HomeJobType.COMPOSE_FEED.value,
                    HomeJobStatus.PENDING.value,
                    HomeJobStatus.RETRY.value,
                ],
            )
            changed += max(int(legacy.rowcount or 0), 0)
            # Composition is latency-critical and must win over optional
            # enrichment left by a previous process.
            updated = connection.execute(
                "UPDATE home_feed_jobs SET priority = 100, updated_at = ? "
                "WHERE job_type = ? AND status IN (?, ?, ?)",
                [timestamp, HomeJobType.COMPOSE_FEED.value,
                 HomeJobStatus.PENDING.value, HomeJobStatus.RETRY.value,
                 HomeJobStatus.RUNNING.value],
            )
            changed += max(int(updated.rowcount or 0), 0)
            rows = connection.execute(
                "SELECT job_id, user_scope_id, payload_json, status FROM home_feed_jobs "
                "WHERE job_type = ? AND status IN (?, ?, ?) "
                "ORDER BY CASE WHEN status = 'running' THEN 0 ELSE 1 END, created_at ASC, job_id ASC",
                [HomeJobType.REPAIR_ARTWORK.value, HomeJobStatus.PENDING.value,
                 HomeJobStatus.RETRY.value, HomeJobStatus.RUNNING.value],
            ).fetchall()
            seen: set[tuple[str, str]] = set()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                artist = payload.get("artist") if isinstance(payload, dict) else {}
                artist = artist if isinstance(artist, dict) else {}
                identity = str(
                    artist.get("canonical_artist_id")
                    or artist.get("musicbrainz_artist_id")
                    or artist.get("provider_artist_id")
                    or artist.get("id") or artist.get("name") or ""
                ).strip().casefold()
                scope = str(row["user_scope_id"] or "guest")
                key = (scope, identity)
                if not identity or key not in seen:
                    seen.add(key)
                    continue
                cancelled = connection.execute(
                    "UPDATE home_feed_jobs SET status = ?, lease_token = '', "
                    "lease_owner = '', lease_expires_at = 0, last_error = ?, updated_at = ? "
                    "WHERE job_id = ? AND status IN (?, ?)",
                    [HomeJobStatus.CANCELLED.value, "superseded_duplicate_identity", timestamp,
                     row["job_id"], HomeJobStatus.PENDING.value, HomeJobStatus.RETRY.value],
                )
                changed += max(int(cancelled.rowcount or 0), 0)
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, job_id: str) -> HomeJobRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_jobs WHERE job_id = ?",
                [str(job_id or "").strip()],
            ).fetchone()
            return _job_from_row(row) if row is not None else None
        finally:
            connection.close()

    def enqueue(
        self,
        user_scope_id: str,
        job_type: HomeJobType,
        generation_key: str,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        run_after: float | None = None,
        now: float | None = None,
    ) -> HomeJobRecord:
        scope = _scope(user_scope_id)
        generation = str(generation_key or "").strip()
        if not generation:
            raise ValueError("generation_key is required")
        timestamp = time.time() if now is None else float(now)
        due_at = timestamp if run_after is None else float(run_after)
        encoded_payload = _canonical_json(dict(payload or {}))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM home_feed_jobs
                WHERE user_scope_id = ? AND job_type = ? AND generation_key = ?
                """,
                [scope, job_type.value, generation],
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"] or "{}") != encoded_payload:
                    raise ImmutableRecordConflict("job key was reused with different payload")
                connection.rollback()
                return _job_from_row(existing)
            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO home_feed_jobs(
                    job_id, user_scope_id, job_type, generation_key, status,
                    priority, attempts, max_attempts, run_after, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                [
                    job_id,
                    scope,
                    job_type.value,
                    generation,
                    HomeJobStatus.PENDING.value,
                    int(priority),
                    max(int(max_attempts), 1),
                    due_at,
                    encoded_payload,
                    timestamp,
                    timestamp,
                ],
            )
            connection.commit()
            return self.get(job_id)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        lease_seconds: float = 60.0,
        job_types: tuple[HomeJobType, ...] = (),
    ) -> HomeJobRecord | None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ValueError("worker_id is required")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0,
                    last_error = CASE WHEN last_error = ''
                        THEN 'lease_expired_max_attempts' ELSE last_error END,
                    updated_at = ?
                WHERE status = ? AND lease_expires_at <= ?
                  AND attempts >= max_attempts
                """,
                [
                    HomeJobStatus.FAILED.value,
                    timestamp,
                    HomeJobStatus.RUNNING.value,
                    timestamp,
                ],
            )
            type_values = [value.value for value in job_types]
            type_clause = ""
            parameters: list[Any] = [
                HomeJobStatus.PENDING.value,
                HomeJobStatus.RETRY.value,
                timestamp,
                HomeJobStatus.RUNNING.value,
                timestamp,
                HomeJobStatus.RUNNING.value,
                timestamp,
            ]
            if type_values:
                placeholders = ",".join("?" for _ in type_values)
                type_clause = f" AND candidate.job_type IN ({placeholders})"
                parameters.extend(type_values)
            row = connection.execute(
                f"""
                SELECT candidate.* FROM home_feed_jobs AS candidate
                WHERE candidate.attempts < candidate.max_attempts
                  AND (
                    (candidate.status IN (?, ?) AND candidate.run_after <= ?)
                    OR (candidate.status = ? AND candidate.lease_expires_at <= ?)
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM home_feed_jobs AS active
                    WHERE active.user_scope_id = candidate.user_scope_id
                      AND active.job_type = candidate.job_type
                      AND active.status = ?
                      AND active.lease_expires_at > ?
                  )
                  {type_clause}
                ORDER BY candidate.priority DESC,
                         candidate.run_after ASC,
                         candidate.created_at ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = str(uuid.uuid4())
            updated = connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, attempts = attempts + 1,
                    lease_token = ?, lease_owner = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND attempts = ? AND status = ?
                """,
                [
                    HomeJobStatus.RUNNING.value,
                    token,
                    owner,
                    timestamp + max(float(lease_seconds), 1.0),
                    timestamp,
                    row["job_id"],
                    row["attempts"],
                    row["status"],
                ],
            )
            if updated.rowcount != 1:
                raise HomeRepositoryConflict("job claim lost a concurrent race")
            connection.commit()
            return self.get(str(row["job_id"]))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> HomeJobRecord:
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            updated = connection.execute(
                """
                UPDATE home_feed_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_token = ?
                """,
                [
                    timestamp + max(float(lease_seconds), 1.0),
                    timestamp,
                    job_id,
                    HomeJobStatus.RUNNING.value,
                    lease_token,
                ],
            )
            connection.commit()
            if updated.rowcount != 1:
                raise LeaseLost("job lease is no longer owned")
            return self.get(job_id)  # type: ignore[return-value]
        finally:
            connection.close()

    def complete(self, job_id: str, lease_token: str, *, now: float | None = None) -> HomeJobRecord:
        return self._finish(
            job_id,
            lease_token,
            status=HomeJobStatus.SUCCEEDED,
            error="",
            now=now,
        )

    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        now: float | None = None,
    ) -> HomeJobRecord:
        return self._finish(
            job_id,
            lease_token,
            status=HomeJobStatus.FAILED,
            error=error,
            now=now,
        )

    def retry(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float | None = None,
        now: float | None = None,
    ) -> HomeJobRecord:
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM home_feed_jobs WHERE job_id = ?",
                [job_id],
            ).fetchone()
            if row is None or str(row["status"]) != HomeJobStatus.RUNNING.value or str(
                row["lease_token"] or ""
            ) != str(lease_token or ""):
                raise LeaseLost("job lease is no longer owned")
            attempts = max(int(row["attempts"] or 0), 1)
            terminal = attempts >= max(int(row["max_attempts"] or 0), 1)
            status = HomeJobStatus.FAILED if terminal else HomeJobStatus.RETRY
            run_after = (
                timestamp
                if terminal
                else timestamp
                + (
                    max(float(delay_seconds), 0.0)
                    if delay_seconds is not None
                    else retry_delay_seconds(attempts)
                )
            )
            connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, run_after = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0, last_error = ?, updated_at = ?
                WHERE job_id = ? AND lease_token = ?
                """,
                [status.value, run_after, str(error or "")[:1000], timestamp, job_id, lease_token],
            )
            connection.commit()
            return self.get(job_id)  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        status: HomeJobStatus,
        error: str,
        now: float | None,
    ) -> HomeJobRecord:
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            updated = connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0, last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_token = ?
                """,
                [
                    status.value,
                    str(error or "")[:1000],
                    timestamp,
                    job_id,
                    HomeJobStatus.RUNNING.value,
                    lease_token,
                ],
            )
            connection.commit()
            if updated.rowcount != 1:
                raise LeaseLost("job lease is no longer owned")
            return self.get(job_id)  # type: ignore[return-value]
        finally:
            connection.close()

    def list_for_scope(self, user_scope_id: str) -> list[HomeJobRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM home_feed_jobs
                WHERE user_scope_id = ?
                ORDER BY created_at ASC, job_id ASC
                """,
                [_scope(user_scope_id)],
            ).fetchall()
            return [_job_from_row(row) for row in rows]
        finally:
            connection.close()

    def reconcile_expired(self, *, now: float | None = None) -> int:
        """Return abandoned leases to the shared retry state immediately."""

        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            terminal = connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0,
                    last_error = CASE WHEN last_error = ''
                        THEN 'lease_expired_max_attempts' ELSE last_error END,
                    updated_at = ?
                WHERE status = ? AND lease_expires_at <= ?
                  AND attempts >= max_attempts
                """,
                [
                    HomeJobStatus.FAILED.value,
                    timestamp,
                    HomeJobStatus.RUNNING.value,
                    timestamp,
                ],
            )
            retryable = connection.execute(
                """
                UPDATE home_feed_jobs
                SET status = ?, run_after = ?, lease_token = '', lease_owner = '',
                    lease_expires_at = 0,
                    last_error = 'lease_expired_worker_lost', updated_at = ?
                WHERE status = ? AND lease_expires_at <= ?
                  AND attempts < max_attempts
                """,
                [
                    HomeJobStatus.RETRY.value,
                    timestamp,
                    timestamp,
                    HomeJobStatus.RUNNING.value,
                    timestamp,
                ],
            )
            connection.commit()
            return max(int(terminal.rowcount or 0), 0) + max(
                int(retryable.rowcount or 0), 0
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def has_active(
        self,
        user_scope_id: str,
        *,
        job_types: tuple[HomeJobType, ...] = (),
        ignore_delayed_idle: bool = False,
        now: float | None = None,
    ) -> bool:
        """Return whether durable work is pending, retrying, or leased.

        A future idle-reserve check is maintenance, not ownership of an urgent
        refresh. Callers may ignore only that delayed generation so a launch
        or promotion can atomically supersede it through the singleton queue.
        Provider backoff and leased work remain blocking.
        """

        timestamp = time.time() if now is None else float(now)
        self.reconcile_expired(now=timestamp)
        type_values = [value.value for value in job_types]
        type_clause = ""
        parameters: list[Any] = [
            _scope(user_scope_id),
            HomeJobStatus.PENDING.value,
            HomeJobStatus.RETRY.value,
            HomeJobStatus.RUNNING.value,
            timestamp,
        ]
        if type_values:
            placeholders = ",".join("?" for _ in type_values)
            type_clause = f" AND job_type IN ({placeholders})"
            parameters.extend(type_values)
        idle_clause = ""
        if ignore_delayed_idle:
            idle_clause = (
                " AND NOT (status IN (?, ?) AND run_after > ? "
                "AND payload_json LIKE ?)"
            )
            parameters.extend(
                [
                    HomeJobStatus.PENDING.value,
                    HomeJobStatus.RETRY.value,
                    timestamp,
                    '%"reason":"idle_reserve_enrichment"%',
                ]
            )
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT 1 FROM home_feed_jobs
                WHERE user_scope_id = ? AND (
                    status IN (?, ?)
                    OR (status = ? AND lease_expires_at > ?)
                )
                {type_clause}
                {idle_clause}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def has_runnable(
        self,
        user_scope_id: str,
        *,
        job_types: tuple[HomeJobType, ...] = (),
        now: float | None = None,
    ) -> bool:
        """Return whether useful work is due now or currently leased.

        Delayed backoff is durable maintenance, not an in-progress feed build.
        """

        timestamp = time.time() if now is None else float(now)
        self.reconcile_expired(now=timestamp)
        type_values = [value.value for value in job_types]
        type_clause = ""
        parameters: list[Any] = [
            _scope(user_scope_id),
            HomeJobStatus.PENDING.value,
            HomeJobStatus.RETRY.value,
            timestamp,
            HomeJobStatus.RUNNING.value,
            timestamp,
        ]
        if type_values:
            placeholders = ",".join("?" for _ in type_values)
            type_clause = f" AND job_type IN ({placeholders})"
            parameters.extend(type_values)
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT 1 FROM home_feed_jobs
                WHERE user_scope_id = ? AND (
                    (status IN (?, ?) AND run_after <= ?)
                    OR (status = ? AND lease_expires_at > ?)
                )
                {type_clause}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return row is not None
        finally:
            connection.close()


class _SnapshotRepository(_Repository):
    table_name = ""
    record_type: type[CandidateSnapshotRecord] | type[RadioSnapshotRecord]

    def put(self, record: CandidateSnapshotRecord | RadioSnapshotRecord) -> None:
        envelope = {
            "snapshot_id": record.snapshot_id,
            "user_scope_id": record.user_scope_id,
            "profile_fingerprint": record.profile_fingerprint,
            "generated_at": float(record.generated_at),
            "expires_at": float(record.expires_at),
            "payload": dict(record.payload),
        }
        digest = _content_hash(envelope)
        encoded = _canonical_json(dict(record.payload))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT content_hash FROM {self.table_name} WHERE snapshot_id = ?",
                [record.snapshot_id],
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"] or "") != digest:
                    raise ImmutableRecordConflict(
                        f"snapshot {record.snapshot_id} was rewritten"
                    )
                connection.rollback()
                return
            connection.execute(
                f"""
                INSERT INTO {self.table_name}(
                    snapshot_id, user_scope_id, profile_fingerprint,
                    generated_at, expires_at, payload_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record.snapshot_id,
                    _scope(record.user_scope_id),
                    record.profile_fingerprint,
                    record.generated_at,
                    record.expires_at,
                    encoded,
                    digest,
                    time.time(),
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, snapshot_id: str) -> CandidateSnapshotRecord | RadioSnapshotRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT * FROM {self.table_name} WHERE snapshot_id = ?",
                [str(snapshot_id or "").strip()],
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            connection.close()

    def latest_fresh(
        self,
        user_scope_id: str,
        *,
        profile_fingerprint: str = "",
        now: float | None = None,
    ) -> CandidateSnapshotRecord | RadioSnapshotRecord | None:
        timestamp = time.time() if now is None else float(now)
        parameters: list[Any] = [_scope(user_scope_id), timestamp, timestamp]
        profile_clause = ""
        if profile_fingerprint:
            profile_clause = " AND profile_fingerprint = ?"
            parameters.append(profile_fingerprint)
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT * FROM {self.table_name}
                WHERE user_scope_id = ? AND generated_at <= ? AND expires_at > ?
                  {profile_clause}
                ORDER BY generated_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            connection.close()

    def _from_row(self, row: sqlite3.Row) -> CandidateSnapshotRecord | RadioSnapshotRecord:
        payload = json.loads(row["payload_json"] or "{}")
        return self.record_type(
            snapshot_id=str(row["snapshot_id"]),
            user_scope_id=str(row["user_scope_id"]),
            profile_fingerprint=str(row["profile_fingerprint"] or ""),
            generated_at=float(row["generated_at"]),
            expires_at=float(row["expires_at"]),
            payload=payload if isinstance(payload, dict) else {},
        )


class CandidateSnapshotRepository(_SnapshotRepository):
    table_name = "home_candidate_snapshots"
    record_type = CandidateSnapshotRecord

    def latest_fresh(
        self,
        user_scope_id: str,
        *,
        profile_fingerprint: str = "",
        now: float | None = None,
    ) -> CandidateSnapshotRecord | None:
        record = super().latest_fresh(
            user_scope_id,
            profile_fingerprint=profile_fingerprint,
            now=now,
        )
        return record if isinstance(record, CandidateSnapshotRecord) else None

    def latest_usable(
        self,
        user_scope_id: str,
        *,
        profile_fingerprint: str = "",
        now: float | None = None,
    ) -> CandidateSnapshotRecord | None:
        """Return verified facts without misrepresenting them as fresh."""

        timestamp = time.time() if now is None else float(now)
        parameters: list[Any] = [_scope(user_scope_id), timestamp]
        profile_clause = ""
        if profile_fingerprint:
            profile_clause = " AND profile_fingerprint = ?"
            parameters.append(profile_fingerprint)
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT * FROM {self.table_name}
                WHERE user_scope_id = ? AND generated_at <= ?
                  {profile_clause}
                ORDER BY generated_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            connection.close()

    def recent(
        self,
        user_scope_id: str,
        *,
        limit: int = 8,
        now: float | None = None,
    ) -> tuple[CandidateSnapshotRecord, ...]:
        """Return bounded durable fragments for one-time profile consolidation."""

        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM {self.table_name}
                WHERE user_scope_id = ? AND generated_at <= ?
                ORDER BY generated_at DESC, snapshot_id DESC
                LIMIT ?
                """,
                [_scope(user_scope_id), timestamp, max(1, min(int(limit), 32))],
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)
        finally:
            connection.close()


class HomeExposureRepository(_Repository):
    """Durable exposure/cooldown ledger for served feed selections."""

    def record(
        self,
        user_scope_id: str,
        entries: Iterable[tuple[str, str, str]],
        *,
        now: float | None = None,
    ) -> int:
        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        normalized = {
            (
                str(row_kind or "").strip(),
                str(item_kind or "").strip(),
                str(identity or "").strip().casefold(),
            )
            for row_kind, item_kind, identity in entries
            if str(row_kind or "").strip()
            and str(item_kind or "").strip()
            and str(identity or "").strip()
        }
        if not normalized:
            return 0
        connection = self._connect()
        try:
            connection.executemany(
                """
                INSERT INTO home_feed_exposures(
                    user_scope_id, row_kind, item_kind, item_identity,
                    exposure_count, last_exposed_at
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_scope_id, row_kind, item_kind, item_identity)
                DO UPDATE SET
                    exposure_count = home_feed_exposures.exposure_count + 1,
                    last_exposed_at = excluded.last_exposed_at
                """,
                [
                    [scope, row_kind, item_kind, identity, timestamp]
                    for row_kind, item_kind, identity in normalized
                ],
            )
            # Bound durable state without erasing recent cooldown evidence.
            connection.execute(
                """
                DELETE FROM home_feed_exposures
                WHERE user_scope_id = ? AND last_exposed_at < ?
                """,
                [scope, timestamp - 30 * 24 * 60 * 60],
            )
            connection.commit()
            return len(normalized)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recent_identities(
        self,
        user_scope_id: str,
        *,
        since: float,
    ) -> set[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT item_identity FROM home_feed_exposures
                WHERE user_scope_id = ? AND last_exposed_at >= ?
                  AND row_kind NOT IN ('last_played', 'frequently_listened')
                """,
                [_scope(user_scope_id), float(since)],
            ).fetchall()
            return {
                str(row["item_identity"] or "").strip()
                for row in rows
                if str(row["item_identity"] or "").strip()
            }
        finally:
            connection.close()

    def recent_artist_identities(
        self,
        user_scope_id: str,
        *,
        since: float,
    ) -> set[str]:
        """Return artist-only exposure evidence for background supply."""

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT item_identity FROM home_feed_exposures
                WHERE user_scope_id = ? AND last_exposed_at >= ?
                  AND item_kind = 'artist'
                  AND row_kind NOT IN ('last_played', 'frequently_listened')
                """,
                [_scope(user_scope_id), float(since)],
            ).fetchall()
            return {
                str(row["item_identity"] or "").strip().casefold()
                for row in rows
                if str(row["item_identity"] or "").strip()
            }
        finally:
            connection.close()

    def recent_radio_artist_identities(
        self,
        user_scope_id: str,
        *,
        since: float,
    ) -> set[str]:
        """Return only artists previously served by Popular Radio.

        Artists merely displayed by another Home row are not listening
        evidence and must not make an otherwise exploratory radio card look
        familiar.  Radio owns its own exposure cooldown.
        """

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT item_identity FROM home_feed_exposures
                WHERE user_scope_id = ? AND last_exposed_at >= ?
                  AND item_kind = 'artist' AND row_kind = 'popular_radio'
                """,
                [_scope(user_scope_id), float(since)],
            ).fetchall()
            return {
                str(row["item_identity"] or "").strip().casefold()
                for row in rows
                if str(row["item_identity"] or "").strip()
            }
        finally:
            connection.close()


class RadioSnapshotRepository(_SnapshotRepository):
    table_name = "home_radio_snapshots"
    record_type = RadioSnapshotRecord

    def latest_usable(
        self,
        user_scope_id: str,
        *,
        profile_fingerprint: str = "",
        now: float | None = None,
    ) -> RadioSnapshotRecord | None:
        """Return the latest verified radio facts, even when refresh is due."""

        timestamp = time.time() if now is None else float(now)
        parameters: list[Any] = [_scope(user_scope_id), timestamp]
        profile_clause = ""
        if profile_fingerprint:
            profile_clause = " AND profile_fingerprint = ?"
            parameters.append(profile_fingerprint)
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT * FROM {self.table_name}
                WHERE user_scope_id = ? AND generated_at <= ? {profile_clause}
                ORDER BY generated_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            connection.close()


class FeedArtifactRepository(_Repository):
    def get_by_supply(
        self,
        user_scope_id: str,
        candidate_snapshot_id: str,
        radio_snapshot_id: str = "",
        *,
        allocation_epoch: int | None = None,
    ) -> FeedArtifactRecord | None:
        connection = self._connect()
        try:
            epoch_clause = ""
            parameters: list[Any] = [
                _scope(user_scope_id),
                str(candidate_snapshot_id or "").strip(),
                str(radio_snapshot_id or "").strip(),
            ]
            if allocation_epoch is not None:
                epoch_clause = " AND allocation_epoch = ?"
                parameters.append(max(int(allocation_epoch), 0))
            row = connection.execute(
                f"""
                SELECT * FROM home_feed_artifacts
                WHERE user_scope_id = ? AND candidate_snapshot_id = ?
                  AND radio_snapshot_id = ?
                  {epoch_clause}
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"] or "{}")
            return FeedArtifactRecord(
                artifact_id=str(row["artifact_id"]),
                user_scope_id=str(row["user_scope_id"]),
                profile_fingerprint=str(row["profile_fingerprint"] or ""),
                candidate_snapshot_id=str(row["candidate_snapshot_id"]),
                radio_snapshot_id=str(row["radio_snapshot_id"] or ""),
                allocation_epoch=int(row["allocation_epoch"] or 0),
                created_at=float(row["created_at"]),
                expires_at=float(row["expires_at"]),
                payload=payload if isinstance(payload, dict) else {},
            )
        finally:
            connection.close()

    def put(self, record: FeedArtifactRecord) -> None:
        envelope = {
            "artifact_id": record.artifact_id,
            "user_scope_id": record.user_scope_id,
            "profile_fingerprint": record.profile_fingerprint,
            "candidate_snapshot_id": record.candidate_snapshot_id,
            "radio_snapshot_id": record.radio_snapshot_id,
            "allocation_epoch": max(int(record.allocation_epoch or 0), 0),
            "created_at": float(record.created_at),
            "expires_at": float(record.expires_at),
            "payload": dict(record.payload),
        }
        digest = _content_hash(envelope)
        encoded = _canonical_json(dict(record.payload))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT user_scope_id, profile_fingerprint
                FROM home_candidate_snapshots WHERE snapshot_id = ?
                """,
                [record.candidate_snapshot_id],
            ).fetchone()
            if candidate is None or str(candidate["user_scope_id"] or "") != record.user_scope_id:
                raise InvalidArtifactReference(
                    "candidate snapshot is missing or belongs to another scope"
                )
            if record.radio_snapshot_id:
                radio = connection.execute(
                    """
                    SELECT user_scope_id FROM home_radio_snapshots WHERE snapshot_id = ?
                    """,
                    [record.radio_snapshot_id],
                ).fetchone()
                if radio is None or str(radio["user_scope_id"] or "") != record.user_scope_id:
                    raise InvalidArtifactReference(
                        "radio snapshot is missing or belongs to another scope"
                    )
            consumed = connection.execute(
                """
                SELECT artifact_id FROM home_feed_artifacts
                WHERE user_scope_id = ? AND candidate_snapshot_id = ?
                  AND radio_snapshot_id = ? AND allocation_epoch = ?
                  AND artifact_id != ?
                LIMIT 1
                """,
                [
                    record.user_scope_id,
                    record.candidate_snapshot_id,
                    record.radio_snapshot_id,
                    max(int(record.allocation_epoch or 0), 0),
                    record.artifact_id,
                ],
            ).fetchone()
            if consumed is not None:
                raise ImmutableRecordConflict(
                    "candidate/radio allocation was already materialized"
                )
            existing = connection.execute(
                "SELECT content_hash FROM home_feed_artifacts WHERE artifact_id = ?",
                [record.artifact_id],
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"] or "") != digest:
                    raise ImmutableRecordConflict(
                        f"artifact {record.artifact_id} was rewritten"
                    )
                connection.rollback()
                return
            if (
                not is_complete_feed_bundle_payload(record.payload)
                or not _feed_payload_accepted(record.payload)
            ):
                raise InvalidArtifactReference(
                    "feed artifact must contain one complete immutable bundle"
                )
            connection.execute(
                """
                INSERT INTO home_feed_artifacts(
                    artifact_id, user_scope_id, profile_fingerprint,
                    candidate_snapshot_id, radio_snapshot_id, allocation_epoch,
                    created_at, expires_at, payload_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record.artifact_id,
                    _scope(record.user_scope_id),
                    record.profile_fingerprint,
                    record.candidate_snapshot_id,
                    record.radio_snapshot_id,
                    max(int(record.allocation_epoch or 0), 0),
                    record.created_at,
                    record.expires_at,
                    encoded,
                    digest,
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, artifact_id: str) -> FeedArtifactRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_artifacts WHERE artifact_id = ?",
                [str(artifact_id or "").strip()],
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"] or "{}")
            return FeedArtifactRecord(
                artifact_id=str(row["artifact_id"]),
                user_scope_id=str(row["user_scope_id"]),
                profile_fingerprint=str(row["profile_fingerprint"] or ""),
                candidate_snapshot_id=str(row["candidate_snapshot_id"]),
                radio_snapshot_id=str(row["radio_snapshot_id"] or ""),
                allocation_epoch=int(row["allocation_epoch"] or 0),
                created_at=float(row["created_at"]),
                expires_at=float(row["expires_at"]),
                payload=payload if isinstance(payload, dict) else {},
            )
        finally:
            connection.close()

    def latest_stale_fallback(
        self,
        user_scope_id: str,
        *,
        now: float | None = None,
    ) -> FeedArtifactRecord | None:
        """Return one bounded, formerly accepted active artifact for display only."""

        scope = _scope(user_scope_id)
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM home_feed_artifacts
                WHERE user_scope_id = ? AND created_at <= ?
                  AND expires_at <= ? AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 8
                """,
                [
                    scope,
                    timestamp,
                    timestamp,
                    timestamp - STALE_ACTIVE_FALLBACK_TTL_SECONDS,
                ],
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or not _feed_payload_accepted(payload):
                    continue
                return FeedArtifactRecord(
                    artifact_id=str(row["artifact_id"]),
                    user_scope_id=str(row["user_scope_id"]),
                    profile_fingerprint=str(row["profile_fingerprint"] or ""),
                    candidate_snapshot_id=str(row["candidate_snapshot_id"]),
                    radio_snapshot_id=str(row["radio_snapshot_id"] or ""),
                    allocation_epoch=int(row["allocation_epoch"] or 0),
                    created_at=float(row["created_at"]),
                    expires_at=float(row["expires_at"]),
                    payload=payload,
                )
            return None
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
        return cls(recommendation_store_connection_factory(server))

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
