from __future__ import annotations

from typing import Any, Mapping
import threading
import time
import uuid

from fastapi import HTTPException

from ..discovery.adapters import (
    artifact_to_session,
    home_response_from_artifact,
    row_page_response_from_artifact,
)
from ..discovery.inventory import (
    candidate_inventory_from_payload,
)
from ..discovery.radio_inventory import (
    RADIO_EXPOSURE_LEDGER_MAX_ENTRIES,
    artist_radio_inventory_from_payload,
    merge_radio_exposure,
)
from .composer import artifact_exposure_entries, artifact_from_record
from .candidate_supply import exposure_aware_candidate_supply_status
from .bundles import is_complete_feed_bundle_payload
from .contracts import (
    CANDIDATE_REFRESH_RETRY_SECONDS,
    HomeJobType,
    HomeStateRecord,
    QUEUE_TARGET_DEPTH,
    home_supply_profile_fingerprint,
)
from .repositories import HomeRepositorySet, InvalidArtifactReference, StaleRevision
from .scheduler import HomeJobScheduler
from .radio_supply import _radio_expansion_needed


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

        state = self._repositories.state.reconcile(scope)
        if request_mode == "background_prepare":
            self._ensure_successors(request, state, reason="background_prepare")
            return self._serve_or_prepare(
                request, state, request_id=request_id, action="served_active"
            )

        force = bool(
            getattr(request, "force_refresh", False)
            or getattr(request, "prefer_fresh_rows", False)
        )
        session_intent = bool(getattr(request, "session_intent", False))
        launch = bool(getattr(request, "promote_ready_on_launch", False))
        promoted = ""
        action = "served_active"

        if force and state.ready_artifact_ids:
            state, promoted = self._promote(state)
            action = "promoted_prepared" if promoted else "unchanged_no_rotation"
        elif launch and not session_intent and state.ready_artifact_ids:
            launch_token = str(getattr(request, "launch_token", "") or "").strip()
            if not launch_token or launch_token != state.last_launch_token:
                state, promoted = self._promote(state, launch_token=launch_token)
                action = "promoted_prepared" if promoted else "served_active"
        elif not state.active_artifact_id and state.ready_artifact_ids:
            state, promoted = self._promote(state)
            action = "promoted_prepared" if promoted else "preparing_initial"

        if promoted:
            artifact = artifact_from_record(self._repositories.artifacts.get(promoted))
            if artifact is not None:
                self._record_radio_exposure(artifact)
            self._replenish_after_delivery(request, state, reason="post_promotion",
                                          background_tasks=background_tasks)
            return self._serve_or_prepare(
                request,
                state,
                request_id=request_id,
                action=action,
                changed=True,
            )

        if force and state.active_artifact_id and not state.ready_artifact_ids:
            self._ensure_successors(request, state, reason="pull_to_refresh")
            wait_ms = self._refresh_wait_ms(request)
            deadline = time.monotonic() + wait_ms / 1000.0
            while wait_ms > 0 and time.monotonic() < deadline:
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))
                state = self._repositories.state.reconcile(scope)
                if state.ready_artifact_ids:
                    state, promoted = self._promote(state)
                    if promoted:
                        artifact = artifact_from_record(
                            self._repositories.artifacts.get(promoted)
                        )
                        if artifact is not None:
                            self._record_radio_exposure(artifact)
                        self._replenish_after_delivery(request, state, reason="post_promotion",
                                                      background_tasks=background_tasks)
                        return self._serve_or_prepare(
                            request,
                            state,
                            request_id=request_id,
                            action="promoted_prepared",
                            changed=True,
                            reason="refresh_wait_promoted",
                        )
            action = "unchanged_no_rotation"

        self._replenish_after_delivery(
            request,
            state,
            reason=("launch_missing_successor" if launch else "serve_replenish"),
            background_tasks=background_tasks,
        )
        return self._serve_or_prepare(
            request,
            state,
            request_id=request_id,
            action=action,
            changed=False,
            reason=("refresh_preparing_timeout" if force else ""),
        )

    def _replenish_after_delivery(
        self, request: Any, state: HomeStateRecord, *, reason: str,
        background_tasks: Any = None,
    ) -> None:
        # Only accepted, persisted Home may bypass inventory decoding before
        # delivery. Initial supply and explicit preparation remain synchronous.
        if background_tasks is None or not state.active_artifact_id:
            self._ensure_successors(request, state, reason=reason)
            return
        background_tasks.add_task(self._replenish_delivered, request, state.user_scope_id, reason)

    def _replenish_delivered(self, request: Any, scope: str, reason: str) -> None:
        with self._replenishment_lock:
            if scope in self._replenishing_scopes:
                return
            self._replenishing_scopes.add(scope)
        try:
            state = self._repositories.state.reconcile(scope)
            from ..recommend.feature_store import request_store_runtime
            with request_store_runtime(allow_persistent_reads=False):
                self._ensure_successors(request, state, reason=reason)
        except Exception:
            # Existing durable jobs and the next request can recover this work.
            # Never include profile data or external error text in this warning.
            print("[home] delivered_feed_replenishment_failed", flush=True)
        finally:
            with self._replenishment_lock:
                self._replenishing_scopes.discard(scope)

    def _ensure_successors(
        self,
        request: Any,
        state: HomeStateRecord,
        *,
        reason: str,
    ) -> None:
        scope = state.user_scope_id
        profile = home_supply_profile_fingerprint(scope)
        # Never compose a post-cutover feed from a request-profile fragment.
        # If the stable account supply does not exist yet, its consolidation
        # job is the only valid next step.
        fresh_candidate = self._repositories.candidates.latest_fresh(
            scope,
            profile_fingerprint=profile,
        )
        candidate = fresh_candidate or self._repositories.candidates.latest_usable(
            scope,
            profile_fingerprint=profile,
        )
        candidate_inventory = (
            candidate_inventory_from_payload(dict(candidate.payload or {}))
            if candidate is not None
            else None
        )
        if candidate is None or candidate_inventory is None or not candidate_inventory.is_ready:
            active = self._repositories.jobs.has_active(
                scope,
                job_types=(HomeJobType.REFRESH_CANDIDATES,),
                ignore_delayed_idle=True,
            )
            retry_initial = (not state.active_artifact_id and
                             bool(getattr(request, "force_refresh", False)) and
                             not self._repositories.jobs.has_runnable(
                                 scope, job_types=(HomeJobType.REFRESH_CANDIDATES,)))
            if not active or retry_initial:
                # Explicit Retry on a never-published Home must consume the
                # current signals instead of waiting on a delayed stale job.
                # Singleton replacement preserves leased work; the provider
                # circuit/backoff still governs every acquisition request.
                self._scheduler.candidates(
                    request,
                    reason="candidate_inventory_refresh",
                    profile_fingerprint=profile,
                    generation_hint=(f"initial-retry:{int(time.time() // 300)}"
                                     if retry_initial else ""),
                )
            return
        if reason == "background_prepare" and any(
            getattr(request, key, None)
            for key in ("recent_track_ids", "top_track_ids", "last_played_tracks",
                        "library_track_ids", "artist_hints", "playlist_names")
        ) and not self._repositories.jobs.has_active(
            scope, job_types=(HomeJobType.REFRESH_CANDIDATES,), ignore_delayed_idle=True,
        ):
            # Listening/library changes need to enter durable supply even
            # while the old inventory is fresh and the ready queue is full.
            # Coalesce terminal attempts too: one refresh per five-minute
            # window, no provider work or active-feed promotion here.
            self._scheduler.candidates(
                request, reason="signal_reconcile", profile_fingerprint=profile,
                generation_hint=f"signals:{int(time.time() // (5 * 60))}",
                coalesce_terminal=True,
            )
        if fresh_candidate is None and not self._repositories.jobs.has_active(
            scope,
            job_types=(HomeJobType.REFRESH_CANDIDATES,),
            ignore_delayed_idle=True,
        ):
            # Verified facts remain usable, but expiry is a real refresh
            # deadline. Coalesce all requests in one bounded retry window so
            # stale supply cannot either suppress acquisition or flood it.
            refresh_window = int(time.time() // CANDIDATE_REFRESH_RETRY_SECONDS)
            self._scheduler.candidates(
                request,
                reason="candidate_revalidation_due",
                profile_fingerprint=profile,
                generation_hint=(
                    f"revalidate:{candidate.snapshot_id}:{refresh_window}"
                ),
                coalesce_terminal=True,
            )
        elif (
            fresh_candidate is not None
            and bool(
                exposure_aware_candidate_supply_status(
                    candidate_inventory,
                    repositories=self._repositories,
                    user_scope_id=scope,
                )["expansion_needed"]
            )
            and not self._repositories.jobs.has_active(
                scope,
                job_types=(HomeJobType.REFRESH_CANDIDATES,),
                ignore_delayed_idle=True,
            )
        ):
            expansion_window = int(time.time() // (15 * 60))
            self._scheduler.candidates(
                request,
                reason="candidate_reserve_replenish",
                profile_fingerprint=profile,
                generation_hint=(
                    f"reserve:{candidate.snapshot_id}:{expansion_window}"
                ),
                coalesce_terminal=True,
            )
        fresh_radio = self._repositories.radio.latest_fresh(
            scope,
            profile_fingerprint=profile,
        )
        if fresh_radio is None:
            self._scheduler.radio(
                request,
                reason="radio_snapshot_refresh",
                profile_fingerprint=profile,
                candidate_snapshot_id=candidate.snapshot_id,
                generation_hint=f"candidate:{candidate.snapshot_id}",
            )
        elif (
            (radio_inventory := artist_radio_inventory_from_payload(
                dict(fresh_radio.payload or {})
            )) is not None
            and _radio_expansion_needed(radio_inventory)
            and not self._repositories.jobs.has_active(
                scope,
                job_types=(HomeJobType.REFRESH_RADIO,),
                ignore_delayed_idle=True,
            )
        ):
            radio_window = int(time.time() // (15 * 60))
            self._scheduler.radio(
                request,
                reason="radio_reserve_replenish",
                profile_fingerprint=profile,
                candidate_snapshot_id=candidate.snapshot_id,
                generation_hint=f"reserve:{fresh_radio.snapshot_id}:{radio_window}",
                coalesce_terminal=True,
            )
        reusable_radio = (
            fresh_radio
            or self._repositories.radio.latest_usable(
                scope,
                profile_fingerprint=profile,
            )
            or self._repositories.radio.latest_usable(scope)
        )
        # Initial publication still waits for the first complete independent
        # radio row. Once an accepted active feed exists, radio replacement is
        # no longer allowed to block improvements in the other rows.
        if reusable_radio is None and not state.active_artifact_id:
            return
        if len(state.ready_artifact_ids) >= QUEUE_TARGET_DEPTH:
            return
        radio_snapshot_id = (
            reusable_radio.snapshot_id if reusable_radio is not None else ""
        )
        allocation_epoch = (
            state.active_version + len(state.ready_artifact_ids) + 1
        )
        materialized = self._repositories.artifacts.get_by_supply(
            scope,
            candidate.snapshot_id,
            radio_snapshot_id,
            allocation_epoch=allocation_epoch,
        )
        state_ids = {
            state.active_artifact_id,
            *state.ready_artifact_ids,
        }
        if materialized is not None and materialized.artifact_id in state_ids:
            return
        self._scheduler.compose(
            request,
            reason=reason,
            profile_fingerprint=profile,
            candidate_snapshot_id=candidate.snapshot_id,
            radio_snapshot_id=radio_snapshot_id,
            allocation_epoch=allocation_epoch,
        )

    def _promote(
        self,
        state: HomeStateRecord,
        *,
        launch_token: str = "",
    ) -> tuple[HomeStateRecord, str]:
        try:
            return self._repositories.state.promote_ready(
                state.user_scope_id,
                expected_revision=state.revision,
                launch_token=launch_token,
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
            ),
        )
        complete_bundle_ids = [
            artifact_id
            for artifact_id in state.ready_artifact_ids
            if (
                (record := self._repositories.artifacts.get(artifact_id)) is not None
                and is_complete_feed_bundle_payload(record.payload)
            )
        ]
        depth = len(complete_bundle_ids)
        phase = (
            "ready"
            if depth >= QUEUE_TARGET_DEPTH
            else "building"
            if active_jobs
            else "idle"
        )
        return {
            "user_scope_id": state.user_scope_id,
            "feed_state_version": "home-repository-v2",
            "feed_version": state.active_version,
            "queue_revision": state.revision,
            "ready_feed_count": depth,
            "ready_feed_depth": depth,
            "ready_feed_target_depth": QUEUE_TARGET_DEPTH,
            "ready_feed_session_ids": complete_bundle_ids,
            "queue_build_inflight": active_jobs,
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
    ) -> dict[str, Any]:
        state = self._repositories.state.reconcile(state.user_scope_id)
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
        if artifact is None:
            diagnostics = {
                "engine": "discovery_engine",
                "feed_action": "preparing_initial",
                **self._queue_diagnostics(state),
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
            **self._queue_diagnostics(state),
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
            self._record_impressions_once(
                artifact,
                delivery_key="complete",
                row_offset=0,
                row_limit=None,
            )
        return home_response_from_artifact(
            artifact,
            request_id=request_id,
            page_size=max(
                int(getattr(self._server, "RECOMMENDATION_ROW_PAGE_SIZE", 8) or 8),
                1,
            ),
        )

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

    def _record_radio_exposure(self, artifact: Any) -> None:
        identities: list[str] = []
        for row in artifact.rows or []:
            if row.kind != "popular_radio":
                continue
            for card in row.items or []:
                if not isinstance(card, Mapping):
                    continue
                identity = str(
                    card.get("seed_artist_identity")
                    or card.get("seed_artist_key")
                    or card.get("artist_name")
                    or ""
                ).strip().casefold()
                if identity and identity not in identities:
                    identities.append(identity)
        if identities:
            merge_radio_exposure(
                self._server,
                artifact.user_scope_id,
                identities,
                max_entries=RADIO_EXPOSURE_LEDGER_MAX_ENTRIES,
            )

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
