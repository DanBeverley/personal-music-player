"""Home schema, immutable inventories/artifacts and exposure records."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Iterable
import hashlib
import json
import sqlite3
import time

from .contracts import (STALE_ACTIVE_FALLBACK_TTL_SECONDS, CandidateSnapshotRecord, FeedArtifactRecord, RadioSnapshotRecord, evaluate_feed_contract)
from .bundles import is_complete_feed_bundle_payload
from ..storage.inventory_codec import decode_inventory_payload, encode_inventory_payload



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
        if "active_since" not in state_columns:
            connection.execute(
                "ALTER TABLE home_feed_state "
                "ADD COLUMN active_since REAL NOT NULL DEFAULT 0"
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


@dataclass(frozen=True)
class SnapshotReference:
    """Scheduling identity only; the consuming supplier/composer loads the body."""

    snapshot_id: str
    user_scope_id: str
    profile_fingerprint: str
    generated_at: float
    expires_at: float

    def is_usable(self, now: float | None = None) -> bool:
        return self.generated_at <= (time.time() if now is None else float(now))


class _SnapshotRepository(_Repository):
    def reference(self, snapshot_id: str) -> SnapshotReference | None:
        return self._reference_query("snapshot_id = ?", [str(snapshot_id or "").strip()])

    def latest_reference(
        self, user_scope_id: str, *, profile_fingerprint: str = "",
        require_fresh: bool = False, now: float | None = None,
    ) -> SnapshotReference | None:
        timestamp = time.time() if now is None else float(now)
        clause = "user_scope_id = ? AND generated_at <= ?"
        parameters: list[Any] = [_scope(user_scope_id), timestamp]
        if profile_fingerprint:
            clause += " AND profile_fingerprint = ?"
            parameters.append(profile_fingerprint)
        if require_fresh:
            clause += " AND expires_at > ?"
            parameters.append(timestamp)
        return self._reference_query(clause, parameters)

    def _reference_query(self, clause: str, parameters: list[Any]) -> SnapshotReference | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT snapshot_id, user_scope_id, profile_fingerprint, generated_at, expires_at "
                f"FROM {self.table_name} WHERE {clause} "
                "ORDER BY generated_at DESC, snapshot_id DESC LIMIT 1", parameters,
            ).fetchone()
            return SnapshotReference(**dict(row)) if row is not None else None
        finally:
            connection.close()

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
        kind = "candidate" if self.table_name == "home_candidate_snapshots" else "radio"
        encoded = _canonical_json(encode_inventory_payload(dict(record.payload), kind))
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

    def latest_usable(
        self,
        user_scope_id: str,
        *,
        profile_fingerprint: str = "",
        now: float | None = None,
    ) -> CandidateSnapshotRecord | RadioSnapshotRecord | None:
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

    def _from_row(self, row: sqlite3.Row) -> CandidateSnapshotRecord | RadioSnapshotRecord:
        kind = "candidate" if self.table_name == "home_candidate_snapshots" else "radio"
        payload = decode_inventory_payload(json.loads(row["payload_json"] or "{}"), kind)
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


    def recent(
        self,
        user_scope_id: str,
        *,
        limit: int = 8,
        now: float | None = None,
    ) -> tuple[CandidateSnapshotRecord, ...]:
        """Return bounded durable fragments for one-time profile consolidation."""

        return tuple(self.iter_recent(user_scope_id, limit=limit, now=now))

    def recent_ids(self, user_scope_id: str, *, limit: int = 12) -> tuple[str, ...]:
        """Read immutable fragment identities without decoding their bodies."""
        connection = self._connect()
        try:
            return tuple(str(row[0]) for row in connection.execute(
                f"SELECT snapshot_id FROM {self.table_name} WHERE user_scope_id = ? AND generated_at <= ? "
                "ORDER BY generated_at DESC, snapshot_id DESC LIMIT ?",
                [_scope(user_scope_id), time.time(), max(1, min(int(limit), 32))],
            ))
        finally:
            connection.close()

    def iter_recent(self, user_scope_id: str, *, limit: int = 8, now: float | None = None):
        """Decode one immutable snapshot at a time instead of retaining all bodies."""
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
            )
            for row in rows:
                yield self._from_row(row)
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


    def recent_track_exposures(self, user_scope_id: str, *, since: float) -> dict[str, float]:
        """Shared Home/queue recommendation exposure; no listening prerequisite."""
        return self.recent_exposures(user_scope_id, since=since, item_kind='track')

    def recent_exposures(self, user_scope_id: str, *, since: float,
                         item_kind: str | None = None) -> dict[str, float]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT item_identity, MAX(last_exposed_at) AS last_at
                   FROM home_feed_exposures WHERE user_scope_id = ?
                   AND (? IS NULL OR item_kind = ?) AND last_exposed_at >= ?
                   AND row_kind NOT IN ('last_played', 'frequently_listened')
                   GROUP BY item_identity""", [_scope(user_scope_id), item_kind, item_kind, float(since)],
            ).fetchall()
            return {str(row['item_identity']): float(row['last_at']) for row in rows}
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



class FeedArtifactRepository(_Repository):
    @staticmethod
    def _from_row(row: sqlite3.Row, payload=None) -> FeedArtifactRecord:
        if payload is None:
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
            return self._from_row(row)
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
            return self._from_row(row)
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
                return self._from_row(row, payload)
            return None
        finally:
            connection.close()
