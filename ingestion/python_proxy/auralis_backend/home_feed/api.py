from __future__ import annotations

from typing import Any, Mapping
from collections import OrderedDict
import threading
import time
import uuid

from fastapi import HTTPException

from ..discovery.adapters import (
    artifact_to_session,
    home_response_from_artifact,
    row_page_response_from_artifact,
)
from .composer import artifact_exposure_entries, artifact_from_record, _attach_inventory_portraits
from .bundles import is_complete_feed_bundle_payload
from .contracts import (
    HomeJobType,
    HomeJobStatus,
    HomeStateRecord,
    QUEUE_TARGET_DEPTH,
    home_supply_profile_fingerprint,
)
from .repositories import HomeRepositorySet, InvalidArtifactReference, StaleRevision
from .scheduler import HomeJobScheduler


def _home_stage(trace, name, started):
    if isinstance(trace, dict):
        stages = trace.setdefault('stage_ms', {})
        key = 'recommend.' + name
        stages[key] = stages.get(key, 0) + int((time.perf_counter()-started)*1000)


class HomeFeedApi:
    """Serve, promote, and report Home state exclusively from durable repositories."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._repositories = HomeRepositorySet.from_server(server)
        self._scheduler = HomeJobScheduler(server)
        self._impressed_artifact_ids: set[str] = set()
        self._impression_lock = threading.Lock()
        self._replenishment_lock = threading.Lock()
        self._replenishing_scopes: set[str] = set()
        self._replenished: OrderedDict[tuple[str, int], float] = OrderedDict()

    def recommend(
        self,
        request: Any,
        *,
        request_mode: str,
        trace: Mapping[str, Any] | None = None,
        background_tasks: Any = None,
    ) -> dict[str, Any]:
        request_id = str((trace or {}).get("request_id") or uuid.uuid4())
        scope = str(getattr(request, "user_scope_id", "") or "guest").strip() or "guest"
        if request_mode == "row_page":
            return self._row_page(request, scope=scope, request_id=request_id)
        if request_mode == "queue_status" or bool(
            getattr(request, "feed_queue_status_only", False)
        ):
            return self._queue_status(request, scope=scope, request_id=request_id)

        reconcile_started = time.perf_counter()
        state = self._repositories.state.reconcile(scope)
        _home_stage(trace, 'state_reconcile', reconcile_started)
        if request_mode == "background_prepare":
            self._replenish_after_delivery(request, state, reason="background_prepare",
                                          background_tasks=background_tasks)
            diagnostics = {"engine": "discovery_engine", "feed_action": "background_prepare",
                           **self._queue_diagnostics(state)}
            return {"status": "success", "request_id": request_id,
                    "session_id": state.active_artifact_id, "feed_version": state.active_version,
                    "feed_action": "background_prepare", "rows": [], "has_more": False,
                    "next_offset": 0, "preparation_state": diagnostics['preparation_state'],
                    "diagnostics": diagnostics}

        force = bool(
            getattr(request, "force_refresh", False)
            or getattr(request, "prefer_fresh_rows", False)
        )
        session_intent = bool(getattr(request, "session_intent", False))
        launch = bool(getattr(request, "promote_ready_on_launch", False))
        promoted = ""
        action = "served_active"
        refresh_from_version = getattr(request, 'refresh_from_version', None)
        asynchronous_refresh = force and refresh_from_version is not None
        if asynchronous_refresh and state.active_version != refresh_from_version:
            return self._serve_or_prepare(
                request, state, request_id=request_id, action='served_active',
                reason='refresh_version_advanced', trace=trace,
            )
        promotion_started = time.perf_counter()

        if force and state.ready_artifact_ids:
            state, promoted = self._promote(state,
                launch_token=str(getattr(request, 'refresh_token', '') or ''),
                expected_active_version=refresh_from_version)
            action = "promoted_prepared" if promoted else "unchanged_no_rotation"
        elif launch and not session_intent and state.ready_artifact_ids:
            launch_token = str(getattr(request, "launch_token", "") or "").strip()
            if not launch_token or launch_token != state.last_launch_token:
                state, promoted = self._promote(
                    state, launch_token=launch_token, minimum_active_age=30 * 60
                )
                action = "promoted_prepared" if promoted else "served_active"
        elif not state.active_artifact_id and state.ready_artifact_ids:
            state, promoted = self._promote(state)
            action = "promoted_prepared" if promoted else "preparing_initial"

        _home_stage(trace, 'promotion', promotion_started)
        if promoted:
            self._replenish_after_delivery(request, state, reason="post_promotion",
                                          background_tasks=background_tasks)
            return self._serve_or_prepare(
                request,
                state,
                request_id=request_id,
                action=action,
                changed=True,
                trace=trace,
            )

        if force and state.active_artifact_id and not state.ready_artifact_ids:
            # New clients retain the user's intent and follow metadata status.
            # Preparation uses the existing post-response owner, not a second
            # job or an eight-second blocking request on every poll.
            if not asynchronous_refresh:
                self._ensure_successors(request, state, reason="pull_to_refresh")
            wait_ms = 0 if asynchronous_refresh else self._refresh_wait_ms(request)
            deadline = time.monotonic() + wait_ms / 1000.0
            while wait_ms > 0 and time.monotonic() < deadline:
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))
                state = self._repositories.state.reconcile(scope)
                if state.ready_artifact_ids:
                    state, promoted = self._promote(state)
                    if promoted:
                        self._replenish_after_delivery(request, state, reason="post_promotion",
                                                      background_tasks=background_tasks)
                        return self._serve_or_prepare(
                            request,
                            state,
                            request_id=request_id,
                            action="promoted_prepared",
                            changed=True,
                            reason="refresh_wait_promoted",
                            trace=trace,
                        )
            action = "unchanged_no_rotation"

        self._replenish_after_delivery(
            request,
            state,
            reason=('pull_to_refresh' if asynchronous_refresh else
                    "launch_missing_successor" if launch else "serve_replenish"),
            background_tasks=background_tasks,
        )
        return self._serve_or_prepare(
            request,
            state,
            request_id=request_id,
            action=action,
            changed=False,
            reason=('refresh_preparing' if asynchronous_refresh else
                    "refresh_preparing_timeout" if force else ""),
            trace=trace,
        )

    def _replenish_after_delivery(
        self, request: Any, state: HomeStateRecord, *, reason: str,
        background_tasks: Any = None,
    ) -> None:
        if background_tasks is None:
            self._ensure_successors(request, state, reason=reason)
            return
        if not state.active_artifact_id and not state.ready_artifact_ids:
            # A fresh guest still gets durable acquisition before delivery,
            # but inventory decoding and supply reconciliation must not hold
            # its first HTTP response. Existing jobs remain the only owner.
            if not self._repositories.jobs.has_active(
                state.user_scope_id,
                job_types=(HomeJobType.REFRESH_CANDIDATES, HomeJobType.REFRESH_RADIO,
                           HomeJobType.COMPOSE_FEED), ignore_delayed_idle=True,
            ):
                self._scheduler.candidates(
                    request, reason="candidate_inventory_refresh",
                    profile_fingerprint=home_supply_profile_fingerprint(state.user_scope_id),
                )
        background_tasks.add_task(self._replenish_delivered, request, state.user_scope_id, reason)

    def _replenish_delivered(self, request: Any, scope: str, reason: str) -> None:
        with self._replenishment_lock:
            if scope in self._replenishing_scopes:
                return
            self._replenishing_scopes.add(scope)
        try:
            state = self._repositories.state.reconcile(scope)
            key = (scope, state.revision)
            with self._replenishment_lock:
                if time.monotonic() - self._replenished.get(key, float('-inf')) < 30:
                    return
            completed = self._ensure_successors(request, state, reason=reason)
            if completed:
                with self._replenishment_lock:
                    self._replenished[key] = time.monotonic()
                    self._replenished.move_to_end(key)
                    while len(self._replenished) > 128:
                        self._replenished.popitem(last=False)
        except Exception:
            # Existing durable jobs and the next request can recover this work.
            # Never include profile data or external error text in this warning.
            print("[home] delivered_feed_replenishment_failed", flush=True)
        finally:
            with self._replenishment_lock:
                self._replenishing_scopes.discard(scope)

    def _ensure_successors(
        self, request: Any, state: HomeStateRecord, *, reason: str,
    ) -> bool:
        # Inventory decoding belongs to the durable worker, never the API.
        self._scheduler.reconcile_supply(request, state=state, reason=reason)
        return True

    def _promote(
        self,
        state: HomeStateRecord,
        *,
        launch_token: str = "",
        minimum_active_age: float = 0.0,
        expected_active_version: int | None = None,
    ) -> tuple[HomeStateRecord, str]:
        try:
            return self._repositories.state.promote_ready(
                state.user_scope_id,
                expected_revision=state.revision,
                launch_token=launch_token,
                minimum_active_age=minimum_active_age,
                expected_active_version=expected_active_version,
            )
        except (InvalidArtifactReference, StaleRevision):
            return self._repositories.state.reconcile(state.user_scope_id), ""

    def _queue_diagnostics(self, state: HomeStateRecord) -> dict[str, Any]:
        active_jobs = self._repositories.jobs.has_runnable(
            state.user_scope_id,
            job_types=(
                HomeJobType.REFRESH_CANDIDATES,
                HomeJobType.REFRESH_RADIO,
                HomeJobType.COMPOSE_FEED,
                HomeJobType.RECONCILE_SUPPLY,
            ),
        )
        # Reconciliation/promotion already validate these immutable references.
        # Status must not decode the same full bundles a second time.
        complete_bundle_ids = list(state.ready_artifact_ids)
        depth = len(complete_bundle_ids)
        compose_active = self._repositories.jobs.has_runnable(
            state.user_scope_id, job_types=(HomeJobType.COMPOSE_FEED,))
        latest_compose = self._repositories.jobs.latest_for_type(
            state.user_scope_id, HomeJobType.COMPOSE_FEED)
        blocked_reason = ""
        if (latest_compose is not None and latest_compose.status is HomeJobStatus.FAILED
                and latest_compose.updated_at >= state.active_since):
            code = latest_compose.last_error.split(":", 1)[0]
            if code in {"artifact_too_similar", "artifact_unchanged", "artifact_contract_rejected"}:
                blocked_reason = code
        phase = (
            "ready"
            if depth
            else "building"
            if compose_active
            else "blocked"
            if blocked_reason
            else "building"
            if active_jobs
            else "idle"
        )
        return {
            "user_scope_id": state.user_scope_id,
            "feed_state_version": "home-repository-v2",
            "feed_version": state.active_version,
            "active_since": state.active_since,
            "auto_promotion_min_age_seconds": 1800,
            "auto_promotion_eligible": bool(depth and (
                not state.active_artifact_id or not state.active_since
                or time.time() - state.active_since >= 1800
            )),
            "queue_revision": state.revision,
            "ready_feed_count": depth,
            "ready_feed_depth": depth,
            "ready_feed_target_depth": QUEUE_TARGET_DEPTH,
            "ready_feed_session_ids": complete_bundle_ids,
            "queue_build_inflight": phase == "building",
            "background_supply_inflight": active_jobs,
            "preparation_blocked_reason": blocked_reason if phase == "blocked" else "",
            "queue_phase": phase,
            "preparation_state": phase,
            "prepared_candidate_available": bool(state.ready_artifact_ids),
            "ready_bundle_count": depth,
            "ready_bundle_target": QUEUE_TARGET_DEPTH,
            "ready_bundles_complete": (
                depth >= QUEUE_TARGET_DEPTH
                and depth == len(state.ready_artifact_ids)
            ),
            "feed_readiness_contract": "complete_immutable_bundle_v1",
            "active_artifact_id": state.active_artifact_id,
            "home_repository_cutover": True,
        }

    def _queue_status(
        self,
        request: Any,
        *,
        scope: str,
        request_id: str,
    ) -> dict[str, Any]:
        state = self._repositories.state.reconcile(scope)
        last_revision = max(
            int(
                getattr(request, "feed_queue_revision", 0)
                or getattr(request, "last_seen_queue_revision", 0)
                or 0
            ),
            0,
        )
        wait_ms = min(
            max(
                int(
                    getattr(request, "feed_queue_wait_ms", 0)
                    or getattr(request, "queue_wait_ms", 0)
                    or 0
                ),
                0,
            ),
            9000,
        )
        deadline = time.monotonic() + wait_ms / 1000.0
        while (
            wait_ms > 0
            and state.revision == last_revision
            and len(state.ready_artifact_ids) < QUEUE_TARGET_DEPTH
            and time.monotonic() < deadline
        ):
            time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))
            current = self._repositories.state.get(scope)
            if current is None or current.revision != state.revision:
                state = self._repositories.state.reconcile(scope)
        # Expiry can change validity without changing the state revision. Check
        # it once before publication, rather than decoding every bundle at 10Hz.
        if wait_ms > 0:
            state = self._repositories.state.reconcile(scope)
        diagnostics = {
            "engine": "discovery_engine",
            "feed_action": "queue_status",
            **self._queue_diagnostics(state),
        }
        return {
            "status": "success",
            "request_id": request_id,
            "session_id": state.active_artifact_id,
            "feed_version": state.active_version,
            "feed_action": "queue_status",
            "preparation_state": diagnostics["preparation_state"],
            "rows": [],
            "has_more": False,
            "next_offset": 0,
            "diagnostics": diagnostics,
        }

    def _serve_or_prepare(
        self,
        request: Any,
        state: HomeStateRecord,
        *,
        request_id: str,
        action: str,
        changed: bool = False,
        reason: str = "",
        trace: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_started = time.perf_counter()
        record = (
            self._repositories.artifacts.get(state.active_artifact_id)
            if state.active_artifact_id
            else None
        )
        stale_fallback = False
        if record is None:
            record = self._repositories.artifacts.latest_stale_fallback(
                state.user_scope_id
            )
            stale_fallback = record is not None
        artifact = artifact_from_record(record)
        _home_stage(trace, 'artifact_read', artifact_started)
        queue_started = time.perf_counter()
        queue_diagnostics = self._queue_diagnostics(state)
        _home_stage(trace, 'queue_diagnostics', queue_started)
        if artifact is None:
            diagnostics = {
                "engine": "discovery_engine",
                "feed_action": "preparing_initial",
                **queue_diagnostics,
                "artifact_status": "preparing",
                "artifact_quality": "preparing",
            }
            return {
                "status": "success",
                "request_id": request_id,
                "session_id": "",
                "feed_version": state.active_version,
                "feed_action": "preparing_initial",
                "preparation_state": diagnostics["preparation_state"],
                "rows": [],
                "has_more": False,
                "next_offset": 0,
                "diagnostics": diagnostics,
            }
        effective_action = (
            "served_stale_active_fallback" if stale_fallback else action
        )
        artifact.diagnostics = {
            **dict(artifact.diagnostics or {}),
            **queue_diagnostics,
            "feed_action": effective_action,
            "feed_version": state.active_version,
            "feed_changed": bool(changed),
            "artifact_status": (
                "stale_fallback" if stale_fallback else "servable"
            ),
            "artifact_quality": (
                "accepted_stale_fallback" if stale_fallback else "servable"
            ),
            "refresh_outcome": effective_action,
            "stale_while_revalidate": stale_fallback,
        }
        if stale_fallback:
            artifact.diagnostics["stale_age_ms"] = max(
                int((time.time() - float(record.expires_at)) * 1000),
                0,
            )
        if reason:
            artifact.diagnostics["feed_action_reason"] = reason
        artifact.artifact_source = (
            "stale_active_fallback"
            if stale_fallback
            else "cache"
            if action == "served_active"
            else action
        )
        if not stale_fallback:
            exposure_started = time.perf_counter()
            self._record_impressions_once(
                artifact,
                delivery_key="complete",
                row_offset=0,
                row_limit=None,
            )
            _home_stage(trace, 'exposures', exposure_started)
        projection_started = time.perf_counter()
        # Refresh presentation metadata from the same persisted artist record
        # used by artist details. Feed identity, order and promotion stay owned
        # by the durable artifact; this performs no external artwork lookup.
        _attach_inventory_portraits(artifact.rows, None, self._server)
        response = home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=max(
                int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
                1,
            ),
        )
        _home_stage(trace, 'projection', projection_started)
        return response

    def _row_page(
        self,
        request: Any,
        *,
        scope: str,
        request_id: str,
    ) -> dict[str, Any]:
        state = self._repositories.state.reconcile(scope)
        artifact_id = str(getattr(request, "session_id", "") or "").strip()
        if not artifact_id:
            artifact_id = state.active_artifact_id
        artifact = artifact_from_record(self._repositories.artifacts.get(artifact_id))
        if artifact is None or artifact.user_scope_id != scope:
            raise HTTPException(status_code=404, detail="Discovery row is not available")
        _attach_inventory_portraits(artifact.rows, None, self._server)
        response = row_page_response_from_artifact(
            artifact,
            row_id=str(getattr(request, "row_id", "") or "").strip(),
            offset=max(int(getattr(request, "offset", 0) or 0), 0),
            limit=max(int(getattr(request, "limit", 8) or 8), 1),
            request_id=request_id,
        )
        if response is None:
            raise HTTPException(status_code=404, detail="Discovery row is not available")
        return response

    def _record_impressions_once(
        self,
        artifact: Any,
        *,
        delivery_key: str,
        row_offset: int,
        row_limit: int | None,
    ) -> None:
        impression_key = f"{artifact.session_id}:{delivery_key}"
        with self._impression_lock:
            if impression_key in self._impressed_artifact_ids:
                return
            self._impressed_artifact_ids.add(impression_key)
            if len(self._impressed_artifact_ids) > 512:
                self._impressed_artifact_ids = {impression_key}
        self._repositories.exposures.record(
            artifact.user_scope_id,
            # This includes radio seed identities, already consumed by radio
            # supply. Promotion needs no second exposure writer/inventory CAS.
            artifact_exposure_entries(
                artifact,
                visible_only=True,
                row_offset=row_offset,
                row_limit=row_limit,
            ),
        )
        try:
            session = artifact_to_session(artifact)
            page_size = max(
                int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
                1,
            )
            selected_rows = list(session.get("rows") or [])[row_offset:]
            if row_limit is not None:
                selected_rows = selected_rows[:row_limit]
            visible = [
                {**row, "items": list(row.get("items") or [])[:page_size]}
                for row in selected_rows
                if isinstance(row, dict)
            ]
            self._server._recommendation_record_impressions(session, visible)
        except Exception:
            return

    @staticmethod
    def _refresh_wait_ms(request: Any) -> int:
        value = getattr(request, "feed_refresh_wait_ms", None)
        if value in (None, "", 0):
            value = getattr(request, "refresh_wait_ms", None)
        if value in (None, ""):
            value = 2500
        return max(0, min(int(value or 0), 3000))
