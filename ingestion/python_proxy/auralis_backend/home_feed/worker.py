from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
import logging
import os
import socket
import threading
import time

from ..contracts import SearchRequest
from ..discovery.signals import build_taste_profile
from .candidate_supply import CandidateSupplyService
from .composer import HomeFeedComposer
from .contracts import (
    QUEUE_TARGET_DEPTH,
    SUPPLY_IDLE_NO_PROGRESS_RECHECK_SECONDS,
    SUPPLY_IDLE_PROGRESS_RECHECK_SECONDS,
    HomeJobRecord,
    HomeJobType,
    home_supply_profile_fingerprint,
    with_home_supply_profile,
)
from .radio_supply import ArtworkSupplyService, RadioSupplyService
from .repositories import HomeRepositorySet, LeaseLost
from .scheduler import HomeJobScheduler


_logger = logging.getLogger(__name__)
HOME_JOB_LEASE_SECONDS = 180.0
HOME_JOB_LEASE_RENEW_SECONDS = 30.0
HOME_ARTWORK_BATCH_LIMIT = 8


class RetryableHomeJob(RuntimeError):
    def __init__(self, message: str, *, delay_seconds: float | None = None) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class HomeWorkerResult:
    claimed: bool
    job_id: str = ""
    job_type: str = ""
    outcome: str = "idle"


HOME_WORKER_LANES: tuple[tuple[str, tuple[HomeJobType, ...]], ...] = (
    ("compose", (HomeJobType.COMPOSE_FEED,)),
    ("candidates", (HomeJobType.REFRESH_CANDIDATES,)),
    ("radio", (HomeJobType.REFRESH_RADIO,)),
    ("artwork", (HomeJobType.REPAIR_ARTWORK,)),
)


