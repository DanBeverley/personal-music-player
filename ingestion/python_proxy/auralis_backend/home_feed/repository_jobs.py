"""Durable Home job admission, worker leases and bounded retry policy."""
from __future__ import annotations

from collections.abc import (Mapping)
from typing import (Any)
import json
import sqlite3
import time
import uuid

from .contracts import (HomeJobRecord, HomeJobStatus, HomeJobType, retry_delay_seconds)



from .repository_records import _Repository, _scope, _canonical_json, ImmutableRecordConflict, HomeRepositoryConflict, LeaseLost

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
    @staticmethod
    def _insert_job(connection, scope, job_type, generation, encoded_payload,
                    priority, max_attempts, due_at, timestamp):
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
        return job_id

    @staticmethod
    def _fail_exhausted_leases(connection, timestamp):
        return connection.execute(
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
        expedite: bool = False,
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
                    or expedite
                ):
                    if expedite:
                        # Promote existing demand without duplicating work or
                        # changing a running lease, attempts or provider clocks.
                        connection.execute(
                            "UPDATE home_feed_jobs SET max_attempts = CASE "
                            "WHEN priority < ? AND status != 'running' THEN MAX(max_attempts, attempts + ?) "
                            "ELSE max_attempts END, priority = MAX(priority, ?), "
                            "run_after = CASE WHEN status = 'running' THEN run_after "
                            "ELSE MIN(run_after, ?) END, payload_json = CASE "
                            "WHEN status = 'running' THEN payload_json ELSE ? END, "
                            "updated_at = ? WHERE job_id = ?",
                            [int(priority), int(max_attempts), int(priority), due_at,
                             encoded_payload, timestamp, existing_job.job_id],
                        )
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
                    return self.get(existing_job.job_id) if expedite else existing_job
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
            job_id = self._insert_job(
                connection, scope, job_type, generation, encoded_payload,
                priority, max_attempts, due_at, timestamp)
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
            # Supply work is singleton per account even when idle callers want
            # terminal generations coalesced. Artwork remains per identity.
            enqueue = (self.enqueue_singleton if job_type in {
                HomeJobType.REFRESH_CANDIDATES, HomeJobType.REFRESH_RADIO,
                HomeJobType.RECONCILE_SUPPLY,
            } else self.enqueue)
            return enqueue(scope, job_type, generation_key, payload=payload,
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
            supplies = connection.execute(
                "SELECT job_id,user_scope_id,job_type,status FROM home_feed_jobs "
                "WHERE job_type IN (?,?) AND status IN (?,?,?) "
                "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, "
                "priority DESC, created_at DESC, job_id DESC",
                [HomeJobType.REFRESH_CANDIDATES.value, HomeJobType.REFRESH_RADIO.value,
                 HomeJobStatus.PENDING.value, HomeJobStatus.RETRY.value, HomeJobStatus.RUNNING.value],
            ).fetchall()
            owners: set[tuple[str, str]] = set()
            for row in supplies:
                key = (row['user_scope_id'], row['job_type'])
                if key not in owners or row['status'] == HomeJobStatus.RUNNING.value:
                    owners.add(key)
                    continue
                cancelled = connection.execute(
                    "UPDATE home_feed_jobs SET status=?,updated_at=?,last_error=? "
                    "WHERE job_id=? AND status IN (?,?)",
                    [HomeJobStatus.CANCELLED.value, timestamp, 'superseded_duplicate_supply',
                     row['job_id'], HomeJobStatus.PENDING.value, HomeJobStatus.RETRY.value],
                )
                changed += max(int(cancelled.rowcount or 0), 0)
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
        connection: sqlite3.Connection | None = None,
    ) -> HomeJobRecord:
        scope = _scope(user_scope_id)
        generation = str(generation_key or "").strip()
        if not generation:
            raise ValueError("generation_key is required")
        timestamp = time.time() if now is None else float(now)
        due_at = timestamp if run_after is None else float(run_after)
        encoded_payload = _canonical_json(dict(payload or {}))
        owns_connection = connection is None
        connection = connection if connection is not None else self._connect()
        try:
            if owns_connection:
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
                if owns_connection:
                    connection.rollback()
                return _job_from_row(existing)
            job_id = self._insert_job(
                connection, scope, job_type, generation, encoded_payload,
                priority, max_attempts, due_at, timestamp)
            if owns_connection:
                connection.commit()
            return _job_from_row(connection.execute(
                "SELECT * FROM home_feed_jobs WHERE job_id = ?", [job_id]
            ).fetchone())
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
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
            # Empty lane polling is a read. Acquire the writer only when there
            # is due work/lease recovery; the transaction below still owns the
            # authoritative claim and all race/attempt checks.
            values = [HomeJobStatus.PENDING.value, HomeJobStatus.RETRY.value,
                      timestamp, HomeJobStatus.RUNNING.value, timestamp]
            lane_clause = ""
            if job_types:
                lane_clause = " AND job_type IN (" + ",".join("?" for _ in job_types) + ")"
                values.extend(kind.value for kind in job_types)
            due = connection.execute(
                "SELECT 1 FROM home_feed_jobs WHERE ((status IN (?, ?) AND run_after <= ?) "
                "OR (status = ? AND lease_expires_at <= ?))" + lane_clause + " LIMIT 1",
                values,
            ).fetchone()
            if due is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            self._fail_exhausted_leases(connection, timestamp)
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
        payload: Mapping[str, Any] | None = None,
        preserve_attempt: bool = False,
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
            attempts = max(int(row["attempts"] or 0) - int(preserve_attempt), 0)
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
                    lease_expires_at = 0, last_error = ?, updated_at = ?, attempts = ?,
                    payload_json = COALESCE(?, payload_json)
                WHERE job_id = ? AND lease_token = ?
                """,
                [status.value, run_after, str(error or "")[:1000], timestamp, attempts,
                 json.dumps(payload, separators=(",", ":")) if payload is not None else None,
                 job_id, lease_token],
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

    def latest_for_type(self, user_scope_id: str, job_type: HomeJobType) -> HomeJobRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM home_feed_jobs WHERE user_scope_id = ? AND job_type = ? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                [_scope(user_scope_id), job_type.value],
            ).fetchone()
            return _job_from_row(row) if row is not None else None
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
            terminal = self._fail_exhausted_leases(connection, timestamp)
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
                    OR (status = ? AND (lease_expires_at > ? OR attempts < max_attempts))
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
                    OR (status = ? AND (lease_expires_at > ? OR attempts < max_attempts))
                )
                {type_clause}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return row is not None
        finally:
            connection.close()