class HomeFeedWorker:
    """Durable leased worker for every Home background side effect."""

    def __init__(
        self,
        server: Any,
        *,
        worker_id: str = "",
        job_types: tuple[HomeJobType, ...] = (),
    ) -> None:
        self._server = server
        self._repositories = HomeRepositorySet.from_server(server)
        self._scheduler = HomeJobScheduler(server)
        self._candidate_supply = CandidateSupplyService(server, self._repositories)
        self._radio_supply = RadioSupplyService(server, self._repositories)
        self._artwork_supply = ArtworkSupplyService(server)
        self._composer = HomeFeedComposer(self._repositories)
        self._worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:home-feed"
        )
        self._job_types = tuple(job_types)
        self._handlers: dict[HomeJobType, Callable[[HomeJobRecord], None]] = {
            HomeJobType.REFRESH_CANDIDATES: self._refresh_candidates,
            HomeJobType.REFRESH_RADIO: self._refresh_radio,
            HomeJobType.REPAIR_ARTWORK: self._repair_artwork,
            HomeJobType.COMPOSE_FEED: self._compose_feed,
        }

    def run_once(self) -> HomeWorkerResult:
        job = self._repositories.jobs.claim_next(
            self._worker_id,
            lease_seconds=HOME_JOB_LEASE_SECONDS,
            job_types=self._job_types,
        )
        if job is None:
            return HomeWorkerResult(claimed=False)
        try:
            handler_result = self._run_with_lease_renewal(
                job, self._handlers[job.job_type]
            )
            self._repositories.jobs.complete(job.job_id, job.lease_token)
            if (
                job.job_type is HomeJobType.COMPOSE_FEED
                and bool(getattr(handler_result, "stored", False))
            ):
                try:
                    self._schedule_next_compose_slot(job)
                except Exception:
                    # The completed artifact remains valid. Queue-status/API
                    # reconciliation can safely schedule the missing slot;
                    # never retry a lease that has already been completed.
                    _logger.exception(
                        "[EBB:home-worker] post_compose_replenish_failed job=%s",
                        job.job_id,
                    )
            elif (
                job.job_type
                in {HomeJobType.REFRESH_CANDIDATES, HomeJobType.REFRESH_RADIO}
                and (
                    bool(getattr(handler_result, "expansion_needed", False))
                    or bool(getattr(handler_result, "retryable", False))
                )
            ):
                try:
                    self._schedule_idle_supply_replenishment(job, handler_result)
                except Exception:
                    _logger.exception(
                        "[EBB:home-worker] post_supply_replenish_failed job=%s",
                        job.job_id,
                    )
            return HomeWorkerResult(
                claimed=True,
                job_id=job.job_id,
                job_type=job.job_type.value,
                outcome="succeeded",
            )
        except RetryableHomeJob as exc:
            self._repositories.jobs.retry(
                job.job_id,
                job.lease_token,
                str(exc),
                delay_seconds=exc.delay_seconds,
            )
            return HomeWorkerResult(
                claimed=True,
                job_id=job.job_id,
                job_type=job.job_type.value,
                outcome="retry",
            )
        except LeaseLost:
            raise
        except Exception as exc:
            _logger.exception(
                "[EBB:home-worker] job=%s type=%s failed",
                job.job_id,
                job.job_type.value,
            )
            self._repositories.jobs.retry(
                job.job_id,
                job.lease_token,
                f"{type(exc).__name__}:{str(exc)[:400]}",
            )
            return HomeWorkerResult(
                claimed=True,
                job_id=job.job_id,
                job_type=job.job_type.value,
                outcome="retry",
            )

    def _run_with_lease_renewal(
        self,
        job: HomeJobRecord,
        handler: Callable[[HomeJobRecord], Any],
    ) -> Any:
        stop = threading.Event()
        lease_lost = threading.Event()

        def renew() -> None:
            while not stop.wait(HOME_JOB_LEASE_RENEW_SECONDS):
                try:
                    self._repositories.jobs.renew_lease(
                        job.job_id,
                        job.lease_token,
                        lease_seconds=HOME_JOB_LEASE_SECONDS,
                    )
                except Exception as exc:
                    lease_lost.set()
                    _logger.error(
                        "[EBB:home-worker] job=%s lease_renewal_failed=%s:%s",
                        job.job_id,
                        type(exc).__name__,
                        str(exc)[:200],
                    )
                    return

        heartbeat = threading.Thread(
            target=renew,
            name=f"home-lease:{job.job_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = handler(job)
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)
        if lease_lost.is_set():
            raise LeaseLost("home job lease renewal failed")
        return result

    def run_until_idle(self, *, max_jobs: int = 8) -> list[HomeWorkerResult]:
        output: list[HomeWorkerResult] = []
        for _ in range(max(1, min(int(max_jobs or 1), 32))):
            result = self.run_once()
            if not result.claimed:
                break
            output.append(result)
        return output

    @staticmethod
    def _request(job: HomeJobRecord) -> SearchRequest:
        payload = dict(job.payload or {})
        request = dict(payload.get("request") or {})
        request["user_scope_id"] = job.user_scope_id
        return SearchRequest(**request)

    def _refresh_candidates(self, job: HomeJobRecord) -> Any:
        started = time.perf_counter()
        request = self._request(job)
        taste = build_taste_profile(self._server, request)
        taste = with_home_supply_profile(taste)
        reason = str((job.payload or {}).get("reason") or "candidate_refresh")
        outcome = self._candidate_supply.refresh(
            taste,
            force=reason in {
                "candidate_inventory_refresh",
                "candidate_revalidation_due",
                "candidate_reserve_replenish",
                "inventory_replenish",
                "signal_reconcile",
                "release_metadata_replenish",
                "idle_reserve_enrichment",
            },
        )
        print(
            "[EBB:home-worker] "
            f"type=candidates scope={job.user_scope_id} ready={outcome.ready} "
            f"candidates={outcome.candidate_count} scheduled={outcome.scheduled_requests} "
            f"progress={outcome.made_progress} expansion={outcome.expansion_needed} "
            f"outcome={outcome.reason} "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)}",
            flush=True,
        )
        if not outcome.ready:
            if job.attempts < job.max_attempts:
                raise RetryableHomeJob(
                    outcome.reason,
                    delay_seconds=(
                        outcome.retry_after_seconds
                        if not outcome.made_progress
                        else None
                    ),
                )
            return outcome
        state = self._repositories.state.reconcile(job.user_scope_id)
        current_artifacts = [
            self._repositories.artifacts.get(artifact_id)
            for artifact_id in (
                ([state.active_artifact_id] if state.active_artifact_id else [])
                + list(state.ready_artifact_ids)
            )
        ]
        already_materialized = any(
            record is not None
            and record.candidate_snapshot_id == outcome.snapshot_id
            for record in current_artifacts
        )
        if not outcome.made_progress and already_materialized:
            # A maintenance attempt that added no usable identities cannot
            # create another supply revision. Existing supply is allocated by
            # compose epochs, so do not restart radio/provider work here. A
            # bounded retry may continue deficient acquisition, after which
            # the idle scheduler owns the next check.
            if outcome.expansion_needed and job.attempts < job.max_attempts:
                raise RetryableHomeJob(
                    "candidate_reserve_replenish",
                    delay_seconds=(
                        outcome.retry_after_seconds
                        or SUPPLY_IDLE_NO_PROGRESS_RECHECK_SECONDS
                    ),
                )
            return outcome
        radio = self._repositories.radio.latest_usable(
            job.user_scope_id,
            profile_fingerprint=taste.profile_key,
        ) or self._repositories.radio.latest_usable(job.user_scope_id)
        # Every ready immutable candidate revision is handed off once. Exact
        # supply-pair coalescing makes this safe, while avoiding the previous
        # stranded state where consolidation reported no provider progress.
        self._scheduler.radio(
            request,
            reason="candidate_snapshot_ready",
            profile_fingerprint=taste.profile_key,
            candidate_snapshot_id=outcome.snapshot_id,
            generation_hint=outcome.snapshot_id,
        )
        # General feed composition is independent of optional radio/album
        # enrichment.  Compose as soon as the candidate snapshot is ready;
        # the composer emits complete core rows and simply omits an optional
        # row until its persisted supply is available.
        if len(
            self._repositories.state.reconcile(
                job.user_scope_id
            ).ready_artifact_ids
        ) < QUEUE_TARGET_DEPTH:
            self._scheduler.compose(
                request,
                reason="candidate_snapshot_ready",
                profile_fingerprint=taste.profile_key,
                candidate_snapshot_id=outcome.snapshot_id,
                radio_snapshot_id=(radio.snapshot_id if radio is not None else ""),
            )
        # Readiness publishes immediately, while this same bounded durable job
        # continues filling deficient reserve partitions. The final attempt
        # always completes so optional expansion can never become an infinite
        # feed-preparation loop.
        if outcome.expansion_needed and job.attempts < job.max_attempts:
            raise RetryableHomeJob(
                "candidate_reserve_replenish",
                delay_seconds=(
                    0.0 if outcome.made_progress else outcome.retry_after_seconds
                ),
            )
        return outcome

    def _refresh_radio(self, job: HomeJobRecord) -> Any:
        started = time.perf_counter()
        request = self._request(job)
        taste = with_home_supply_profile(
            build_taste_profile(self._server, request)
        )
        candidate_snapshot_id = str(
            (job.payload or {}).get("candidate_snapshot_id") or ""
        )
        candidate_record = (
            self._repositories.candidates.get(candidate_snapshot_id)
            if candidate_snapshot_id
            else None
        )
        if candidate_record is None:
            candidate_record = self._repositories.candidates.latest_usable(
                job.user_scope_id,
                profile_fingerprint=home_supply_profile_fingerprint(
                    job.user_scope_id
                ),
            )
            candidate_snapshot_id = (
                candidate_record.snapshot_id if candidate_record is not None else ""
            )
        if (
            candidate_record is not None
            and candidate_record.profile_fingerprint != taste.profile_key
        ):
            stable_candidate = self._repositories.candidates.latest_usable(
                job.user_scope_id,
                profile_fingerprint=taste.profile_key,
            )
            if stable_candidate is None:
                self._scheduler.candidates(
                    request,
                    reason="candidate_inventory_refresh",
                    profile_fingerprint=taste.profile_key,
                    generation_hint="stable-profile-cutover",
                )
                return
            candidate_record = stable_candidate
            candidate_snapshot_id = stable_candidate.snapshot_id
        outcome = self._radio_supply.refresh(
            taste,
            candidate_snapshot_id=candidate_snapshot_id,
        )
        print(
            "[EBB:home-worker] "
            f"type=radio scope={job.user_scope_id} ready={outcome.ready} "
            f"reservoir={outcome.reservoir_count} discovery={outcome.discovery_count} "
            f"scheduled={outcome.scheduled_requests} progress={outcome.made_progress} "
            f"seeds={outcome.seed_candidates_offered} "
            f"verified={outcome.source_verification_verified} "
            f"new_cards={outcome.new_card_count} "
            f"outcome={outcome.reason} "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)}",
            flush=True,
        )
        for record in tuple(outcome.artwork_records)[:HOME_ARTWORK_BATCH_LIMIT]:
            self._scheduler.artwork(
                request,
                record,
                profile_fingerprint=taste.profile_key,
            )
        if outcome.ready and candidate_snapshot_id:
            existing_pair = self._repositories.artifacts.get_by_supply(
                job.user_scope_id,
                candidate_snapshot_id,
                outcome.snapshot_id,
            )
            if not outcome.made_progress and existing_pair is not None:
                if outcome.expansion_needed and job.attempts < job.max_attempts:
                    raise RetryableHomeJob(
                        "radio_reserve_replenish",
                        delay_seconds=SUPPLY_IDLE_NO_PROGRESS_RECHECK_SECONDS,
                    )
                return outcome
            if len(
                self._repositories.state.reconcile(
                    job.user_scope_id
                ).ready_artifact_ids
            ) < QUEUE_TARGET_DEPTH:
                self._scheduler.compose(
                    request,
                    reason="radio_snapshot_ready",
                    profile_fingerprint=taste.profile_key,
                    candidate_snapshot_id=candidate_snapshot_id,
                    radio_snapshot_id=outcome.snapshot_id,
                )
            if outcome.expansion_needed and job.attempts < job.max_attempts:
                raise RetryableHomeJob(
                    "radio_reserve_replenish",
                    delay_seconds=(0.0 if outcome.made_progress else 300.0),
                )
            return outcome
        if outcome.artwork_records:
            return outcome
        if job.attempts < job.max_attempts:
            raise RetryableHomeJob(outcome.reason)
        return outcome

    def _schedule_idle_supply_replenishment(
        self,
        job: HomeJobRecord,
        outcome: Any,
    ) -> None:
        """Keep deficient reserves warm without an unbounded request loop."""

        made_progress = bool(getattr(outcome, "made_progress", False))
        delay = (
            SUPPLY_IDLE_PROGRESS_RECHECK_SECONDS
            if made_progress
            else max(
                int(getattr(outcome, "retry_after_seconds", 0) or 0),
                SUPPLY_IDLE_NO_PROGRESS_RECHECK_SECONDS,
            )
        )
        run_after = time.time() + delay
        snapshot_id = str(getattr(outcome, "snapshot_id", "") or "").strip()
        generation = f"idle:{snapshot_id or job.job_id}:{int(run_after)}"
        request = self._request(job)
        profile = home_supply_profile_fingerprint(job.user_scope_id)
        if job.job_type is HomeJobType.REFRESH_CANDIDATES:
            self._scheduler.candidates(
                request,
                reason="idle_reserve_enrichment",
                profile_fingerprint=profile,
                generation_hint=generation,
                run_after=run_after,
            )
            return
        candidate = self._repositories.candidates.latest_usable(
            job.user_scope_id,
            profile_fingerprint=profile,
        )
        if candidate is None:
            return
        self._scheduler.radio(
            request,
            reason="idle_reserve_enrichment",
            profile_fingerprint=profile,
            candidate_snapshot_id=candidate.snapshot_id,
            generation_hint=generation,
            run_after=run_after,
        )

    def _repair_artwork(self, job: HomeJobRecord) -> None:
        request = self._request(job)
        taste = build_taste_profile(self._server, request)
        record = dict((job.payload or {}).get("artist") or {})
        if not self._artwork_supply.repair(record):
            # Artwork cannot block radio acquisition. Keep its own durable,
            # slow retry while the radio supplier immediately advances to the
            # next seed.
            raise RetryableHomeJob(
                "artist_artwork_unavailable",
                delay_seconds=15 * 60,
            )
        # Only a persisted artwork change may trigger radio reconciliation.
        # Coalescing includes terminal jobs, so dozens of artist repairs in
        # one batch cannot manufacture an endless chain of radio work.
        candidate = self._repositories.candidates.latest_usable(
            job.user_scope_id,
            profile_fingerprint=home_supply_profile_fingerprint(
                job.user_scope_id
            ),
        )
        self._scheduler.radio(
            request,
            reason="radio_artwork_reconcile",
            profile_fingerprint=(
                candidate.profile_fingerprint
                if candidate is not None
                else taste.profile_key
            ),
            candidate_snapshot_id=(
                candidate.snapshot_id if candidate is not None else ""
            ),
            generation_hint=f"artwork-batch:{int(time.time() // 600)}",
            coalesce_terminal=True,
        )

    def _compose_feed(self, job: HomeJobRecord) -> Any:
        started = time.perf_counter()
        request = self._request(job)
        taste = build_taste_profile(self._server, request)
        requested_candidate_snapshot_id = str(
            (job.payload or {}).get("candidate_snapshot_id") or ""
        )
        requested_radio_snapshot_id = str(
            (job.payload or {}).get("radio_snapshot_id") or ""
        )
        allocation_epoch = max(
            int((job.payload or {}).get("allocation_epoch") or 0),
            0,
        )
        stable_profile = home_supply_profile_fingerprint(job.user_scope_id)
        # The slot job is demand-owned. Consume the newest immutable supply
        # available when it actually runs rather than the snapshot that first
        # noticed the missing slot.
        candidate_record = (
            self._repositories.candidates.latest_fresh(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            or self._repositories.candidates.latest_usable(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            or self._repositories.candidates.get(requested_candidate_snapshot_id)
        )
        if (
            candidate_record is not None
            and candidate_record.profile_fingerprint != stable_profile
        ):
            # Drain pre-cutover compose jobs without allowing them to publish
            # another legacy-profile feed.
            self._scheduler.candidates(
                request,
                reason="candidate_inventory_refresh",
                profile_fingerprint=stable_profile,
                generation_hint="legacy-compose-cutover",
            )
            return None
        candidate_snapshot_id = (
            candidate_record.snapshot_id if candidate_record is not None else ""
        )
        radio_record = (
            self._repositories.radio.latest_fresh(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            or self._repositories.radio.latest_usable(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            or self._repositories.radio.get(requested_radio_snapshot_id)
        )
        radio_snapshot_id = (
            radio_record.snapshot_id if radio_record is not None else ""
        )
        outcome = self._composer.compose(
            taste,
            reason=str((job.payload or {}).get("reason") or "background_prepare"),
            candidate_snapshot_id=candidate_snapshot_id,
            radio_snapshot_id=radio_snapshot_id,
            allocation_epoch=allocation_epoch,
        )
        print(
            "[EBB:home-worker] "
            f"type=compose scope={job.user_scope_id} stored={outcome.stored} "
            f"artifact={outcome.artifact_id or '-'} outcome={outcome.reason} "
            f"failures={','.join(outcome.failed_contracts) or '-'} "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)}",
            flush=True,
        )
        if outcome.reason in {
            "candidate_snapshot_missing",
            "candidate_snapshot_invalid",
        }:
            # The slot consumes the latest stable-profile generation. Never
            # fall back to a legacy-profile payload merely because it was the
            # first request that noticed the missing queue slot.
            if (
                self._repositories.candidates.latest_usable(
                    job.user_scope_id,
                    profile_fingerprint=stable_profile,
                )
                is not None
            ):
                return outcome
            self._scheduler.candidates(
                request,
                reason="inventory_replenish",
                profile_fingerprint=taste.profile_key,
                generation_hint=f"compose-missing:{job.job_id}",
            )
            return outcome
        if outcome.reason == "artifact_contract_rejected":
            failures = tuple(outcome.failed_contracts or ())
            needs_radio = any(
                "popular_radio" in value or "radio_" in value
                for value in failures
            )
            needs_candidates = any(
                row in value
                for value in failures
                for row in ("featured_new_albums", "recommended_albums")
            )
            if needs_radio and outcome.candidate_snapshot_id:
                candidate = self._repositories.candidates.get(
                    outcome.candidate_snapshot_id
                )
                self._scheduler.radio(
                    request,
                    reason="required_radio_row_missing",
                    profile_fingerprint=(
                        candidate.profile_fingerprint if candidate is not None else ""
                    ),
                    candidate_snapshot_id=outcome.candidate_snapshot_id,
                    generation_hint=f"required:{outcome.candidate_snapshot_id}",
                )
            if needs_candidates:
                candidate = self._repositories.candidates.get(
                    outcome.candidate_snapshot_id
                )
                self._scheduler.candidates(
                    request,
                    reason="inventory_replenish",
                    profile_fingerprint=(
                        candidate.profile_fingerprint
                        if candidate is not None
                        else taste.profile_key
                    ),
                    generation_hint=f"compose-shortage:{job.job_id}",
                )
            return outcome
        if outcome.reason == "supply_pair_consumed":
            return outcome
        if outcome.reason in {"artifact_unchanged", "artifact_too_similar"}:
            latest_candidate = (
                self._repositories.candidates.latest_fresh(
                    job.user_scope_id,
                    profile_fingerprint=stable_profile,
                )
                or self._repositories.candidates.latest_usable(
                    job.user_scope_id,
                    profile_fingerprint=stable_profile,
                )
            )
            latest_radio = self._repositories.radio.latest_usable(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            if (
                latest_candidate is not None
                and (
                    latest_candidate.snapshot_id != outcome.candidate_snapshot_id
                    or (
                        latest_radio.snapshot_id if latest_radio is not None else ""
                    )
                    != outcome.radio_snapshot_id
                )
            ):
                # Supply advanced while this slot was composing. Retry this
                # same durable job once with the newer immutable inputs rather
                # than enqueueing another compose generation.
                raise RetryableHomeJob("compose_supply_advanced", delay_seconds=0.0)
            # The newest supply genuinely cannot form a different successor.
            # Wait for background supply progress; do not spin composition.
            return outcome
        if outcome.reason == "state_commit_conflict":
            raise RetryableHomeJob(outcome.reason)
        return outcome

    def _schedule_next_compose_slot(self, job: HomeJobRecord) -> None:
        """Replenish only after the current compose lease is terminal."""

        state = self._repositories.state.reconcile(job.user_scope_id)
        if len(state.ready_artifact_ids) >= QUEUE_TARGET_DEPTH:
            return
        stable_profile = home_supply_profile_fingerprint(job.user_scope_id)
        candidate = (
            self._repositories.candidates.latest_fresh(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
            or self._repositories.candidates.latest_usable(
                job.user_scope_id,
                profile_fingerprint=stable_profile,
            )
        )
        candidate_snapshot_id = (
            candidate.snapshot_id
            if candidate is not None
            else str((job.payload or {}).get("candidate_snapshot_id") or "").strip()
        )
        if not candidate_snapshot_id:
            return
        radio = self._repositories.radio.latest_usable(
            job.user_scope_id,
            profile_fingerprint=stable_profile,
        )
        self._scheduler.compose(
            self._request(job),
            reason="ready_queue_next_allocation",
            profile_fingerprint=stable_profile,
            candidate_snapshot_id=candidate_snapshot_id,
            radio_snapshot_id=(
                radio.snapshot_id
                if radio is not None
                else str((job.payload or {}).get("radio_snapshot_id") or "").strip()
            ),
            allocation_epoch=(
                state.active_version + len(state.ready_artifact_ids) + 1
            ),
        )


def run_home_feed_lanes(
    server: Any,
    *,
    max_jobs: int = 8,
    worker_id_prefix: str = "",
) -> list[HomeWorkerResult]:
    """Run independent Home lanes concurrently with one owner per lane.

    Provider work may be slow, but it can no longer hold the local composer or
    artwork repair behind it. Each account remains serialized within a lane by
    the durable repository lease.
    """

    remaining = max(1, min(int(max_jobs or 1), 32))
    prefix = worker_id_prefix or f"{socket.gethostname()}:{os.getpid()}:home-feed"
    workers = [
        HomeFeedWorker(
            server,
            worker_id=f"{prefix}:{lane}",
            job_types=job_types,
        )
        for lane, job_types in HOME_WORKER_LANES
    ]
    output: list[HomeWorkerResult] = []
    with ThreadPoolExecutor(
        max_workers=len(workers),
        thread_name_prefix="home-feed-lane",
    ) as executor:
        while remaining > 0:
            # Always inspect every lane. Truncating the final round to the
            # first N lanes could starve candidate/radio work whenever the
            # earlier compose lane happened to be idle.
            results = list(executor.map(lambda worker: worker.run_once(), workers))
            claimed = [result for result in results if result.claimed]
            if not claimed:
                break
            output.extend(claimed)
            remaining -= len(claimed)
    return output
